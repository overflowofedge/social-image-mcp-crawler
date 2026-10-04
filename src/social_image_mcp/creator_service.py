from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .creator_protocol import creator_target, timestamp
from .intent import parse_intent
from .object_semantics import parse_content_spec
from .models import CreatorFetchRequest, CreatorSort, ImageCandidate
from .ranking import rank_candidates
from .semantic import SemanticReranker
from .sources import CreatorSourceResult, SourceError
from .vision import VisionReranker
from .object_detector import ObjectDetector
from .bilibili import BilibiliError
from .weibo import WeiboError


class CreatorStore:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS creator_jobs (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def get(self, key: str) -> dict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT payload FROM creator_jobs WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None

    def save(self, key: str, state: dict) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO creator_jobs VALUES (?, ?)", (key, json.dumps(state, ensure_ascii=False)))


class CreatorImageService:
    def __init__(self, settings, sources, downloader, semantic: SemanticReranker | None = None, vision: VisionReranker | None = None, object_detector: ObjectDetector | None = None, bilibili=None, weibo=None) -> None:
        self.settings, self.sources, self.downloader, self.bilibili, self.weibo = settings, sources, downloader, bilibili, weibo
        self.semantic, self.vision, self.object_detector = semantic, vision, object_detector
        self.store = CreatorStore(settings.cache_path)
        # Serialize retries for the same creator checkpoint while allowing
        # unrelated creator IDs to run concurrently.
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _request_key(request: CreatorFetchRequest) -> str:
        target = request.profile_url or request.creator_id or request.creator_name or "creator"
        return f"{request.platform.value}:{target}"

    async def _fetch_source_page(self, request: CreatorFetchRequest):
        """Read one logical source batch while preserving platform fallbacks."""
        cli_source = getattr(self.sources, "bilibili_source", None) if request.platform.value == "bilibili" else None
        if request.platform.value == "bilibili" and cli_source is not None and cli_source.command_template:
            return await self.sources.fetch_creator(request)
        if request.platform.value == "weibo" and getattr(getattr(self.sources, "media_crawler", None), "command_template", None):
            try:
                return await self.sources.fetch_creator(request)
            except SourceError as exc:
                if self.weibo is None:
                    raise
                try:
                    return await self.weibo.fetch_creator(request)
                except WeiboError as fallback_exc:
                    raise SourceError(f"weibo CLI: {exc}; public API fallback: {fallback_exc}") from fallback_exc
        if request.platform.value in {"bilibili", "weibo"} and getattr(self, request.platform.value, None) is not None:
            native_api = getattr(self, request.platform.value)
            try:
                return await native_api.fetch_creator(request)
            except (BilibiliError, WeiboError) as exc:
                if request.platform.value == "bilibili":
                    raise SourceError(str(exc)) from exc
                try:
                    return await self.sources.fetch_creator(request)
                except SourceError as fallback_exc:
                    raise SourceError(f"weibo public API: {exc}; media-crawler fallback: {fallback_exc}") from fallback_exc
        return await self.sources.fetch_creator(request)

    @staticmethod
    def _candidate_targets_met(items: list[ImageCandidate], request: CreatorFetchRequest) -> bool:
        """Stop paging after filling the requested quota plus a small replacement reserve."""
        image_target = request.max_images if request.media_type != "videos" else 0
        video_target = (request.max_videos if request.max_videos is not None else request.max_images) if request.media_type != "images" else 0
        reserve = min(20, max(2, (image_target + video_target + 9) // 10)) if request.download else 0
        image_needed = image_target + (reserve if image_target else 0)
        video_needed = video_target + (reserve if video_target else 0)
        images = videos = 0
        per_post: dict[str, int] = {}
        for item in items:
            if item.media_type == "image":
                post = item.post_id or item.id
                if request.per_post_limit is not None and per_post.get(post, 0) >= request.per_post_limit:
                    continue
                per_post[post] = per_post.get(post, 0) + 1
                images += 1
            elif item.media_type == "video":
                videos += 1
        return images >= image_needed and videos >= video_needed

    async def _collect_source_pages(self, request: CreatorFetchRequest) -> CreatorSourceResult:
        """Continue any source cursor until targets or the post budget are exhausted."""
        remaining = request.max_posts
        cursor = request.cursor
        visited = {cursor} if cursor else set()
        identity = None
        items: dict[tuple[str, str, int, str], ImageCandidate] = {}
        post_ids: list[str] = []
        known_posts: set[str] = set()
        warnings: list[str] = []
        rejected_posts = posts_fetched = pages_fetched = 0

        while remaining > 0:
            page_request = request.model_copy(update={"cursor": cursor, "max_posts": remaining})
            try:
                result = await self._fetch_source_page(page_request)
            except (SourceError, BilibiliError, WeiboError, ValueError) as exc:
                if identity is None:
                    raise
                warnings.append(f"creator pagination stopped after {pages_fetched} pages: {exc}")
                break
            if identity is None:
                identity = result.identity
            elif result.identity.canonical_id != identity.canonical_id:
                raise SourceError("creator identity changed while paging; stopped before mixing accounts")

            for item in result.items:
                key = (item.post_id or item.id, item.media_type, item.media_index or 1, item.image_url)
                items[key] = item
            for post_id in result.post_ids:
                if post_id not in known_posts:
                    known_posts.add(post_id)
                    post_ids.append(post_id)
            warnings.extend(result.warnings)
            rejected_posts += result.rejected_posts
            progress = max(0, int(result.posts_fetched))
            posts_fetched += progress
            pages_fetched += max(1, int(result.pages_fetched or 0))
            remaining = max(0, request.max_posts - posts_fetched)
            next_cursor = result.next_cursor

            if self._candidate_targets_met(list(items.values()), request) or not next_cursor or remaining <= 0:
                cursor = next_cursor
                break
            if next_cursor == cursor or next_cursor in visited or progress <= 0:
                warnings.append("creator pagination stopped because the source cursor made no progress")
                cursor = next_cursor
                break
            visited.add(next_cursor)
            cursor = next_cursor

        if identity is None:
            raise SourceError("creator source returned no identity")
        return CreatorSourceResult(
            identity=identity,
            items=list(items.values()),
            posts_fetched=posts_fetched,
            next_cursor=cursor,
            post_ids=tuple(post_ids),
            rejected_posts=rejected_posts,
            pages_fetched=pages_fetched,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    async def fetch(self, request: CreatorFetchRequest) -> dict:
        started = time.monotonic()
        try:
            lock = self._locks.setdefault(self._request_key(request), asyncio.Lock())
            video_target = (request.max_videos if request.max_videos is not None else request.max_images) if request.media_type != "images" else 0
            image_target = request.max_images if request.media_type != "videos" else 0
            task_timeout = min(
                7200.0,
                max(
                    float(self.settings.creator_timeout_seconds),
                    90.0 + request.max_posts * 1.5 + image_target * 3.0 + video_target * 20.0,
                ),
            )
            async with lock:
                result = await asyncio.wait_for(self._fetch(request), timeout=task_timeout)
        except asyncio.TimeoutError:
            result = {"items": [], "downloads": [], "error": {"code": "creator_timeout", "message": "Account retrieval reached its deadline. Retry with resume=true; completed files and pending items were retained."}}
        except (ValueError, SourceError, BilibiliError, WeiboError) as exc:
            result = {"items": [], "downloads": [], "error": {"code": "creator_source_error", "message": str(exc)}}
        result.update({"platform": request.platform.value,
                       "media_type": request.media_type,
                       "semantic_requested": bool(request.content_query),
                       "semantic_applied": result.get("semantic_applied", False),
                       "vision_applied": result.get("vision_applied", False),
                       "elapsed_seconds": round(time.monotonic() - started, 2)})
        return result

    async def _fetch(self, request: CreatorFetchRequest) -> dict:
        target = creator_target(request.platform.value, request.profile_url or request.creator_id) if not request.creator_name else request.creator_name.strip()
        safe_target = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", target).strip("._")[:120] or "creator"
        scope = request.model_dump(mode="json", exclude={"max_posts", "max_images", "creator_id", "profile_url", "output_dir", "download", "resume", "max_concurrency"})
        # Keep creator downloads in a short, account-specific layout:
        # downloads/<platform>/<account>/<images|videos>.
        output = self.settings.ensure_output_dir(request.output_dir or str(Path(self.settings.output_dir) / request.platform.value / safe_target)).resolve()
        key = hashlib.sha256(json.dumps([target, str(output), scope], sort_keys=True).encode()).hexdigest()
        state = self.store.get(key) if request.resume else None
        if state is None:
            state = {
                "identity": None,
                "cursor": request.cursor,
                "pending": [],
                "seen": [],
                "catalog": {},
                "completed": [],
                "exhausted": False,
                "last_refresh_at": None,
                "refresh_count": 0,
                "posts_fetched": 0,
                "post_ids": [],
                "pages_fetched": 0,
                "rejected_posts": 0,
                "warnings": [],
                "semantic_applied": False,
                "vision_applied": False,
                "filtered_count": 0,
                "rejected_by_content_filter": 0,
                "filter_warning": None,
                "filter_error": None,
                "object_detection_applied": False,
                 "object_detection_error": None,
                 "object_backend": None,
                 "object_decisions": {},
                 "object_rejected": 0,
                 "object_unverified": 0,
            }
        else:
            # Checkpoints created by older versions did not carry cumulative
            # counters. Keep them resumable and backfill the new fields.
            state.setdefault("posts_fetched", 0)
            state.setdefault("post_ids", [])
            state.setdefault("pages_fetched", 0)
            state.setdefault("rejected_posts", 0)
            state.setdefault("warnings", [])
            state.setdefault("semantic_applied", False)
            state.setdefault("vision_applied", False)
            state.setdefault("filtered_count", 0)
            state.setdefault("rejected_by_content_filter", 0)
            state.setdefault("filter_warning", None)
            state.setdefault("filter_error", None)
            state.setdefault("object_detection_applied", False)
            state.setdefault("object_detection_error", None)
            state.setdefault("object_backend", None)
            state.setdefault("object_decisions", {})
            state.setdefault("object_rejected", 0)
            state.setdefault("object_unverified", 0)
            state.setdefault("last_refresh_at", None)
            state.setdefault("refresh_count", 0)
            state.setdefault("catalog", {})
            state.setdefault("completed", [])
        warnings = []
        source_result = None
        # Once the previous batch is drained, probe the source again even if
        # its old cursor was exhausted. Creator timelines are append-only in
        # normal use, so the first page acts as a cheap incremental sync: old
        # post/media keys are discarded by ``seen`` and only new works enter
        # the pending queue. A pending batch is left untouched so a failed
        # download can be retried without advancing the source cursor.
        if not state["pending"] and (not state["exhausted"] or request.resume):
            source_data = request.model_dump(mode="python")
            source_data["cursor"] = state["cursor"]
            # A successful first lookup resolves a Douyin handle to sec_uid.
            # Resume with that canonical profile URL so search_users is not
            # repeated on every page or download retry.
            if state["identity"] and state["identity"].get("profile_url"):
                source_data["creator_id"] = None
                source_data["creator_name"] = None
                source_data["profile_url"] = state["identity"]["profile_url"]
            source_request = CreatorFetchRequest.model_validate(source_data)
            source_result = await self._collect_source_pages(source_request)
            identity = source_result.identity
            if state["identity"] and state["identity"]["canonical_id"] != identity.canonical_id:
                raise SourceError("creator identity changed since the saved checkpoint; inspect the account before restarting")
            state["identity"] = identity.model_dump(mode="json")
            state["posts_fetched"] += source_result.posts_fetched
            state["pages_fetched"] += source_result.pages_fetched
            known_post_ids = set(state["post_ids"])
            for post_id in source_result.post_ids:
                if post_id not in known_post_ids:
                    state["post_ids"].append(post_id)
                    known_post_ids.add(post_id)
            seen = set(state["seen"])
            catalog = state.get("catalog") if isinstance(state.get("catalog"), dict) else {}
            valid = []
            rejected = source_result.rejected_posts
            for item in source_result.items:
                if item.platform != request.platform or item.creator_id != identity.canonical_id or not item.post_id or not item.media_index:
                    rejected += 1
                    continue
                media_key = f"{item.post_id}:{item.media_index}"
                catalog[media_key] = item.model_dump(mode="json")
                if media_key in seen:
                    continue
                seen.add(media_key)
                date = timestamp(item.published_at)
                if request.since or request.until:
                    if date is None:
                        warnings.append(f"missing publication date: {item.post_id}")
                        continue
                    if request.since and date < request.since.timestamp():
                        continue
                    if request.until and date > request.until.timestamp():
                        continue
                valid.append(item)
            if rejected:
                warnings.append(f"rejected {rejected} posts/images with missing or mismatched author identity")
            state["rejected_posts"] += rejected
            warnings.extend(source_result.warnings)
            if request.sort == CreatorSort.POPULAR:
                valid.sort(key=lambda item: item.engagement_score, reverse=True)
            else:
                valid.sort(key=lambda item: timestamp(item.published_at) or 0, reverse=True)

            if request.content_query:
                valid, filter_meta = await self._filter_content(valid, request)
                state.update(filter_meta)
            state.update({"seen": list(seen), "catalog": catalog, "pending": [item.model_dump(mode="json") for item in valid],
                          "cursor": source_result.next_cursor, "exhausted": source_result.next_cursor is None})
            state["last_refresh_at"] = timestamp(datetime.now(timezone.utc).isoformat())
            state["refresh_count"] = int(state.get("refresh_count") or 0) + 1
            state["warnings"].extend(warnings)
            self.store.save(key, state)
        pending = [ImageCandidate.model_validate(item) for item in state["pending"]]
        selected = self._select_with_quotas(pending, request)
        records = []
        if request.download and selected:
            selected = []
            attempted: set[str] = set()
            successful = {"image": 0, "video": 0}
            successful_per_post: dict[str, int] = {}
            completed = set(state.get("completed") or [])
            target_total = (request.max_images if request.media_type != "videos" else 0) + (
                (request.max_videos if request.max_videos is not None else request.max_images)
                if request.media_type != "images" else 0
            )
            attempt_budget = min(len(pending), max(target_total * 3, target_total + 50))
            while len(attempted) < attempt_budget and not self._download_targets_met(successful, request):
                batch = self._select_download_batch(
                    pending, request, attempted, successful, successful_per_post,
                    attempt_budget - len(attempted),
                )
                if not batch:
                    break
                selected.extend(batch)
                attempted.update(item.id for item in batch)
                batch_records = await self.downloader.download_many(
                    batch, output, request.max_concurrency, request.min_width,
                    request.min_height, resume=request.resume or bool(records),
                )
                records.extend(batch_records)
                by_id = {item.id: item for item in batch}
                terminal = {
                    record.candidate_id for record in batch_records
                    if record.status in {"downloaded", "existing", "duplicate", "rejected"}
                }
                batch_successes = 0
                for record in batch_records:
                    item = by_id.get(record.candidate_id)
                    if item is None or record.status not in {"downloaded", "existing"}:
                        continue
                    batch_successes += 1
                    successful[item.media_type] += 1
                    if item.media_type == "image":
                        post = item.post_id or item.id
                        successful_per_post[post] = successful_per_post.get(post, 0) + 1
                    completed.add(f"{item.post_id}:{item.media_index}")
                state["completed"] = sorted(completed)
                state["pending"] = [item for item in state["pending"] if item["id"] not in terminal]
                self.store.save(key, state)
                # A whole batch of transport failures usually means the
                # platform/CDN is unavailable; avoid multiplying the failure.
                if not batch_successes and all(record.status == "failed" for record in batch_records):
                    break
        elif warnings:
            state["warnings"].extend(warnings)
            self.store.save(key, state)
        partial = any(record.status == "failed" for record in records) or bool(state["warnings"]) or bool(state.get("filter_warning")) or bool(state.get("filter_error"))
        error = None
        if state.get("filter_error"):
            error = {"code": "content_filter_required", "message": str(state["filter_error"])}
        selected_payload = [item.model_dump(mode="json") for item in selected]
        pending_keys = {
            f"{item.get('post_id') or item.get('id')}:{item.get('media_index')}"
            for item in state.get("pending", [])
            if isinstance(item, dict)
        }
        # ``catalog`` is the durable de-duplication ledger.  The response
        # describes the batch selected for this call so the desktop list does
        # not repeat every historical work after an incremental refresh.
        display_items = selected or [ImageCandidate.model_validate(item) for item in state.get("pending", [])]
        works = self._group_works(display_items, set(state.get("completed") or []), pending_keys)
        new_work_ids = sorted({item.post_id or item.id for item in selected})
        return {"identity": state["identity"], "items": selected_payload, "works": works,
                "work_types": self._work_type_counts(works),
                "new_work_ids": new_work_ids,
                "downloads": [record.model_dump(mode="json") for record in records], "output_dir": str(output),
                "next_cursor": state["cursor"], "pending_images": len(state["pending"]),
                "has_more": bool(state["pending"]) or not state["exhausted"],
                "last_refresh_at": state.get("last_refresh_at"),
                "refresh_count": int(state.get("refresh_count") or 0),
                "posts_fetched": state["posts_fetched"],
                "post_ids": list(state["post_ids"]),
                "pages_fetched": state["pages_fetched"],
                "rejected_posts": state["rejected_posts"],
                "warnings": list(state["warnings"]), "status": "partial" if partial else "ok", "error": error,
                "sort_scope": "fetched_posts", "checkpoint": key,
                "content_query": request.content_query,
                "content_spec": self._content_spec_payload(request.content_query),
                "filter_mode": request.filter_mode,
                "quality_mode": request.quality_mode,
                "media_type": request.media_type,
                "image_limit": request.max_images,
                "video_limit": request.max_videos,
                "per_post_limit": request.per_post_limit,
                "semantic_requested": bool(request.content_query),
                "semantic_applied": bool(state.get("semantic_applied")),
                "vision_applied": bool(state.get("vision_applied")),
                "filtered_count": int(state.get("filtered_count") or 0),
                "rejected_by_content_filter": int(state.get("rejected_by_content_filter") or 0),
                "filter_warning": state.get("filter_warning"),
                "filter_error": state.get("filter_error"),
                "object_detection_applied": bool(state.get("object_detection_applied")),
                "object_detection_error": state.get("object_detection_error"),
                "object_backend": state.get("object_backend"),
                "object_decisions": state.get("object_decisions") or {},
                "object_rejected": int(state.get("object_rejected") or 0),
                "object_unverified": int(state.get("object_unverified") or 0)}

    @staticmethod
    def _group_works(items: list[ImageCandidate], completed: set[str] | None = None, pending: set[str] | None = None) -> list[dict]:
        """Present a creator response as works with media children.

        Source adapters return one row per downloadable asset. Grouping at
        this boundary gives every platform the same browsable work list while
        keeping adapter-specific parsing isolated.
        """
        completed = completed or set()
        pending = pending or set()
        grouped: dict[str, list[ImageCandidate]] = {}
        order: list[str] = []
        for item in items:
            work_id = item.post_id or item.id
            if work_id not in grouped:
                grouped[work_id] = []
                order.append(work_id)
            grouped[work_id].append(item)
        works: list[dict] = []
        for work_id in order:
            media = grouped[work_id]
            kinds = {item.media_type for item in media}
            work_type = "mixed" if len(kinds) > 1 else next(iter(kinds), "unknown")
            first = media[0]
            work_payload = {
                "work_id": work_id,
                "title": first.title,
                "published_at": first.published_at,
                "media_type": work_type,
                "media_count": len(media),
                "image_count": sum(item.media_type == "image" for item in media),
                "video_count": sum(item.media_type == "video" for item in media),
                "items": [],
            }
            for item in media:
                item_payload = item.model_dump(mode="json")
                media_key = f"{item.post_id}:{item.media_index}"
                item_payload["download_status"] = "downloaded" if media_key in completed else ("pending" if media_key in pending else "known")
                work_payload["items"].append(item_payload)
            work_payload["download_status"] = "downloaded" if all(item.get("download_status") == "downloaded" for item in work_payload["items"]) else ("pending" if any(item.get("download_status") == "pending" for item in work_payload["items"]) else "known")
            works.append(work_payload)
        return works

    @staticmethod
    def _work_type_counts(works: list[dict]) -> dict[str, int]:
        counts = {"image": 0, "video": 0, "mixed": 0}
        for work in works:
            kind = str(work.get("media_type") or "mixed")
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    @staticmethod
    def _select_with_quotas(items: list[ImageCandidate], request: CreatorFetchRequest) -> list[ImageCandidate]:
        """Apply separate media quotas and an image limit per post."""
        selected: list[ImageCandidate] = []
        images = videos = 0
        video_limit = request.max_videos if request.max_videos is not None else request.max_images
        per_post: dict[str, int] = {}
        for item in items:
            if request.media_type == "images" and item.media_type != "image":
                continue
            if request.media_type == "videos" and item.media_type != "video":
                continue
            post = item.post_id or item.id
            if item.media_type == "image" and request.per_post_limit is not None and per_post.get(post, 0) >= request.per_post_limit:
                continue
            if item.media_type == "image":
                if images >= request.max_images:
                    continue
                images += 1
                per_post[post] = per_post.get(post, 0) + 1
            else:
                if videos >= video_limit:
                    continue
                videos += 1
            selected.append(item)
            if request.media_type == "images" and images >= request.max_images:
                break
            if request.media_type == "videos" and videos >= video_limit:
                break
            if request.media_type == "all" and images >= request.max_images and videos >= video_limit:
                break
        return selected

    @staticmethod
    def _download_targets_met(counts: dict[str, int], request: CreatorFetchRequest) -> bool:
        image_target = request.max_images if request.media_type != "videos" else 0
        video_target = (request.max_videos if request.max_videos is not None else request.max_images) if request.media_type != "images" else 0
        return counts.get("image", 0) >= image_target and counts.get("video", 0) >= video_target

    @staticmethod
    def _select_download_batch(
        items: list[ImageCandidate],
        request: CreatorFetchRequest,
        attempted: set[str],
        successful: dict[str, int],
        successful_per_post: dict[str, int],
        budget: int,
    ) -> list[ImageCandidate]:
        """Choose replacements without letting failed/duplicate candidates consume the quota."""
        image_target = request.max_images if request.media_type != "videos" else 0
        video_target = (request.max_videos if request.max_videos is not None else request.max_images) if request.media_type != "images" else 0
        batch: list[ImageCandidate] = []
        batch_counts = {"image": 0, "video": 0}
        batch_per_post: dict[str, int] = {}
        for item in items:
            if item.id in attempted:
                continue
            if request.media_type == "images" and item.media_type != "image":
                continue
            if request.media_type == "videos" and item.media_type != "video":
                continue
            target = image_target if item.media_type == "image" else video_target
            if successful[item.media_type] + batch_counts[item.media_type] >= target:
                continue
            post = item.post_id or item.id
            if item.media_type == "image" and request.per_post_limit is not None:
                used = successful_per_post.get(post, 0) + batch_per_post.get(post, 0)
                if used >= request.per_post_limit:
                    continue
                batch_per_post[post] = batch_per_post.get(post, 0) + 1
            batch.append(item)
            batch_counts[item.media_type] += 1
            if len(batch) >= budget:
                break
            if (
                successful["image"] + batch_counts["image"] >= image_target
                and successful["video"] + batch_counts["video"] >= video_target
            ):
                break
        return batch
                

    async def _filter_content(self, items: list[ImageCandidate], request: CreatorFetchRequest) -> tuple[list[ImageCandidate], dict]:
        """Optionally rank creator images by a positive/negative content brief.

        The account identity filter has already run. This layer only changes
        content relevance and never changes the verified creator ID.
        """
        if not items or not request.content_query:
            return items, {"filtered_count": 0, "rejected_by_content_filter": 0, "filter_warning": None,
                           "filter_error": None, "semantic_applied": False, "vision_applied": False}
        intent = parse_intent(request.content_query)
        spec = parse_content_spec(request.content_query)
        pool_limit = min(48, max(request.max_images * 2, request.max_images))
        ranked = rank_candidates(items, intent, pool_limit, request.min_width, request.min_height)
        object_detection_applied = False
        object_detection_error = None
        object_backend = None
        object_decisions: dict = {}
        object_rejected = 0
        object_unverified = 0
        if self.object_detector and self.object_detector.configured and spec.object_level and ranked:
            try:
                ranked, object_meta = await asyncio.wait_for(
                    self.object_detector.inspect(ranked, spec, fail_closed=request.filter_mode == "required"),
                    timeout=max(0.05, float(getattr(self.settings, "object_filter_timeout_seconds", 4))),
                )
            except asyncio.TimeoutError:
                ranked, object_meta = ranked, {
                    "object_detection_applied": False,
                    "object_detection_error": "object filtering exceeded its time budget",
                    "object_decisions": {}, "object_rejected": 0, "object_unverified": len(ranked),
                }
            object_detection_applied = bool(object_meta.get("object_detection_applied"))
            object_detection_error = object_meta.get("object_detection_error")
            object_backend = object_meta.get("object_backend")
            object_decisions = object_meta.get("object_decisions") or {}
            object_rejected = int(object_meta.get("object_rejected") or 0)
            object_unverified = int(object_meta.get("object_unverified") or 0)
        elif spec.object_level:
            object_detection_error = "object detector is not configured"
        if request.filter_mode == "required" and spec.object_level and not object_detection_applied:
            return [], {"filtered_count": 0, "rejected_by_content_filter": len(items),
                        "filter_warning": object_detection_error,
                        "filter_error": "required object detection is unavailable",
                        "semantic_applied": False, "vision_applied": False,
                        "object_detection_applied": False, "object_detection_error": object_detection_error,
                        "object_backend": object_backend,
                        "object_decisions": object_decisions, "object_rejected": len(items), "object_unverified": object_unverified}
        semantic_applied = False
        vision_applied = False
        warning = None
        if self.semantic and self.semantic.enabled and ranked and request.quality_mode != "fast":
            ranked = await self.semantic.rerank(ranked, intent, len(ranked))
            semantic_applied = bool(getattr(self.semantic, "_model", None) is not None)
        # Object classification compares explicit include/exclude concepts;
        # the CLIP pass below additionally scores the complete natural-language
        # brief (needed when no object detector is configured).
        if self.vision and self.vision.enabled and ranked:
            self.vision.start_loading()
            try:
                # Even fast creator jobs with an explicit content_query need
                # a visual relevance check; lexical captions alone are not
                # reliable for Douyin/Weibo. Limit the CLIP pass to a bounded
                # shortlist to keep large jobs responsive.
                vision_pool = ranked[: min(32, len(ranked))]
                vision_ranked = await asyncio.wait_for(
                    self.vision.rerank(vision_pool, intent, len(vision_pool)),
                    timeout=max(1, int(getattr(self.settings, "vision_timeout_seconds", 8))),
                )
                ranked = vision_ranked + ranked[len(vision_pool):]
                vision_applied = any("vision_similarity" in item.source_payload for item in ranked)
            except asyncio.TimeoutError:
                warning = "visual content filtering timed out; optional account results were retained"
            except Exception as exc:
                warning = f"visual content filtering unavailable: {exc}"
        if not semantic_applied and not vision_applied:
            warning = warning or object_detection_error or "content filter models unavailable; optional account results were retained"
        if request.filter_mode == "required" and not (semantic_applied or vision_applied or object_detection_applied):
            return [], {"filtered_count": 0, "rejected_by_content_filter": len(items),
                        "filter_warning": "required content filtering could not run",
                        "filter_error": "required content filtering could not run",
                        "semantic_applied": False, "vision_applied": False,
                        "object_detection_applied": object_detection_applied,
                        "object_detection_error": object_detection_error,
                        "object_backend": object_backend,
                        "object_decisions": object_decisions, "object_rejected": object_rejected, "object_unverified": object_unverified}
        if vision_applied:
            threshold = 0.08 if request.quality_mode == "strict" else 0.0
            # Do not let the unclassified tail pass merely because its default
            # margin is zero.  That would reintroduce unrelated posts when a
            # large account request is truncated to a visual shortlist.
            visual_items = [item for item in ranked if "vision_similarity" in item.source_payload]
            filtered = [item for item in visual_items if float(item.source_payload.get("vision_margin", 0.0)) >= threshold]
            if not filtered and request.filter_mode == "optional" and not object_detection_applied:
                filtered = items
                warning = warning or "no image cleared the visual threshold; optional account results were retained"
            elif not filtered and request.filter_mode == "required":
                return [], {"filtered_count": 0, "rejected_by_content_filter": len(items),
                            "filter_warning": "no image cleared the required content threshold",
                            "filter_error": "no image cleared the required content threshold",
                            "semantic_applied": semantic_applied, "vision_applied": vision_applied,
                            "object_detection_applied": object_detection_applied,
                            "object_detection_error": object_detection_error,
                            "object_decisions": object_decisions, "object_rejected": object_rejected, "object_unverified": object_unverified}
        else:
            filtered = ranked if object_detection_applied else (ranked or items)
        if request.filter_mode == "optional" and not filtered and not object_detection_applied:
            filtered = items
        if request.filter_mode == "required" and not filtered:
            return [], {"filtered_count": 0, "rejected_by_content_filter": len(items),
                        "filter_warning": "no image matched the required content rules",
                        "filter_error": "no image matched the required content rules",
                        "semantic_applied": semantic_applied, "vision_applied": vision_applied,
                        "object_detection_applied": object_detection_applied,
                        "object_detection_error": object_detection_error,
                        "object_backend": object_backend,
                        "object_decisions": object_decisions, "object_rejected": object_rejected, "object_unverified": object_unverified}
        return filtered, {"filtered_count": len(filtered),
                          "rejected_by_content_filter": max(0, len(items) - len(filtered)),
                          "filter_warning": warning, "filter_error": None,
                          "semantic_applied": semantic_applied, "vision_applied": vision_applied,
                          "object_detection_applied": object_detection_applied,
                          "object_detection_error": object_detection_error,
                          "object_backend": object_backend,
                          "object_decisions": object_decisions, "object_rejected": object_rejected, "object_unverified": object_unverified}

    @staticmethod
    def _content_spec_payload(query: str | None) -> dict | None:
        if not query:
            return None
        spec = parse_content_spec(query)
        return {"include": list(spec.include), "exclude": list(spec.exclude),
                "required": list(spec.required), "region": spec.region,
                "object_level": spec.object_level}
