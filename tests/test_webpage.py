import asyncio

import httpx

from social_image_mcp.intent import parse_intent
from social_image_mcp.models import Platform
from social_image_mcp.webpage import WebPageAdapter


def test_unknown_url_is_routed_to_other_platform():
    intent = parse_intent("https://example.org/gallery")
    assert intent.url == "https://example.org/gallery"
    assert intent.identifier_platform == "other"


def test_webpage_adapter_extracts_main_and_lazy_images():
    html = '''<html><head><title>Gallery</title><meta property="og:image" content="/cover.jpg"></head><body><img data-src="/photo-1.webp" alt="one"><img src="https://cdn.example/second.png"></body></html>'''

    async def run():
        def handler(request):
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await WebPageAdapter(client).search(parse_intent("https://example.org/gallery"), 10, True)
        assert len(items) == 3
        assert all(item.platform == Platform.OTHER for item in items)
        assert items[0].image_url == "https://example.org/cover.jpg"
        assert items[1].image_url == "https://example.org/photo-1.webp"

    asyncio.run(run())
