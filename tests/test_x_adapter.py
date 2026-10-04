import asyncio

import httpx

from social_image_mcp.adapters.platforms import XAdapter
from social_image_mcp.intent import parse_intent


def test_x_direct_id_normalizes_single_post_response():
    def handler(request):
        assert request.url.params["post.fields"]
        return httpx.Response(
            200,
            json={
                "data": {"id": "123", "text": "coffee shop", "attachments": {"media_keys": ["m1"]}},
                "includes": {"media": [{"media_key": "m1", "type": "photo", "url": "https://pbs.twimg.com/media/a.jpg?format=jpg&name=small", "width": 1200, "height": 800}]},
            },
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await XAdapter(client, "token").search(parse_intent("x:123"), 10, True)
            assert len(items) == 1
            assert items[0].id == "123"
            assert items[0].width == 1200
            assert "name=orig" in items[0].image_url

    asyncio.run(run())


def test_x_video_uses_highest_bitrate_variant_instead_of_cover():
    def handler(request):
        assert "variants" in request.url.params["media.fields"]
        return httpx.Response(
            200,
            json={
                "data": {"id": "456", "text": "video", "attachments": {"media_keys": ["m2"]}},
                "includes": {"media": [{
                    "media_key": "m2", "type": "video",
                    "preview_image_url": "https://pbs.twimg.com/media/cover.jpg",
                    "variants": [
                        {"content_type": "video/mp4", "bit_rate": 256000, "url": "https://video.twimg.com/low.mp4"},
                        {"content_type": "video/mp4", "bit_rate": 1024000, "url": "https://video.twimg.com/high.mp4"},
                    ],
                    "width": 1280, "height": 720,
                }]},
            },
            request=request,
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await XAdapter(client, "token").search(parse_intent("x:456"), 10, True)
            assert len(items) == 1
            assert items[0].media_type == "video"
            assert items[0].image_url.endswith("high.mp4")
            assert items[0].thumbnail_url.endswith("cover.jpg")

    asyncio.run(run())


def test_x_video_excludes_hdr_and_prefers_hevc_at_the_highest_resolution():
    media = {
        "width": 7680, "height": 4320,
        "variants": [
            {"content_type": "video/mp4", "bit_rate": 90000000,
             "url": "https://video.twimg.com/vid/hevc/7680x4320/hdr-video.mp4"},
            {"content_type": "video/mp4", "bit_rate": 80000000,
             "url": "https://video.twimg.com/vid/avc1/3840x2160/video.mp4"},
            {"content_type": "video/mp4", "bit_rate": 70000000,
             "url": "https://video.twimg.com/vid/hevc/3840x2160/video.mp4"},
        ],
    }
    assert XAdapter._best_video_variant(media).endswith("hevc/3840x2160/video.mp4")
