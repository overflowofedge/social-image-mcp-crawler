import asyncio
import io
import time
import httpx
import pytest
from PIL import Image

from social_image_mcp.creator_protocol import CreatorCollector, creator_target, douyin_identity, pack_cursor, unpack_cursor
from social_image_mcp.creator_service import CreatorImageService
from social_image_mcp.downloader import ImageDownloader
from social_image_mcp.models import CreatorFetchRequest, CreatorIdentity, DownloadRecord, ImageCandidate, Platform
from social_image_mcp.sources import CreatorSourceResult
from social_image_mcp.weibo import WeiboApi, WeiboError


def _identity() -> CreatorIdentity:
    return CreatorIdentity(platform=Platform.DOUYIN, requested_id="Gracebb0722", canonical_id="sec-1", name="放学小野猪", source="test", matched_by="exact_account_id")


def test_creator_target_requires_platform_specific_ids():
    assert creator_target("douyin", "douyin-user:Gracebb0722") == "Gracebb0722"
    assert creator_target("weibo", "https://weibo.com/u/5756404150") == "5756404150"
    with pytest.raises(ValueError):
        creator_target("weibo", "weibo-user:alice")


def test_creator_name_request_is_supported():
    request = CreatorFetchRequest(platform=Platform.DOUYIN, creator_name="放学小野猪", media_type="videos", download=False)
    assert request.creator_name == "放学小野猪"
    assert request.media_type == "videos"
    chinese_id = CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="放学小野猪", download=False)
    assert chinese_id.creator_name == "放学小野猪" and chinese_id.creator_id is None


def test_different_creator_ids_run_concurrently(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        creator_timeout_seconds = 5

    async def run():
        service = CreatorImageService(Settings(), object(), object())

        async def fake_fetch(request):
            await asyncio.sleep(0.12)
            return {"items": [], "downloads": [], "error": None}

        service._fetch = fake_fetch
        requests = [
            CreatorFetchRequest(platform=Platform.WEIBO, creator_id="10001", download=False),
            CreatorFetchRequest(platform=Platform.WEIBO, creator_id="10002", download=False),
        ]
        started = time.perf_counter()
        await asyncio.gather(*(service.fetch(request) for request in requests))
        assert time.perf_counter() - started < 0.22

    asyncio.run(run())


def test_weibo_nickname_uses_authenticated_fallback_after_public_432(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5
        def ensure_output_dir(self, value=None):
            path = __import__("pathlib").Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path
    class Native:
        async def fetch_creator(self, request):
            raise WeiboError("weibo public API returned HTTP 432")
    class Sources:
        async def fetch_creator(self, request):
            assert request.creator_name == "凌云Tiger1"
            identity = CreatorIdentity(platform=Platform.WEIBO, requested_id=request.creator_name,
                                       canonical_id="5984743446", name=request.creator_name,
                                       profile_url="https://weibo.com/u/5984743446", source="media-crawler", matched_by="exact_account_name")
            item = ImageCandidate(id="post:1", platform=Platform.WEIBO,
                                  image_url="https://wx.test/photo.jpg", creator_id=identity.canonical_id,
                                  post_id="post", media_index=1)
            return CreatorSourceResult(identity=identity, items=[item], posts_fetched=1, post_ids=("post",), pages_fetched=1)
    async def run():
        service = CreatorImageService(Settings(), Sources(), object(), weibo=Native())
        return await service.fetch(CreatorFetchRequest(platform=Platform.WEIBO, creator_name="凌云Tiger1", download=False, resume=False))
    result = asyncio.run(run())
    assert result["error"] is None
    assert result["identity"]["canonical_id"] == "5984743446"
    assert result["items"][0]["post_id"] == "post"


def test_weibo_profile_to_image_and_video_files(tmp_path):
    image = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(image, format="PNG")
    def handler(request):
        if request.url.host == "wx.test":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=image.getvalue())
        if request.url.host == "video.test":
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video-data")
        container = request.url.params.get("containerid")
        if container == "1005055984743446":
            return httpx.Response(200, json={"ok": 1, "data": {"userInfo": {"idstr": "5984743446", "screen_name": "凌云Tiger1"}}})
        if container == "1076035984743446":
            return httpx.Response(200, json={"ok": 1, "data": {"cards": [
                {"mblog": {"id": "image-post", "user": {"idstr": "5984743446"}, "pics": [{"large": {"url": "https://wx.test/image.png"}}]}},
                {"mblog": {"id": "video-post", "user": {"idstr": "5984743446"}, "page_info": {"media_info": {"mp4_hd_mp4": "https://video.test/video.mp4"}}}},
            ], "cardlistInfo": {"since_id": 0}}})
        return httpx.Response(404)
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5
        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = CreatorImageService(Settings(), object(), ImageDownloader(client), weibo=WeiboApi(client))
            request = CreatorFetchRequest(platform=Platform.WEIBO, profile_url="https://weibo.com/u/5984743446",
                                          media_type="all", max_images=2, max_videos=2, output_dir=str(tmp_path / "out"), resume=False)
            return await service.fetch(request)
    result = asyncio.run(run())
    assert result["error"] is None
    assert {record["media_type"] for record in result["downloads"]} == {"image", "video"}
    assert all(record["status"] == "downloaded" for record in result["downloads"])
    assert {__import__("pathlib").Path(record["path"]).suffix for record in result["downloads"]} == {".png", ".mp4"}


def test_douyin_identity_resolves_exact_nickname():
    class Client:
        def search_users(self, value, count=20):
            return {"user_list": [{"user_info": {"sec_uid": "sec-1", "nickname": value}}]}
        def get_user_profile(self, sec_uid):
            return {"sec_uid": sec_uid, "nickname": "放学小野猪"}
    identity = douyin_identity(Client(), "放学小野猪", nickname=True)
    assert identity.canonical_id == "sec-1"
    assert identity.matched_by == "exact_account_name"


def test_douyin_identity_uses_first_confirmed_ranked_exact_nickname():
    class Client:
        def search_users(self, value, count=20):
            return {"user_list": [
                {"user_info": {"sec_uid": "sec-ranked", "nickname": value}},
                {"user_info": {"sec_uid": "sec-later", "nickname": value}},
            ]}

        def get_user_profile(self, sec_uid):
            return {"sec_uid": sec_uid, "nickname": "同名账号"}

    identity = douyin_identity(Client(), "同名账号", nickname=True)
    assert identity.canonical_id == "sec-ranked"
    assert identity.matched_by == "ranked_exact_account_name"


def test_creator_collector_hard_filters_author_and_keeps_media_order():
    identity = _identity()
    request = CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722", max_posts=2, max_images=10, download=False)
    collector = CreatorCollector(identity, request)
    rows = [
        {"aweme_id": "p1", "author": {"sec_uid": "sec-1"}, "desc": "one", "images": [{"url_list": ["https://img.test/1.jpg"]}, {"url_list": ["https://img.test/2.jpg"]}]},
        {"aweme_id": "foreign", "author": {"sec_uid": "other"}, "images": [{"url_list": ["https://img.test/foreign.jpg"]}]},
    ]
    assert collector.consume(rows, "1", True) is False
    result = collector.result()
    assert result["post_ids"] == ["p1"]
    assert [item["id"] for item in result["items"]] == ["p1:1", "p1:2"]
    assert result["rejected_posts"] == 1


def test_creator_cursor_round_trip():
    cursor = pack_cursor(18, 2)
    assert unpack_cursor(cursor) == ("18", 2)
    with pytest.raises(ValueError):
        unpack_cursor("bad")


def test_creator_service_does_not_call_semantic_or_vision(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5

        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

    candidate = ImageCandidate(id="p1:1", platform=Platform.DOUYIN, image_url="https://img.test/1.jpg", creator_id="sec-1", post_id="p1", media_index=1)

    class Sources:
        async def fetch_creator(self, request):
            return CreatorSourceResult(identity=_identity(), items=[candidate], posts_fetched=1, post_ids=("p1",), pages_fetched=1)

    class Downloader:
        async def download_many(self, *args, **kwargs):
            raise AssertionError("download should not run when download=false")

    async def run():
        service = CreatorImageService(Settings(), Sources(), Downloader())
        result = await service.fetch(CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722", max_images=1, download=False, output_dir=str(tmp_path / "out"), resume=False))
        assert result["error"] is None
        assert result["semantic_applied"] is False
        assert result["vision_applied"] is False
        assert result["items"][0]["creator_id"] == "sec-1"

    asyncio.run(run())


def test_creator_default_mode_does_not_run_object_filter(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5

        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

    candidate = ImageCandidate(id="p1", platform=Platform.DOUYIN, image_url="https://img.test/1.jpg", creator_id="sec-1", post_id="p1", media_index=1)

    class Sources:
        async def fetch_creator(self, request):
            return CreatorSourceResult(identity=_identity(), items=[candidate], posts_fetched=1, post_ids=("p1",), pages_fetched=1)

    class Detector:
        configured = True
        async def inspect(self, *args):
            raise AssertionError("object detector must not run in default account mode")

    async def run():
        service = CreatorImageService(Settings(), Sources(), object(), object_detector=Detector())
        result = await service.fetch(CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722", max_images=1, download=False, output_dir=str(tmp_path / "out"), resume=False))
        assert result["items"]
        assert result["semantic_requested"] is False
        assert result["object_detection_applied"] is False

    asyncio.run(run())


def test_creator_required_object_filter_fails_closed_without_detector(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5
        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir); path.mkdir(parents=True, exist_ok=True); return path

    candidate = ImageCandidate(id="p1", platform=Platform.DOUYIN, image_url="https://img.test/1.jpg", creator_id="sec-1", post_id="p1", media_index=1)
    class Sources:
        async def fetch_creator(self, request):
            return CreatorSourceResult(identity=_identity(), items=[candidate], posts_fetched=1, post_ids=("p1",), pages_fetched=1)

    async def run():
        service = CreatorImageService(Settings(), Sources(), object())
        result = await service.fetch(CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722", content_query="只要人物穿搭，排除风景", filter_mode="required", download=False, output_dir=str(tmp_path / "out"), resume=False))
        assert result["items"] == []
        assert result["error"]["code"] == "content_filter_required"
        assert result["object_detection_applied"] is False

    asyncio.run(run())


def test_creator_optional_object_filter_keeps_detector_decisions(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5
        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir); path.mkdir(parents=True, exist_ok=True); return path

    items = [
        ImageCandidate(id="person", platform=Platform.DOUYIN, image_url="https://img.test/person.jpg", creator_id="sec-1", post_id="p1", media_index=1),
        ImageCandidate(id="landscape", platform=Platform.DOUYIN, image_url="https://img.test/landscape.jpg", creator_id="sec-1", post_id="p2", media_index=1),
    ]
    class Sources:
        async def fetch_creator(self, request):
            return CreatorSourceResult(identity=_identity(), items=items, posts_fetched=2, post_ids=("p1", "p2"), pages_fetched=1)
    class Detector:
        configured = True
        async def inspect(self, values, spec, fail_closed=False):
            return values[:1], {"object_detection_applied": True, "object_detection_error": None, "object_decisions": {}, "object_rejected": 1, "object_unverified": 0, "object_backend": "fake"}

    async def run():
        service = CreatorImageService(Settings(), Sources(), object(), object_detector=Detector())
        result = await service.fetch(CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722", max_images=2,
            content_query="只要人物穿搭，排除风景", filter_mode="optional", download=False, output_dir=str(tmp_path / "out"), resume=False))
        assert [item["id"] for item in result["items"]] == ["person"]
        assert result["object_detection_applied"] is True
        assert result["object_rejected"] == 1

    asyncio.run(run())


def test_creator_required_object_filter_reports_when_all_items_are_rejected(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5

        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir); path.mkdir(parents=True, exist_ok=True); return path

    candidate = ImageCandidate(id="scene", platform=Platform.DOUYIN, image_url="https://img.test/scene.jpg", creator_id="sec-1", post_id="p1", media_index=1)

    class Sources:
        async def fetch_creator(self, request):
            return CreatorSourceResult(identity=_identity(), items=[candidate], posts_fetched=1, post_ids=("p1",), pages_fetched=1)

    class Detector:
        configured = True

        async def inspect(self, values, spec, fail_closed=False):
            return [], {
                "object_detection_applied": True, "object_detection_error": None,
                "object_decisions": {candidate.id: {"content_decision": "rejected"}},
                "object_rejected": 1, "object_unverified": 0, "object_backend": "fake",
            }

    async def run():
        service = CreatorImageService(Settings(), Sources(), object(), object_detector=Detector())
        result = await service.fetch(CreatorFetchRequest(
            platform=Platform.DOUYIN, creator_id="Gracebb0722", max_images=1,
            content_query="只要人物", filter_mode="required", download=False,
            output_dir=str(tmp_path / "out"), resume=False,
        ))
        assert result["items"] == []
        assert result["error"]["code"] == "content_filter_required"
        assert "no image matched" in result["error"]["message"]

    asyncio.run(run())


def test_creator_object_filter_timeout_returns_without_blocking(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5
        object_filter_timeout_seconds = 0.05
        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir); path.mkdir(parents=True, exist_ok=True); return path

    candidate = ImageCandidate(id="p1", platform=Platform.DOUYIN, image_url="https://img.test/1.jpg", creator_id="sec-1", post_id="p1", media_index=1)
    class Sources:
        async def fetch_creator(self, request):
            return CreatorSourceResult(identity=_identity(), items=[candidate], posts_fetched=1, post_ids=("p1",), pages_fetched=1)
    class Detector:
        configured = True
        async def inspect(self, *args, **kwargs):
            await asyncio.sleep(1)
            return [candidate], {"object_detection_applied": True}

    async def run():
        service = CreatorImageService(Settings(), Sources(), object(), object_detector=Detector())
        started = asyncio.get_running_loop().time()
        result = await service.fetch(CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722", max_images=1,
            content_query="只要人物", filter_mode="optional", download=False, output_dir=str(tmp_path / "out"), resume=False))
        assert asyncio.get_running_loop().time() - started < 0.5
        assert result["items"]
        assert result["object_detection_applied"] is False
        assert "time budget" in result["filter_warning"]

    asyncio.run(run())


def test_creator_resume_keeps_cumulative_counts_when_pending_images_remain(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5

        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

    candidate = ImageCandidate(
        id="p1:1", platform=Platform.DOUYIN, image_url="https://img.test/1.jpg",
        creator_id="sec-1", post_id="p1", media_index=1,
    )

    class Sources:
        calls = 0

        async def fetch_creator(self, request):
            self.calls += 1
            return CreatorSourceResult(
                identity=_identity(), items=[candidate], posts_fetched=1,
                post_ids=("p1",), pages_fetched=1, next_cursor='{"native":"2","offset":0}',
            )

    class Downloader:
        async def download_many(self, *args, **kwargs):
            raise AssertionError("download should not run when download=false")

    async def run():
        sources = Sources()
        service = CreatorImageService(Settings(), sources, Downloader())
        request = CreatorFetchRequest(
            platform=Platform.DOUYIN, creator_id="Gracebb0722", max_images=1,
            download=False, output_dir=str(tmp_path / "out"), resume=True,
        )
        first = await service.fetch(request)
        second = await service.fetch(request)
        assert first["posts_fetched"] == 1
        assert second["posts_fetched"] == 1
        assert second["post_ids"] == ["p1"]
        assert second["pages_fetched"] == 1
        assert sources.calls == 1

    asyncio.run(run())


def test_creator_resume_refreshes_exhausted_source_and_returns_new_work_list(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5

        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

    first = ImageCandidate(
        id="old:1", platform=Platform.DOUYIN, image_url="https://img.test/old.jpg",
        creator_id="sec-1", post_id="old", media_index=1,
        title="旧作品", published_at="2026-10-01T00:00:00Z",
    )
    second = ImageCandidate(
        id="new:1", platform=Platform.DOUYIN, image_url="https://img.test/new.jpg",
        creator_id="sec-1", post_id="new", media_index=1,
        title="新作品", published_at="2026-10-03T00:00:00Z",
    )

    class Sources:
        calls = 0

        async def fetch_creator(self, request):
            self.calls += 1
            item = first if self.calls == 1 else second
            return CreatorSourceResult(
                identity=_identity(), items=[item], posts_fetched=1,
                post_ids=(item.post_id,), pages_fetched=1,
            )

    class Downloader:
        async def download_many(self, items, *args, **kwargs):
            return [
                DownloadRecord(
                    candidate_id=item.id, platform=item.platform, image_url=item.image_url,
                    media_type=item.media_type, status="downloaded",
                )
                for item in items
            ]

    async def run():
        sources = Sources()
        service = CreatorImageService(Settings(), sources, Downloader())
        request = CreatorFetchRequest(
            platform=Platform.DOUYIN, creator_id="Gracebb0722", max_images=1,
            download=True, output_dir=str(tmp_path / "out"), resume=True,
        )
        first_result = await service.fetch(request)
        second_result = await service.fetch(request)
        return sources, first_result, second_result

    sources, first_result, second_result = asyncio.run(run())
    assert sources.calls == 2
    assert first_result["works"][0]["work_id"] == "old"
    assert second_result["works"][0]["work_id"] == "new"
    assert second_result["work_types"] == {"image": 1, "video": 0, "mixed": 0}


def test_creator_service_continues_source_cursor_until_target_is_filled(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5

        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

    class Sources:
        calls = []

        async def fetch_creator(self, request):
            page = len(self.calls) + 1
            self.calls.append(request.cursor)
            item = ImageCandidate(
                id=f"p{page}:1", platform=Platform.DOUYIN,
                image_url=f"https://img.test/{page}.jpg", creator_id="sec-1",
                post_id=f"p{page}", media_index=1,
            )
            return CreatorSourceResult(
                identity=_identity(), items=[item], posts_fetched=1,
                post_ids=(item.post_id,), pages_fetched=1,
                next_cursor=pack_cursor(page + 1) if page < 3 else None,
            )

    async def run():
        sources = Sources()
        service = CreatorImageService(Settings(), sources, object())
        result = await service.fetch(CreatorFetchRequest(
            platform=Platform.DOUYIN, creator_id="Gracebb0722",
            max_posts=3, max_images=3, download=False, resume=False,
        ))
        return sources, result

    sources, result = asyncio.run(run())
    assert sources.calls == [None, pack_cursor(2), pack_cursor(3)]
    assert [item["post_id"] for item in result["items"]] == ["p1", "p2", "p3"]
    assert result["pages_fetched"] == 3


def test_creator_download_replaces_duplicate_candidates_from_the_same_job(tmp_path):
    class Settings:
        cache_path = str(tmp_path / "cache.sqlite3")
        output_dir = str(tmp_path / "out")
        creator_timeout_seconds = 5

        def ensure_output_dir(self, value=None):
            from pathlib import Path
            path = Path(value or self.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

    items = [
        ImageCandidate(
            id=f"p{index}:1", platform=Platform.DOUYIN,
            image_url=f"https://img.test/{index}.jpg", creator_id="sec-1",
            post_id=f"p{index}", media_index=1,
        )
        for index in range(1, 4)
    ]

    class Sources:
        async def fetch_creator(self, request):
            return CreatorSourceResult(
                identity=_identity(), items=items, posts_fetched=3,
                post_ids=tuple(item.post_id for item in items), pages_fetched=1,
            )

    class Downloader:
        calls = []

        async def download_many(self, batch, *args, **kwargs):
            self.calls.append([item.id for item in batch])
            return [
                DownloadRecord(
                    candidate_id=item.id, platform=item.platform,
                    image_url=item.image_url,
                    status="duplicate" if item.id == "p1:1" else "downloaded",
                )
                for item in batch
            ]

    async def run():
        downloader = Downloader()
        service = CreatorImageService(Settings(), Sources(), downloader)
        result = await service.fetch(CreatorFetchRequest(
            platform=Platform.DOUYIN, creator_id="Gracebb0722",
            max_posts=3, max_images=2, download=True, resume=False,
        ))
        return downloader, result

    downloader, result = asyncio.run(run())
    assert downloader.calls == [["p1:1", "p2:1"], ["p3:1"]]
    assert [record["status"] for record in result["downloads"]] == ["duplicate", "downloaded", "downloaded"]
    assert len(result["items"]) == 3
