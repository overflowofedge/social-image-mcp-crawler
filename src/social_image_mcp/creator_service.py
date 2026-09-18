from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from .creator_protocol import creator_target, timestamp
from .intent import parse_intent
from .object_semantics import parse_content_spec
from .models import CreatorFetchRequest, CreatorSort, ImageCandidate
from .ranking import rank_candidates
from .semantic import SemanticReranker
from .sources import SourceError
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

    async def fetch(self, request: CreatorFetchRequest) -> dict:
        started = time.monotonic()
        try:
            lock = self._locks.setdefault(self._request_key(request), asyncio.Lock())
            async with lock:
                result = await asyncio.wait_for(self._fetch(request), timeout=self.settings.creator_timeout_seconds)
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
        output = self.settings.ensure_output_dir(request.output_dir or str(Path(self.settings.output_dir) / "creators" / request.platform.value / safe_target)).resolve()
        key = hashlib.sha256(json.dumps([target, str(output), scope], sort_keys=True).encode()).hexdigest()
        state = self.store.get(key) if request.resume else None
        if state is None:
            state = {
                "identity": None,
                "cursor": request.cursor,
                "pending": [],
                "seen": [],
                "exhausted": False,
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
        warnings = []
        source_result = None
        if not state["pending"] and not state["exhausted"]:
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
            if request.platform.value in {"bilibili", "weibo"} and getattr(self, request.platform.value, None) is not None:
                native_api = getattr(self, request.platform.value)
                try:
                    source_result = await native_api.fetch_creator(source_request)
                except (BilibiliError, WeiboError) as exc:
                    if request.platform.value == "bilibili" or request.creator_name:
                        raise SourceError(str(exc)) from exc
                    # Weibo's mobile endpoint is rate limited in some
                    # regions; retain MediaCrawler as an authenticated,
                    # bounded fallback when it is configured.
                    try:
                        source_result = await self.sources.fetch_creator(source_request)
                    except SourceError as fallback_exc:
                        raise SourceError(f"weibo public API: {exc}; media-crawler fallback: {fallback_exc}") from fallback_exc
            else:
                source_result = await self.sources.fetch_creator(source_request)
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
            valid = []
            rejected = source_result.rejected_posts
            for item in source_result.items:
                if item.platform != request.platform or item.creator_id != identity.canonical_id or not item.post_id or not item.media_index:
                    rejected += 1
                    continue
                media_key = f"{item.post_id}:{item.media_index}"
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
            state.update({"seen": list(seen), "pending": [item.model_dump(mode="json") for item in valid],
                          "cursor": source_result.next_cursor, "exhausted": source_result.next_cursor is None})
            state["warnings"].extend(warnings)
            self.store.save(key, state)
        selected = self._select_with_quotas(
            [ImageCandidate.model_validate(item) for item in state["pending"]], request
        )
        records = []
        if request.download and selected:
            records = await self.downloader.download_many(selected, output, request.max_concurrency, request.min_width, request.min_height, resume=request.resume)
            finished = {record.candidate_id for record in records if record.status in {"downloaded", "existing", "duplicate", "rejected"}}
            state["pending"] = [item for item in state["pending"] if item["id"] not in finished]
            self.store.save(key, state)
        elif warnings:
            state["warnings"].extend(warnings)
            self.store.save(key, state)
        partial = any(record.status == "failed" for record in records) or bool(state["warnings"]) or bool(state.get("filter_warning")) or bool(state.get("filter_error"))
        error = None
        if state.get("filter_error"):
            error = {"code": "content_filter_required", "message": str(state["filter_error"])}
        return {"identity": state["identity"], "items": [item.model_dump(mode="json") for item in selected],
                "downloads": [record.model_dump(mode="json") for record in records], "output_dir": str(output),
                "next_cursor": state["cursor"], "pending_images": len(state["pending"]),
                "has_more": bool(state["pending"]) or not state["exhausted"],
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
    def _select_with_quotas(items: list[ImageCandidate], request: CreatorFetchRequest) -> list[ImageCandidate]:
        """Apply total image/video and per-post limits deterministically."""
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
            if request.per_post_limit is not None and per_post.get(post, 0) >= request.per_post_limit:
                continue
            if item.media_type == "image":
                if images >= request.max_images:
                    continue
                images += 1
            else:
                if videos >= video_limit:
                    continue
                videos += 1
            per_post[post] = per_post.get(post, 0) + 1
            selected.append(item)
            if request.media_type == "images" and images >= request.max_images:
                break
            if request.media_type == "videos" and videos >= video_limit:
                break
            if request.media_type == "all" and images >= request.max_images and videos >= video_limit:
                break
        return selected
                

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
