import asyncio

from social_image_mcp import server
from social_image_mcp.models import DownloadRecord, ImageCandidate, Platform


def test_search_video_downloads_in_sequential_batches_until_target(monkeypatch, tmp_path):
    class Settings:
        video_download_batch_size = 3
        video_download_concurrency = 1

        @staticmethod
        def ensure_output_dir(value=None):
            path = tmp_path / "out"
            path.mkdir(parents=True, exist_ok=True)
            return path

    class Service:
        settings = Settings()

        def __init__(self):
            self.search_request = None
            self.download_calls = []

        async def search(self, request):
            self.search_request = request
            return {
                "items": [
                    ImageCandidate(
                        id=f"v{index}",
                        platform=Platform.DOUYIN,
                        image_url=f"https://video.test/{index}.mp4",
                        media_type="video",
                    ).model_dump(mode="json")
                    for index in range(1, 8)
                ]
            }

        async def download(self, items, output_dir, concurrency, min_width, min_height, *, resume=False):
            self.download_calls.append(([item.id for item in items], concurrency, resume))
            return [
                DownloadRecord(
                    candidate_id=item.id,
                    platform=item.platform,
                    image_url=item.image_url,
                    media_type="video",
                    status="failed" if item.id == "v1" else "downloaded",
                )
                for item in items
            ]

    fake_service = Service()
    monkeypatch.setattr(server, "service", fake_service)

    result = asyncio.run(server.search_images(
        query="测试视频",
        platforms=["douyin"],
        max_results=4,
        media_type="videos",
        video_limit=4,
        max_posts=20,
        download=True,
        output_dir=str(tmp_path / "out"),
    ))

    assert fake_service.search_request.video_limit == 20
    assert fake_service.search_request.max_posts == 20
    assert fake_service.download_calls == [
        (["v1", "v2", "v3"], 1, True),
        (["v4", "v5"], 1, True),
    ]
    assert [record["status"] for record in result["downloads"]] == [
        "failed", "downloaded", "downloaded", "downloaded", "downloaded",
    ]


def test_exact_post_does_not_request_replacement_works(monkeypatch):
    class Service:
        settings = type("Settings", (), {
            "video_download_batch_size": 3,
            "video_download_concurrency": 1,
        })()

        async def search(self, request):
            assert request.video_limit == 1
            assert request.max_results == 1
            return {"items": []}

    monkeypatch.setattr(server, "service", Service())
    result = asyncio.run(server.search_images(
        query="https://www.douyin.com/video/7641888838654951067",
        platforms=["douyin"],
        max_results=1,
        media_type="videos",
        video_limit=1,
        download=True,
    ))
    assert result["items"] == []


def test_search_continues_beyond_old_three_times_reserve_until_success(monkeypatch, tmp_path):
    class Settings:
        video_download_batch_size = 3
        video_download_concurrency = 1
        image_download_batch_size = 10
        image_download_concurrency = 3

        @staticmethod
        def ensure_output_dir(value=None):
            path = tmp_path / "out"
            path.mkdir(parents=True, exist_ok=True)
            return path

    class Service:
        settings = Settings()

        async def search(self, request):
            assert request.video_limit == 20
            return {"items": [
                ImageCandidate(
                    id=f"v{index}", platform=Platform.DOUYIN,
                    image_url=f"https://video.test/{index}.mp4", media_type="video",
                ).model_dump(mode="json")
                for index in range(1, request.video_limit + 1)
            ]}

        async def download(self, items, *args, **kwargs):
            return [DownloadRecord(
                candidate_id=item.id, platform=item.platform,
                image_url=item.image_url, media_type="video",
                status="failed" if int(item.id[1:]) <= 12 else "downloaded",
            ) for item in items]

    monkeypatch.setattr(server, "service", Service())
    result = asyncio.run(server.search_images(
        query="补位视频", platforms=["douyin"], media_type="videos",
        video_limit=4, max_results=4, max_posts=20, download=True,
    ))

    assert sum(item["status"] == "downloaded" for item in result["downloads"]) == 4
    assert result["downloads"][-1]["candidate_id"] == "v16"


def test_download_images_batches_images_and_videos_per_account(monkeypatch, tmp_path):
    class Settings:
        image_download_batch_size = 2
        video_download_batch_size = 2
        video_download_concurrency = 1

    class Service:
        settings = Settings()
        download_calls = []

        async def download(self, items, output_dir, concurrency, min_width, min_height, *, resume=False):
            self.download_calls.append(([item.id for item in items], concurrency, resume, output_dir.name))
            return [DownloadRecord(
                candidate_id=item.id, platform=item.platform, image_url=item.image_url,
                media_type=item.media_type, status="downloaded", path=str(output_dir / item.id),
            ) for item in items]

    fake_service = Service()
    monkeypatch.setattr(server, "service", fake_service)
    items = [
        ImageCandidate(id="i1", platform=Platform.DOUYIN, image_url="https://cdn.test/i1.jpg", media_type="image"),
        ImageCandidate(id="i2", platform=Platform.DOUYIN, image_url="https://cdn.test/i2.jpg", media_type="image"),
        ImageCandidate(id="i3", platform=Platform.DOUYIN, image_url="https://cdn.test/i3.jpg", media_type="image"),
        ImageCandidate(id="v1", platform=Platform.DOUYIN, image_url="https://cdn.test/v1.mp4", media_type="video"),
        ImageCandidate(id="v2", platform=Platform.DOUYIN, image_url="https://cdn.test/v2.mp4", media_type="video"),
        ImageCandidate(id="v3", platform=Platform.DOUYIN, image_url="https://cdn.test/v3.mp4", media_type="video"),
    ]

    result = asyncio.run(server.download_images(
        [item.model_dump(mode="json") for item in items],
        output_dir=str(tmp_path / "out"), max_concurrency=3,
    ))

    assert fake_service.download_calls == [
        (["i1", "i2"], 2, True, "douyin"),
        (["i3"], 1, True, "douyin"),
        (["v1", "v2"], 1, True, "douyin"),
        (["v3"], 1, True, "douyin"),
    ]
    assert len(result["records"]) == 6
