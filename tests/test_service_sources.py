import asyncio

from social_image_mcp.config import Settings
from social_image_mcp.models import ImageCandidate, Platform, SearchRequest
from social_image_mcp.service import SocialImageService


class FakeSources:
    def statuses(self):
        return [{"name": "fake-source", "configured": True, "mode": "test", "detail": "", "platforms": ["xhs"]}]

    async def search(self, platform, intent, limit, *, request=None):
        return [ImageCandidate(id="n1", platform=platform, image_url="https://cdn.test/n1.jpg", title="极简咖啡店室内", width=1600, height=900)]


class FailingSources(FakeSources):
    async def search(self, platform, intent, limit, *, request=None):
        raise RuntimeError("source unavailable")


def test_service_uses_recommended_source_before_download(tmp_path):
    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        await service.start()
        service.sources = FakeSources()
        result = await service.search(SearchRequest(query="咖啡店 横图", platforms=[Platform.XHS], use_cache=False, retrieval_mode="sources"))
        assert result["items"][0]["id"] == "n1"
        assert result["retrieval"]["mode"] == "sources"
        assert result["retrieval"]["sources"][0]["name"] == "fake-source"
        assert result["platforms"]["xhs"]["status"]["configured"] is True
        assert result["platforms"]["xhs"]["status"]["mode"] == "source-project"
        await service.close()

    asyncio.run(run())


def test_hybrid_keeps_discovery_candidates_when_source_fails(tmp_path):
    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        await service.start()
        service.sources = FailingSources()
        service.discovery.search = lambda platform, intent, limit: _discovery_candidate(platform)
        result = await service.search(SearchRequest(query="咖啡店", platforms=[Platform.XHS], use_cache=False, retrieval_mode="hybrid"))
        assert result["items"][0]["id"] == "discovery"
        assert result["platforms"]["xhs"]["error"]["code"] == "partial_error"
        await service.close()

    asyncio.run(run())


async def _discovery_candidate(platform):
    return [ImageCandidate(id="discovery", platform=platform, image_url="https://cdn.test/discovery.jpg", title="咖啡店")]


def test_mixed_share_request_keeps_video_after_a_large_gallery(tmp_path):
    class MixedSources(FakeSources):
        async def search(self, platform, intent, limit, *, request=None):
            assert request.media_type == "all"
            assert request.max_posts == 17
            images = [ImageCandidate(id=f"photo-{i}", platform=platform, image_url=f"https://cdn.test/{i}.jpg")
                      for i in range(8)]
            return images + [ImageCandidate(id="clip", platform=platform, image_url="https://cdn.test/play", media_type="video")]

    async def run():
        service = SocialImageService(Settings(cache_path=str(tmp_path / "cache.sqlite3"), vision_model=None))
        await service.start()
        try:
            service.sources = MixedSources()
            result = await service.search(SearchRequest(
                query="https://v.douyin.com/share/", media_type="all", max_results=3,
                image_limit=2, video_limit=1, max_posts=17, use_cache=False,
            ))
            assert [item["id"] for item in result["items"]] == ["photo-0", "photo-1", "clip"]
        finally:
            await service.close()

    asyncio.run(run())
