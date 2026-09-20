import asyncio

from social_image_mcp.config import Settings
from social_image_mcp.models import ImageCandidate, Platform, SearchRequest
from social_image_mcp.service import SocialImageService


def test_webpage_search_keeps_video_quota_and_forwards_detail_limit(tmp_path):
    from social_image_mcp.webpage import PageMedia
    from social_image_mcp.adapters.base import AdapterStatus

    class WebAdapter:
        status = AdapterStatus(Platform.OTHER, True, "webpage", "test")

        async def search_media(self, request):
            assert request.max_posts == 2
            assert request.media_type == "all"
            return PageMedia([
                ImageCandidate(id="photo", platform=Platform.OTHER, image_url="https://cdn.test/a.jpg", post_id="post"),
                ImageCandidate(id="video", platform=Platform.OTHER, image_url="https://cdn.test/v.mp4", media_type="video", post_id="post"),
            ], warnings=["partial detail page"])

    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        await service.start()
        try:
            service.adapters[Platform.OTHER] = WebAdapter()
            result = await service.search(SearchRequest(query="https://example.org/", media_type="all", image_limit=1, video_limit=1, per_post_limit=1, max_posts=2, use_cache=False))
            assert [item["media_type"] for item in result["items"]] == ["image", "video"]
            assert result["platforms"]["other"]["count"] == 2
            assert result["platforms"]["other"]["error"]["message"] == "partial detail page"
        finally:
            await service.close()

    asyncio.run(run())


def test_unconfigured_platform_returns_explicit_status(tmp_path):
    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        result = await service.search(SearchRequest(query="咖啡店", platforms=[Platform.XHS], use_cache=False, retrieval_mode="platform"))
        assert result["items"] == []
        assert result["platforms"]["xhs"]["error"]["code"] == "unavailable"
        await service.close()

    asyncio.run(run())


def test_search_has_server_side_deadline(tmp_path):
    class HangingSources:
        def statuses(self):
            return [{
                "name": "hanging-source",
                "configured": True,
                "verified": False,
                "verified_platforms": [],
                "mode": "test",
                "detail": "",
                "platforms": ["douyin"],
            }]

        async def search(self, platform, intent, limit, *, request=None):
            await asyncio.sleep(10)
            return []

    async def run():
        service = SocialImageService(Settings(
            cache_path=str(tmp_path / "cache.sqlite3"),
            vision_model=None,
            search_timeout_seconds=1,
        ))
        await service.start()
        service.sources = HangingSources()
        started = asyncio.get_running_loop().time()
        result = await service.search(SearchRequest(
            query="咖啡店",
            platforms=[Platform.DOUYIN],
            use_cache=False,
            retrieval_mode="sources",
        ))
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 3
        assert result["items"] == []
        assert result["platforms"]["douyin"]["error"]["code"] == "search_timeout"
        assert result["intent"]["tokens"]
        assert "semantic" in result["retrieval"]
        assert "vision" in result["retrieval"]
        await service.close()

    asyncio.run(run())


def test_platform_timeout_keeps_other_platform_candidates(tmp_path):
    class MixedSources:
        def statuses(self):
            return [{
                "name": "mixed-source",
                "configured": True,
                "verified": False,
                "verified_platforms": [],
                "mode": "test",
                "detail": "",
                "platforms": ["douyin", "xhs"],
            }]

        async def search(self, platform, intent, limit, *, request=None):
            if platform is Platform.DOUYIN:
                await asyncio.sleep(10)
            return [ImageCandidate(
                id="fast-xhs",
                platform=platform,
                image_url="https://cdn.test/fast.jpg",
                title="咖啡店室内",
                width=1200,
                height=800,
            )]

    async def run():
        service = SocialImageService(Settings(
            cache_path=str(tmp_path / "cache.sqlite3"),
            vision_model=None,
            platform_timeout_seconds=1,
            search_timeout_seconds=3,
        ))
        await service.start()
        service.sources = MixedSources()
        result = await service.search(SearchRequest(
            query="咖啡店室内",
            platforms=[Platform.DOUYIN, Platform.XHS],
            use_cache=False,
            retrieval_mode="sources",
        ))
        assert result["items"][0]["id"] == "fast-xhs"
        assert result["platforms"]["douyin"]["error"]["code"] == "platform_timeout"
        assert result["platforms"]["xhs"]["count"] == 1
        await service.close()

    asyncio.run(run())


def test_platform_specific_id_only_queries_its_platform(tmp_path):
    class RecordingSources:
        def __init__(self):
            self.called = []

        def statuses(self):
            return [{
                "name": "recording-source",
                "configured": True,
                "verified": False,
                "verified_platforms": [],
                "mode": "test",
                "detail": "",
                "platforms": [platform.value for platform in Platform],
            }]

        async def search(self, platform, intent, limit, *, request=None):
            self.called.append(platform)
            return [ImageCandidate(
                id="123456789:1",
                platform=platform,
                image_url="https://cdn.test/exact.jpg",
            )]

    async def run():
        service = SocialImageService(Settings(
            cache_path=str(tmp_path / "cache.sqlite3"),
            vision_model=None,
        ))
        await service.start()
        sources = RecordingSources()
        service.sources = sources
        result = await service.search(SearchRequest(
            query="douyin:123456789",
            platforms=None,
            use_cache=False,
            retrieval_mode="sources",
        ))
        assert sources.called == [Platform.DOUYIN]
        assert set(result["platforms"]) == {"douyin"}
        await service.close()

    asyncio.run(run())


def test_platform_counts_match_delivered_items_after_limits(tmp_path):
    class Sources:
        def statuses(self):
            return [{"name": "fake", "configured": True, "verified": True,
                     "verified_platforms": ["douyin"], "ready_platforms": ["douyin"],
                     "mode": "test", "detail": "", "platforms": ["douyin"]}]

        async def search(self, platform, intent, limit, *, request=None):
            return [
                ImageCandidate(id="one", platform=platform, image_url="https://cdn.test/one.jpg"),
                ImageCandidate(id="two", platform=platform, image_url="https://cdn.test/two.jpg"),
            ]

    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        await service.start()
        service.sources = Sources()
        result = await service.search(SearchRequest(
            query="https://v.douyin.com/example/", platforms=[Platform.DOUYIN],
            max_results=1, image_limit=1, use_cache=False, retrieval_mode="sources",
        ))
        assert len(result["items"]) == 1
        assert result["platforms"]["douyin"]["count"] == 1
        await service.close()

    asyncio.run(run())


def test_video_and_all_limits_drive_source_fetch_budget(tmp_path):
    class Sources:
        def __init__(self):
            self.limits = []

        def statuses(self):
            return [{"name": "fake", "configured": True, "verified": True,
                     "verified_platforms": ["douyin"], "ready_platforms": ["douyin"],
                     "mode": "test", "detail": "", "platforms": ["douyin"]}]

        async def search(self, platform, intent, limit, *, request=None):
            self.limits.append(limit)
            return []

    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        await service.start()
        sources = Sources()
        service.sources = sources
        await service.search(SearchRequest(
            query="猫 视频", platforms=[Platform.DOUYIN], max_results=3,
            image_limit=3, video_limit=7, media_type="videos", use_cache=False, retrieval_mode="sources",
        ))
        await service.search(SearchRequest(
            query="猫 图片和视频", platforms=[Platform.DOUYIN], max_results=3,
            image_limit=3, video_limit=7, media_type="all", use_cache=False, retrieval_mode="sources",
        ))
        assert sources.limits == [14, 20]
        await service.close()

    asyncio.run(run())


def test_cache_hit_refreshes_current_source_verification_status(tmp_path):
    class DynamicSources:
        def __init__(self):
            self.ready = True
            self.calls = 0

        def statuses(self):
            return [{
                "name": "dynamic-source",
                "configured": True,
                "verified": True,
                "verified_platforms": ["xhs"],
                "ready_platforms": ["xhs"] if self.ready else [],
                "mode": "test",
                "detail": "current source state",
                "platforms": ["xhs"],
                "platform_status": {
                    "xhs": {
                        "verified": True,
                        "ready": self.ready,
                        "last_result": "success" if self.ready else "failure",
                        "last_error": None if self.ready else "login expired",
                    }
                },
            }]

        async def search(self, platform, intent, limit, *, request=None):
            self.calls += 1
            return [ImageCandidate(
                id="cached-xhs",
                platform=platform,
                image_url="https://cdn.test/cached.jpg",
                title="咖啡店室内",
            )]

    async def run():
        service = SocialImageService(Settings(
            cache_path=str(tmp_path / "cache.sqlite3"),
            vision_model=None,
        ))
        await service.start()
        sources = DynamicSources()
        service.sources = sources
        request = SearchRequest(
            query="咖啡店室内",
            platforms=[Platform.XHS],
            use_cache=True,
            retrieval_mode="sources",
        )

        first = await service.search(request)
        assert first["cached"] is False
        assert first["platforms"]["xhs"]["status"]["ready"] is True

        sources.ready = False
        second = await service.search(request)
        assert second["cached"] is True
        assert sources.calls == 1
        assert second["platforms"]["xhs"]["status"]["ready"] is False
        assert second["retrieval"]["sources"][0]["platform_status"]["xhs"]["last_error"] == "login expired"
        await service.close()

    asyncio.run(run())


def test_keyword_search_does_not_wait_for_cold_visual_worker(tmp_path):
    class Sources:
        calls = 0

        def statuses(self):
            return [{
                "name": "fake-source", "configured": True, "verified": True,
                "verified_platforms": ["xhs"], "ready_platforms": ["xhs"],
                "mode": "test", "detail": "", "platforms": ["xhs"],
            }]

        async def search(self, platform, intent, limit, *, request=None):
            self.calls += 1
            return [ImageCandidate(
                id="cold-visual", platform=platform,
                image_url="https://cdn.test/cold.jpg", title="咖啡店室内",
            )]

    class DeferredVision:
        enabled = True
        model_name = "local-test"

        def __init__(self):
            self.ready = False

        @property
        def status(self):
            return {"enabled": self.ready, "loading": not self.ready, "model": self.model_name, "error": None}

        def start_loading(self):
            return None

        async def rerank(self, *args, **kwargs):
            if not self.ready:
                raise AssertionError("a cold visual worker must not block this request")
            items = args[0]
            return [item.model_copy(update={"source_payload": {**item.source_payload, "vision_similarity": 0.9}}) for item in items]

        async def close(self):
            return None

    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        await service.start()
        service.sources = Sources()
        sources = service.sources
        vision = DeferredVision()
        service.vision = vision
        started = asyncio.get_running_loop().time()
        result = await service.search(SearchRequest(
            query="咖啡店室内", platforms=[Platform.XHS], use_cache=True,
            retrieval_mode="sources", max_results=1,
        ))
        assert asyncio.get_running_loop().time() - started < 0.5
        assert result["items"][0]["id"] == "cold-visual"
        assert result["retrieval"]["vision_deferred"] is True
        vision.ready = True
        refreshed = await service.search(SearchRequest(
            query="咖啡店室内", platforms=[Platform.XHS], use_cache=True,
            retrieval_mode="sources", max_results=1,
        ))
        assert sources.calls == 1
        assert refreshed["cached"] is True
        assert refreshed["retrieval"]["vision_applied"] is True
        await service.close()

    asyncio.run(run())


def test_keyword_search_bounds_cold_semantic_reranker(tmp_path):
    class Sources:
        def statuses(self):
            return [{"name": "fake-source", "configured": True, "verified": True,
                     "verified_platforms": ["xhs"], "ready_platforms": ["xhs"],
                     "mode": "test", "detail": "", "platforms": ["xhs"]}]

        async def search(self, platform, intent, limit, *, request=None):
            return [ImageCandidate(id="semantic-cold", platform=platform,
                                   image_url="https://cdn.test/semantic.jpg", title="咖啡店")]

    class SlowSemantic:
        enabled = True
        _model = None

        @property
        def status(self):
            return {"enabled": False, "model": "local-test", "error": None}

        async def rerank(self, *args, **kwargs):
            await asyncio.sleep(1)
            return args[0]

    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None,
                                              semantic_model="local-test", semantic_timeout_seconds=0.05))
        await service.start()
        service.sources = Sources()
        service.reranker = SlowSemantic()
        started = asyncio.get_running_loop().time()
        result = await service.search(SearchRequest(query="咖啡店", platforms=[Platform.XHS], use_cache=False,
                                                    retrieval_mode="sources", max_results=1))
        assert asyncio.get_running_loop().time() - started < 0.5
        assert result["items"][0]["id"] == "semantic-cold"
        assert result["retrieval"]["semantic_applied"] is False
        assert result["retrieval"]["semantic_deferred"] is True
        await service.close()

    asyncio.run(run())
