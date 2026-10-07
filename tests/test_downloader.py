import asyncio
import io
from pathlib import Path
import re

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


def test_weibo_video_download_writes_mp4_with_page_referer(tmp_path):
    seen = {}
    def handler(request):
        seen["referer"] = request.headers.get("referer")
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"weibo-video", request=request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(id="post:1", platform=Platform.WEIBO, image_url="https://video.test/play",
                                  media_type="video", permalink="https://m.weibo.cn/detail/post")
            return (await ImageDownloader(client).download_many([item], tmp_path))[0]
    record = asyncio.run(run())
    assert record.status == "downloaded"
    assert record.path.endswith(".mp4")
    assert seen["referer"] == "https://m.weibo.cn/detail/post"


def test_douyin_video_download_sends_site_referer(tmp_path):
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, content=b"video", headers={"content-type": "video/mp4"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(id="v1", platform=Platform.DOUYIN, image_url="https://cdn.test/play", media_type="video")
            record = (await ImageDownloader(client).download_many([item], tmp_path))[0]
            assert record.status == "downloaded"

    asyncio.run(run())
    assert seen["referer"] == "https://www.douyin.com/"
    assert seen["origin"] == "https://www.douyin.com"
    assert "Chrome/" in seen["user-agent"]


def test_bilibili_video_download_sends_page_origin(tmp_path):
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, content=b"video", headers={"content-type": "video/mp4"}, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(
                id="BV1:video", platform=Platform.BILIBILI,
                image_url="https://upos.example.test/video.mp4", media_type="video",
                permalink="https://www.bilibili.com/video/BV1",
            )
            return (await ImageDownloader(client).download_many([item], tmp_path))[0]

    record = asyncio.run(run())
    assert record.status == "downloaded"
    assert seen["referer"] == "https://www.bilibili.com/video/BV1"
    assert seen["origin"] == "https://www.bilibili.com"
    assert "Chrome/" in seen["user-agent"]


def test_bilibili_best_video_and_audio_tracks_are_muxed_without_transcoding(tmp_path, monkeypatch):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.host == "audio.test":
            return httpx.Response(200, content=b"best-audio", headers={"content-type": "audio/mp4"}, request=request)
        return httpx.Response(200, content=b"best-video", headers={"content-type": "video/mp4"}, request=request)

    async def fake_mux(self, executable, video, audio, output):
        assert executable == "ffmpeg-test"
        output.write_bytes(video.read_bytes() + b"+" + audio.read_bytes())

    monkeypatch.setattr(ImageDownloader, "_ffmpeg_executable", staticmethod(lambda: "ffmpeg-test"))
    monkeypatch.setattr(ImageDownloader, "_mux_streams", fake_mux)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(
                id="BV1:video", platform=Platform.BILIBILI,
                image_url="https://video.test/8k-hevc.m4s", media_type="video",
                permalink="https://www.bilibili.com/video/BV1",
                width=7680, height=4320,
                source_payload={"download": {
                    "video_urls": ["https://video.test/8k-hevc.m4s"],
                    "audio_url": "https://audio.test/flac.m4s",
                    "audio_urls": ["https://audio.test/flac.m4s"],
                    "fallback_url": "https://video.test/fallback.mp4",
                }},
            )
            return (await ImageDownloader(client).download_many([item], tmp_path))[0]

    record = asyncio.run(run())
    assert record.status == "downloaded"
    assert record.path.endswith(".mkv")
    assert record.width == 7680 and record.height == 4320
    assert Path(record.path).read_bytes() == b"best-video+best-audio"
    assert requested == ["https://video.test/8k-hevc.m4s", "https://audio.test/flac.m4s"]


def test_bilibili_uses_complete_fallback_when_muxer_is_unavailable(tmp_path, monkeypatch):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=b"complete-video", headers={"content-type": "video/mp4"}, request=request)

    monkeypatch.setattr(ImageDownloader, "_ffmpeg_executable", staticmethod(lambda: None))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(
                id="BV1:video", platform=Platform.BILIBILI,
                image_url="https://video.test/video-only.m4s", media_type="video",
                source_payload={"download": {
                    "audio_url": "https://audio.test/audio-only.m4s",
                    "fallback_url": "https://video.test/complete.mp4",
                }},
            )
            return (await ImageDownloader(client).download_many([item], tmp_path))[0]

    record = asyncio.run(run())
    assert record.status == "downloaded"
    assert record.path.endswith(".mp4")
    assert requested == ["https://video.test/complete.mp4"]


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


def test_downloader_keeps_near_identical_images_from_one_gallery(tmp_path):
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
                ImageCandidate(
                    id="gallery:1", platform=Platform.INSTAGRAM,
                    image_url="https://cdn.test/1.png", post_id="gallery", media_index=1,
                ),
                ImageCandidate(
                    id="gallery:2", platform=Platform.INSTAGRAM,
                    image_url="https://cdn.test/2.png", post_id="gallery", media_index=2,
                ),
            ]
            records = await downloader.download_many(items, tmp_path)
            assert [record.status for record in records] == ["downloaded", "downloaded"]
            assert len({record.sha256 for record in records}) == 2
            assert len(list((tmp_path / "images").glob("*"))) == 2

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


def test_downloader_names_work_with_timestamp_id_title_and_media_namespace(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(buffer, format="JPEG")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=buffer.getvalue(), request=request)
        )) as client:
            item = ImageCandidate(
                id="work-1:1", platform=Platform.DOUYIN, image_url="https://cdn.test/work.jpg",
                creator_id="sec-1", post_id="work-1", media_index=1,
                title="春日穿搭/原图", published_at="2026-10-03T12:34:56+08:00",
            )
            return (await ImageDownloader(client).download_many([item], tmp_path))[0]

    record = asyncio.run(run())
    path = Path(record.path)
    assert path.parent == tmp_path / "images"
    assert re.match(r"^20261003_043456_work-1_春日穿搭_原图\.jpg$", path.name)


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


def test_douyin_video_accepts_mislabeled_complete_mp4(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "audio/mp4"}, content=b"complete-mp4", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(
                id="douyin-mp4", platform=Platform.DOUYIN,
                image_url="https://douyin.test/play", media_type="video",
            )
            return (await ImageDownloader(client).download_many([item], tmp_path))[0]

    record = asyncio.run(run())

    assert record.status == "downloaded"
    assert record.content_type == "audio/mp4"
    assert record.path.endswith(".mp4")


def test_video_download_resumes_after_an_incomplete_response(tmp_path):
    payload = b"first-part-second-part"
    calls = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload[:10]
            raise httpx.RemoteProtocolError("peer closed early")

    def handler(request):
        calls.append(request.headers.get("range"))
        if len(calls) == 1:
            return httpx.Response(200, headers={"content-type": "video/mp4"}, stream=BrokenStream())
        assert request.headers["range"] == "bytes=10-"
        return httpx.Response(
            206,
            headers={"content-type": "video/mp4", "content-range": f"bytes 10-{len(payload) - 1}/{len(payload)}"},
            content=payload[10:],
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            item = ImageCandidate(
                id="resumed", platform=Platform.DOUYIN,
                image_url="https://video.test/resume.mp4", media_type="video",
            )
            return (await ImageDownloader(client).download_many([item], tmp_path))[0]

    record = asyncio.run(run())

    assert record.status == "downloaded", record.error
    assert Path(record.path).read_bytes() == payload
    assert calls == [None, "bytes=10-"]


def test_webpage_download_rejects_tiny_images_even_without_user_size_filter(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (96, 96), "red").save(buffer, format="PNG")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "image/png"})
        )) as client:
            item = ImageCandidate(id="hashed-icon", platform=Platform.OTHER, image_url="https://example.org/a")
            record = (await ImageDownloader(client).download_many([item], tmp_path))[0]
        assert record.status == "rejected"
        assert record.path is None
        assert not list(tmp_path.glob("*.png"))

    asyncio.run(run())
