from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx

from .adapters import AdapterError, AdapterUnavailable, build_adapters
from .cache import SearchCache
from .config import Settings
from .creator_service import CreatorImageService
from .downloader import ImageDownloader
from .intent import parse_intent
from .models import CreatorFetchRequest, DownloadRecord, ImageCandidate, Platform, SearchRequest
from .ranking import rank_candidates
from .semantic import SemanticReranker
from .discovery import BingImageDiscovery, DiscoveryError
from .vision import VisionReranker
from .object_detector import ObjectDetector
from .sources import SourceError, SourceHub, SourceUnavailable
from .feedback import FeedbackStore
from .weibo import WeiboApi


class SocialImageService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.client: httpx.AsyncClient | None = None
        self.cache: SearchCache | None = None
        self.adapters = {}
        self.downloader: ImageDownloader | None = None
        self.reranker = SemanticReranker(self.settings.semantic_model)
        self.discovery: BingImageDiscovery | None = None
        self.vision: VisionReranker | None = None
        self.object_detector: ObjectDetector | None = None
        self.sources: SourceHub | None = None
        self.creators: CreatorImageService | None = None
        self.feedback = FeedbackStore(self.settings.cache_path)
        self._verified_adapters: set[Platform] = set()

    async def start(self) -> None:
        if self.client:
            return
        self.client = httpx.AsyncClient(headers={"User-Agent": "social-image-mcp/0.1"})
        self.cache = SearchCache(self.settings.cache_path)
        self.adapters = build_adapters(self.settings, self.client)
        self.downloader = ImageDownloader(self.client)
        self.discovery = BingImageDiscovery(self.client)
        self.vision = VisionReranker(
            self.settings.vision_model,
            self.client,
            local_files_only=self.settings.vision_local_only,
            image_timeout_seconds=self.settings.vision_image_timeout_seconds,
        )
        self.object_detector = ObjectDetector(
            self.settings.object_model, self.client, self.settings.object_confidence,
            label_map=getattr(self.settings, "object_label_map", None),
            vision=self.vision,
        )
        self.sources = SourceHub(self.settings.media_crawler_command, self.settings.xhs_downloader_command, self.settings.gallery_dl_binary, self.settings.gallery_dl_config, self.settings.source_timeout_seconds, self.settings.douyin_source_command, self.settings.douyin_source_timeout_seconds, self.settings.gallery_dl_cookies_from_browser, self.settings.source_failure_cooldown_seconds, self.settings.source_verification_path, self.settings.source_verification_ttl_seconds, self.settings.gallery_dl_cookies_file, getattr(self.settings, "douyin_media_crawler_fallback", False))
        self.creators = CreatorImageService(
            self.settings, self.sources, self.downloader, self.reranker, self.vision, self.object_detector,
            bilibili=getattr(self.adapters.get(Platform.BILIBILI), "api", None),
            weibo=WeiboApi(self.client, self.settings.weibo_cookie, self.settings.native_api_timeout_seconds),
        )

    async def close(self) -> None:
        if self.vision:
            await self.vision.close()
        if self.client:
            await self.client.aclose()
            self.client = None

    async def search(self, request: SearchRequest) -> dict:
        await self.start()
        intent = parse_intent(request.query)
        webpage = request.platforms == [Platform.OTHER] or intent.identifier_platform == "other"
        if webpage:
            request = request.model_copy(update={"platforms": [Platform.OTHER]})
            if not intent.url:
                raise ValueError("其他平台需要输入完整的 http(s) 网页网址")
            if request.media_type == "videos":
                raise ValueError("其他平台目前支持提取网页图片，请选择图片")
        if intent.identifier_scope == "creator" and not webpage:
            return await self.fetch_creator(CreatorFetchRequest(
                platform=Platform(intent.identifier_platform), creator_id=intent.identifier,
                max_images=request.max_results, min_width=request.min_width, min_height=request.min_height,
                safe_mode=request.safe_mode, download=False,
            ))
        if intent.identifier_scope == "creator_name" and not webpage:
            return await self.fetch_creator(CreatorFetchRequest(
                platform=Platform(intent.identifier_platform), creator_name=intent.identifier,
                max_images=request.max_results, min_width=request.min_width, min_height=request.min_height,
                safe_mode=request.safe_mode, download=False,
            ))
        if intent.is_keyword:
            self.vision.start_loading()
        try:
            return await asyncio.wait_for(
                self._search(request),
                timeout=max(1, self.settings.search_timeout_seconds),
            )
        except asyncio.TimeoutError:
            intent = parse_intent(request.query)
            platforms = self._target_platforms(request, intent.identifier_platform)
            timeout = self.settings.search_timeout_seconds
            return {
                "query": request.query,
                "intent": {
                    "tokens": intent.tokens,
                    "negative_tokens": intent.negative_tokens,
                    "identifier": intent.identifier,
                    "identifier_platform": intent.identifier_platform,
                    "identifier_scope": intent.identifier_scope,
                    "orientation": intent.orientation,
                    "quality_preference": intent.quality_preference,
                    "exclude_watermark": intent.exclude_watermark,
                    "exclude_text_overlay": intent.exclude_text_overlay,
                },
                "items": [],
                "platforms": {
                    platform.value: {
                        "count": 0,
                        "error": {
                            "code": "search_timeout",
                            "message": f"Search stopped after {timeout}s; a platform source did not finish in time",
                        },
                        "status": next(item for item in self.statuses() if item["platform"] == platform.value),
                    }
                    for platform in platforms
                },
                "retrieval": {
                    "mode": request.retrieval_mode,
                    "sources": self.sources.statuses(),
                    "provider": self.discovery.status if request.retrieval_mode in ("discovery", "hybrid") else None,
                    "semantic": self.reranker.status,
                    "vision": self.vision.status,
                },
                "cached": False,
            }

    async def _search(self, request: SearchRequest) -> dict:
        intent = parse_intent(request.query)
        platforms = self._target_platforms(request, intent.identifier_platform)
        # Bump the namespace when relevance rules change so old low-quality
        # keyword results are never served from the persistent cache.
        key = self.cache.key("search-v3-relevance-gated", request.model_dump(mode="json"), intent.normalized) if request.use_cache else None
        if key and (cached := self.cache.get(key)) is not None:
            return await self._refresh_cached_status(cached, platforms, request, key)

        async def search_one(platform: Platform):
            adapter = self.adapters[platform]
            requested_total = self._requested_media_total(request)
            source_limit = 100 if intent.identifier_scope == "post" else requested_total * 2
            candidates: list[ImageCandidate] = []
            errors: list[dict[str, str]] = []

            async def collect(label: str, operation):
                try:
                    found = await operation
                    candidates.extend(found)
                    if found and label in ("platform", "browser"):
                        self._verified_adapters.add(platform)
                except AdapterUnavailable as exc:
                    if label in ("platform", "browser"):
                        self._verified_adapters.discard(platform)
                    errors.append({"code": "unavailable", "message": f"{label}: {exc}"})
                except (AdapterError, DiscoveryError, SourceError, SourceUnavailable) as exc:
                    if label in ("platform", "browser"):
                        self._verified_adapters.discard(platform)
                    errors.append({"code": "adapter_error", "message": f"{label}: {exc}"})
                except Exception as exc:
                    if label in ("platform", "browser"):
                        self._verified_adapters.discard(platform)
                    errors.append({"code": "unexpected_error", "message": f"{label}: {exc}"})

            if platform == Platform.OTHER:
                await collect("webpage", adapter.search(intent, requested_total, request.safe_mode))
                if not candidates and errors:
                    first = errors[0]
                    return platform, [], {"code": first["code"], "message": first["message"]}
                return platform, candidates, None

            if request.retrieval_mode in ("discovery", "hybrid"):
                await collect("discovery", self.discovery.search(platform, intent, requested_total * 2))
            if request.retrieval_mode in ("sources", "hybrid"):
                if platform in (Platform.BILIBILI, Platform.WEIBO):
                    media_crawler = getattr(self.sources, "media_crawler", None)
                    if platform == Platform.WEIBO and getattr(media_crawler, "command_template", None):
                        # Prefer an already configured authenticated source;
                        # the public mobile endpoint is frequently rate limited.
                        await collect("sources", self.sources.search(platform, intent, source_limit))
                        if not candidates:
                            await collect("platform", adapter.search(intent, requested_total * 2, request.safe_mode))
                    else:
                        # Bilibili's bounded public API is the recommended source.
                        await collect("platform", adapter.search(intent, requested_total * 2, request.safe_mode))
                else:
                    await collect("sources", self.sources.search(platform, intent, source_limit))
                if platform not in (Platform.BILIBILI, Platform.WEIBO) and request.retrieval_mode == "sources" and getattr(adapter.status, "mode", "") == "browser-fallback":
                    await collect("browser", adapter.search(intent, requested_total, request.safe_mode))
            if request.retrieval_mode == "platform" or (request.retrieval_mode == "hybrid" and platform not in (Platform.BILIBILI, Platform.WEIBO)):
                await collect("platform", adapter.search(intent, requested_total, request.safe_mode))
            elif request.retrieval_mode == "sources" and platform in (Platform.X, Platform.INSTAGRAM) and adapter.status.configured and not candidates:
                # Official X/Instagram APIs are a usable download source when
                # the user supplied a token, even if gallery-dl is not set up.
                await collect("platform", adapter.search(intent, requested_total, request.safe_mode))

            # In hybrid mode, preserve useful candidates and expose partial
            # failures as warnings. An error is fatal only when every channel
            # failed to produce a candidate.
            if not candidates and errors:
                first = errors[0]
                message = "; ".join(error["message"] for error in errors)
                return platform, [], {"code": first["code"], "message": message}
            warning = None
            if errors:
                warning = {"code": "partial_error", "message": "; ".join(error["message"] for error in errors)}
            return platform, candidates, warning

        async def search_one_with_deadline(platform: Platform):
            try:
                return await asyncio.wait_for(
                    search_one(platform),
                    timeout=max(1, self.settings.platform_timeout_seconds),
                )
            except asyncio.TimeoutError:
                return platform, [], {
                    "code": "platform_timeout",
                    "message": f"{platform.value} search stopped after {self.settings.platform_timeout_seconds}s; other platforms may still return results",
                }

        results = await asyncio.gather(*(search_one_with_deadline(platform) for platform in platforms))
        all_candidates: list[ImageCandidate] = []
        platform_status: dict[str, dict] = {}
        effective_status = {item["platform"]: item for item in self.statuses()}
        for platform, candidates, error in results:
            if request.media_type == "videos":
                candidates = [item for item in candidates if item.media_type == "video"]
            elif request.media_type == "images":
                candidates = [item for item in candidates if item.media_type == "image"]
            all_candidates.extend(candidates)
            status = effective_status[platform.value]
            platform_status[platform.value] = {"count": len(candidates), "error": error, "status": status}
        semantic_applied = False
        semantic_deferred = False
        vision_deferred = False
        requested_total = self._requested_media_total(request)
        if intent.is_keyword:
            target_limit = requested_total
            ranked = rank_candidates(all_candidates, intent, min(target_limit * 3, 200), request.min_width, request.min_height, require_match=True)
            ranked = [item.model_copy(update={"score": round(item.score + self.feedback.bias(intent, item), 4)}) for item in ranked]
            ranked.sort(key=lambda item: item.score, reverse=True)
            if self.reranker.enabled:
                try:
                    ranked = await asyncio.wait_for(
                        self.reranker.rerank(ranked, intent, min(target_limit * 2, 100)),
                        timeout=max(0.05, float(getattr(self.settings, "semantic_timeout_seconds", 4))),
                    )
                    semantic_applied = bool(getattr(self.reranker, "_last_applied", False))
                except asyncio.TimeoutError:
                    semantic_deferred = True
                    ranked = ranked[:min(target_limit * 2, 100)]
            else:
                ranked = ranked[:min(target_limit * 2, 100)]
            # Starting CLIP is intentionally asynchronous. A cold model can
            # take longer than the useful request budget, so only use it when
            # the worker is already ready; later requests get visual ranking
            # without paying the cold-start penalty.
            if self.vision.status.get("enabled"):
                try:
                    # Fetching and classifying hundreds of remote images can
                    # exceed the request budget.  Visually rerank the lexical
                    # shortlist, then keep the remaining candidates behind it
                    # so the top results are actually on-topic.
                    vision_pool = ranked[: min(32, len(ranked))]
                    vision_ranked = await asyncio.wait_for(
                        self.vision.rerank(vision_pool, intent, min(target_limit, len(vision_pool))),
                        timeout=max(1, self.settings.vision_timeout_seconds),
                    )
                    ranked = vision_ranked + ranked[len(vision_pool):]
                except asyncio.TimeoutError:
                    self.vision.mark_timeout(self.settings.vision_timeout_seconds)
                    ranked = ranked[:target_limit]
            else:
                vision_deferred = bool(self.vision.status.get("loading"))
                ranked = ranked[:target_limit]
        else:
            ranked = self._direct_items(all_candidates, requested_total, request.min_width, request.min_height)
        ranked = self._apply_media_limits(ranked, request)
        # Report the same count the caller receives in ``items``.  The source
        # adapters intentionally fetch a larger shortlist for ranking, so the
        # pre-ranking candidate count can otherwise exceed the requested total
        # and make the UI look as if it downloaded more files than requested.
        delivered_counts = {platform.value: 0 for platform in platforms}
        for item in ranked:
            delivered_counts[item.platform.value] = delivered_counts.get(item.platform.value, 0) + 1
        for platform_name, entry in platform_status.items():
            entry["count"] = delivered_counts.get(platform_name, 0)
        payload = {"query": request.query, "intent": {"tokens": intent.tokens, "negative_tokens": intent.negative_tokens, "identifier": intent.identifier, "identifier_platform": intent.identifier_platform, "identifier_scope": intent.identifier_scope, "orientation": intent.orientation, "quality_preference": intent.quality_preference, "exclude_watermark": intent.exclude_watermark, "exclude_text_overlay": intent.exclude_text_overlay}, "items": [item.model_dump(mode="json") for item in ranked], "platforms": platform_status, "retrieval": {"mode": request.retrieval_mode, "sources": self.sources.statuses(), "provider": self.discovery.status if request.retrieval_mode in ("discovery", "hybrid") else None, "semantic": self.reranker.status, "vision": self.vision.status}, "cached": False}
        payload["retrieval"]["semantic_applied"] = semantic_applied
        payload["retrieval"]["semantic_deferred"] = semantic_deferred
        payload["retrieval"]["vision_applied"] = intent.is_keyword and any("vision_similarity" in item.source_payload for item in ranked)
        payload["retrieval"]["vision_deferred"] = vision_deferred
        if key:
            self.cache.set(key, payload)
        return payload

    async def _refresh_cached_status(self, cached: dict, platforms: list[Platform], request: SearchRequest, key: str | None = None) -> dict:
        """Keep cached candidates while exposing current source/auth state.

        A cold keyword request may be cached before CLIP finishes loading. Once
        the worker is ready, rerank that cached candidate set in place instead
        of issuing another platform request or serving stale source ordering.
        """
        payload = {**cached, "cached": True}
        current_status = {item["platform"]: item for item in self.statuses()}
        platform_payload = dict(payload.get("platforms") or {})
        delivered_counts: dict[str, int] = {}
        for item in payload.get("items") or []:
            if isinstance(item, dict) and item.get("platform"):
                name = str(item["platform"])
                delivered_counts[name] = delivered_counts.get(name, 0) + 1
        for platform in platforms:
            entry = dict(platform_payload.get(platform.value) or {})
            entry["status"] = current_status.get(platform.value)
            entry["count"] = delivered_counts.get(platform.value, 0)
            platform_payload[platform.value] = entry
        payload["platforms"] = platform_payload

        retrieval = dict(payload.get("retrieval") or {})
        retrieval["sources"] = self.sources.statuses()
        retrieval["semantic"] = self.reranker.status
        retrieval["vision"] = self.vision.status
        if request.retrieval_mode in ("discovery", "hybrid"):
            retrieval["provider"] = self.discovery.status
        cached_intent = parse_intent(request.query)
        semantic_applied = bool(retrieval.get("semantic_applied"))
        semantic_deferred = bool(retrieval.get("semantic_deferred"))
        cache_refresh_applied = False
        if (
            cached_intent.is_keyword
            and self.reranker.enabled
            and not semantic_applied
            and getattr(self.reranker, "_model", None) is not None
        ):
            try:
                cached_items = [ImageCandidate.model_validate(item) for item in payload.get("items") or []]
                reranked = await asyncio.wait_for(
                    self.reranker.rerank(cached_items, cached_intent, request.max_results),
                    timeout=max(0.05, float(getattr(self.settings, "semantic_timeout_seconds", 4))),
                )
                payload["items"] = [item.model_dump(mode="json") for item in reranked]
                semantic_applied = bool(getattr(self.reranker, "_last_applied", False))
                semantic_deferred = False if semantic_applied else semantic_deferred
                cache_refresh_applied = cache_refresh_applied or semantic_applied
            except asyncio.TimeoutError:
                semantic_deferred = True
            except Exception:
                pass
        vision_applied = bool(retrieval.get("vision_applied"))
        vision_deferred = bool(retrieval.get("vision_deferred"))
        if cached_intent.is_keyword and self.vision.status.get("enabled") and not vision_applied:
            try:
                cached_items = [ImageCandidate.model_validate(item) for item in payload.get("items") or []]
                reranked = await asyncio.wait_for(
                    self.vision.rerank(cached_items, cached_intent, request.max_results),
                    timeout=max(1, self.settings.vision_timeout_seconds),
                )
                payload["items"] = [item.model_dump(mode="json") for item in reranked]
                vision_applied = any("vision_similarity" in item.source_payload for item in reranked)
                vision_deferred = False if vision_applied else vision_deferred
                cache_refresh_applied = cache_refresh_applied or vision_applied
            except asyncio.TimeoutError:
                self.vision.mark_timeout(self.settings.vision_timeout_seconds)
            except Exception:
                # Keep a valid cached response if an optional visual refresh
                # fails; the source-ranked items are still useful.
                pass
        retrieval["vision_applied"] = vision_applied
        retrieval["vision_deferred"] = vision_deferred
        retrieval["semantic_applied"] = semantic_applied
        retrieval["semantic_deferred"] = semantic_deferred
        retrieval["semantic"] = self.reranker.status
        retrieval["vision"] = self.vision.status
        payload["retrieval"] = retrieval
        if key and cache_refresh_applied:
            self.cache.set(key, payload)
        return payload

    @staticmethod
    def _target_platforms(request: SearchRequest, identifier_platform: str | None) -> list[Platform]:
        if request.platforms:
            return request.platforms
        if identifier_platform:
            return [Platform(identifier_platform)]
        return [platform for platform in Platform if platform != Platform.OTHER]

    async def inspect(self, platform: Platform, item_id: str) -> dict:
        await self.start()
        intent = parse_intent(f"{platform.value}:{item_id}")
        candidates: list[ImageCandidate] = []
        errors: list[str] = []
        if platform not in (Platform.BILIBILI, Platform.WEIBO):
            try:
                candidates.extend(await self.sources.search(platform, intent, 30))
            except SourceError as exc:
                errors.append(str(exc))
        try:
            candidates.extend(await self.adapters[platform].inspect(item_id))
        except AdapterError as exc:
            errors.append(str(exc))
        ranked = self._direct_items(candidates, 30)
        return {"platform": platform.value, "item_id": item_id, "items": [item.model_dump(mode="json") for item in ranked], "errors": errors}

    @staticmethod
    def _requested_media_total(request: SearchRequest) -> int:
        image_limit = request.image_limit or request.max_results
        video_limit = request.video_limit if request.video_limit is not None else request.max_results
        if request.media_type == "videos":
            return video_limit
        if request.media_type == "all":
            return image_limit + video_limit
        return image_limit

    @staticmethod
    def _direct_items(items: list[ImageCandidate], limit: int, min_width: int = 0, min_height: int = 0) -> list[ImageCandidate]:
        result = []
        seen = set()
        for item in items:
            if item.image_url in seen or (item.width and item.width < min_width) or (item.height and item.height < min_height):
                continue
            seen.add(item.image_url)
            result.append(item.model_copy(update={"score": 0, "matched_terms": []}))
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _apply_media_limits(items: list[ImageCandidate], request: SearchRequest) -> list[ImageCandidate]:
        image_limit = request.image_limit or request.max_results
        video_limit = request.video_limit if request.video_limit is not None else request.max_results
        per_post_limit = request.per_post_limit
        counts = {"image": 0, "video": 0}
        posts: dict[str, int] = {}
        selected: list[ImageCandidate] = []
        for item in items:
            if request.media_type == "images" and item.media_type != "image":
                continue
            if request.media_type == "videos" and item.media_type != "video":
                continue
            post = item.post_id or item.id
            if per_post_limit is not None and posts.get(post, 0) >= per_post_limit:
                continue
            limit = image_limit if item.media_type == "image" else video_limit
            if counts[item.media_type] >= limit:
                continue
            counts[item.media_type] += 1
            posts[post] = posts.get(post, 0) + 1
            selected.append(item)
            if request.media_type != "all" and len(selected) >= request.max_results:
                break
            if request.media_type == "all" and counts["image"] >= image_limit and counts["video"] >= video_limit:
                break
        return selected

    async def fetch_creator(self, request: CreatorFetchRequest) -> dict:
        await self.start()
        return await self.creators.fetch(request)

    def record_feedback(self, query: str, platform: Platform, candidate_id: str, accepted: bool, image_url: str = "") -> dict:
        intent = parse_intent(query)
        candidate = ImageCandidate(id=candidate_id, platform=platform, image_url=image_url or "https://feedback.invalid/" + candidate_id)
        self.feedback.record(intent, candidate, accepted)
        if self.cache:
            self.cache.clear()
        return {"recorded": True, "query": query, "platform": platform.value, "candidate_id": candidate_id, "accepted": accepted}

    async def download(self, items: list[ImageCandidate], output_dir: str | None, max_concurrency: int, min_width: int, min_height: int) -> list[DownloadRecord]:
        await self.start()
        return await self.downloader.download_many(items, self.settings.ensure_output_dir(output_dir), max_concurrency, min_width, min_height)

    def statuses(self) -> list[dict]:
        """Return the effective availability of every platform.

        The legacy adapters (cookies, API tokens and JSON gateways) are only
        one retrieval path.  Recommended source projects are another path and
        must count toward platform availability; otherwise ``--check`` reports
        a misleading ``configured=false`` even when source retrieval works.
        """
        if not self.adapters:
            # Build lightweight status objects without opening sockets.
            with httpx.Client() as client:
                adapters = build_adapters(self.settings, client)  # type: ignore[arg-type]
                adapter_statuses = {platform: adapter.status for platform, adapter in adapters.items()}
        else:
            adapter_statuses = {platform: adapter.status for platform, adapter in self.adapters.items()}

        source_statuses = self.source_statuses()
        configured_sources: dict[str, list[str]] = {platform.value: [] for platform in Platform}
        verified_sources: dict[str, list[str]] = {platform.value: [] for platform in Platform}
        ready_sources: dict[str, list[str]] = {platform.value: [] for platform in Platform}
        source_evidence: dict[str, list[dict]] = {platform.value: [] for platform in Platform}
        for source in source_statuses:
            if not source.get("configured"):
                continue
            name = str(source.get("name") or "source-project")
            for platform in source.get("platforms", ()):
                if platform in configured_sources:
                    configured_sources[platform].append(name)
                    verified_platforms = source.get("verified_platforms")
                    if source.get("verified") and (
                        not isinstance(verified_platforms, (list, tuple, set))
                        or platform in verified_platforms
                    ):
                        verified_sources[platform].append(name)
                    ready_platforms = source.get("ready_platforms")
                    if isinstance(ready_platforms, (list, tuple, set)) and platform in ready_platforms:
                        ready_sources[platform].append(name)
                    platform_status = source.get("platform_status")
                    if isinstance(platform_status, dict) and isinstance(platform_status.get(platform), dict):
                        source_evidence[platform].append({"source": name, **platform_status[platform]})

        result: list[dict] = []
        for platform in Platform:
            adapter_status = adapter_statuses[platform]
            source_names = configured_sources[platform.value]
            source_verified = bool(verified_sources[platform.value])
            source_ready = bool(ready_sources[platform.value])
            source_configured = bool(source_names)
            legacy_configured = bool(adapter_status.configured)
            adapter_verified = platform in self._verified_adapters
            if source_configured:
                mode = "source-project"
                detail = "Recommended source configured; legacy API adapter is optional"
                if legacy_configured:
                    detail = "Recommended source and legacy API adapter configured"
            else:
                mode = adapter_status.mode
                detail = adapter_status.detail
            result.append({
                "platform": platform.value,
                "configured": source_configured or legacy_configured,
                "mode": mode,
                "detail": detail,
                "source_configured": source_configured,
                "source_projects": source_names,
                "verified": source_verified or adapter_verified,
                "ready": source_ready or adapter_verified,
                "verified_sources": verified_sources[platform.value],
                "ready_sources": ready_sources[platform.value],
                "verification": source_evidence[platform.value],
                "legacy_adapter_configured": legacy_configured,
                "legacy_adapter_verified": adapter_verified,
            })
        return result

    def source_statuses(self) -> list[dict]:
        if self.sources is None:
            self.sources = SourceHub(self.settings.media_crawler_command, self.settings.xhs_downloader_command, self.settings.gallery_dl_binary, self.settings.gallery_dl_config, self.settings.source_timeout_seconds, self.settings.douyin_source_command, self.settings.douyin_source_timeout_seconds, self.settings.gallery_dl_cookies_from_browser, self.settings.source_failure_cooldown_seconds, self.settings.source_verification_path, self.settings.source_verification_ttl_seconds, self.settings.gallery_dl_cookies_file, getattr(self.settings, "douyin_media_crawler_fallback", False))
        return self.sources.statuses()


@asynccontextmanager
async def service_lifespan(service: SocialImageService):
    await service.start()
    try:
        yield
    finally:
        await service.close()
