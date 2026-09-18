import asyncio
import io

import httpx
from PIL import Image

from social_image_mcp.downloader import ImageDownloader
from social_image_mcp.models import ImageCandidate, Platform


def test_downloader_validates_and_writes_manifest(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 20), "red").save(buffer, format="PNG")
    payload = buffer.getvalue()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=payload, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            downloader = ImageDownloader(client)
            item = ImageCandidate(id="abc", platform=Platform.X, image_url="https://example.test/a.png")
            records = await downloader.download_many([item], tmp_path)
            assert records[0].status == "downloaded"
            assert records[0].width == 32
            assert (tmp_path / "manifest.jsonl").exists()

    asyncio.run(run())


def test_weibo_image_download_sends_page_referer(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 20), "red").save(buffer, format="JPEG")
    payload = buffer.getvalue()
    seen = {}

    def handler(request):
        seen["referer"] = request.headers.get("referer")
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=payload, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(
                id="wb-1:1", platform=Platform.WEIBO,
                image_url="https://wx2.sinaimg.cn/mw2000/a.jpg",
                permalink="https://m.weibo.cn/detail/123",
            )
            record = (await ImageDownloader(client).download_many([item], tmp_path))[0]
            assert record.status == "downloaded"
            assert seen["referer"] == "https://m.weibo.cn/detail/123"

    asyncio.run(run())


def test_downloader_deduplicates_near_identical_images(tmp_path):
    first = io.BytesIO()
    second = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(first, format="PNG")
    Image.new("RGB", (64, 64), "#ff0001").save(second, format="PNG")
    payloads = [first.getvalue(), second.getvalue()]

    def handler(request):
        index = 0 if request.url.path.endswith("1.png") else 1
        return httpx.Response(200, content=payloads[index], request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            downloader = ImageDownloader(client)
            items = [
                ImageCandidate(id="1", platform=Platform.X, image_url="https://cdn.test/1.png"),
                ImageCandidate(id="2", platform=Platform.X, image_url="https://cdn.test/2.png"),
            ]
            records = await downloader.download_many(items, tmp_path)
            assert [record.status for record in records].count("duplicate") == 1

    asyncio.run(run())


def test_downloader_resume_reuses_verified_existing_file(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "blue").save(buffer, format="JPEG")
    payload = buffer.getvalue()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=payload, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            downloader = ImageDownloader(client)
            item = ImageCandidate(id="creator-post:1", platform=Platform.DOUYIN, image_url="https://example.test/a.jpg", creator_id="sec-1", post_id="creator-post", media_index=1)
            first = await downloader.download_many([item], tmp_path, resume=True)
            second = await downloader.download_many([item], tmp_path, resume=True)
            assert first[0].status == "downloaded"
            assert second[0].status == "existing"

    asyncio.run(run())


def test_downloader_accepts_video_media_and_keeps_media_type(tmp_path):
    payload = b"fake-mp4-payload"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=payload, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            downloader = ImageDownloader(client)
            item = ImageCandidate(id="v1", platform=Platform.DOUYIN, image_url="https://cdn.test/v1.mp4", media_type="video")
            record = (await downloader.download_many([item], tmp_path))[0]
            assert record.status == "downloaded"
            assert record.media_type == "video"
            assert record.path and record.path.endswith(".mp4")

    asyncio.run(run())
