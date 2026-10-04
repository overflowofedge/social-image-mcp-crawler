import asyncio
import io

import httpx
from PIL import Image
import pytest

from social_image_mcp.adapters.base import AdapterError
from social_image_mcp.intent import parse_intent
from social_image_mcp.models import Platform, SearchRequest
from social_image_mcp.webpage import WebPageAdapter, extract_page


def image_bytes(size=(800, 600)):
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, "PNG")
    return buffer.getvalue()


def html_response(html):
    return httpx.Response(200, text=html, headers={"content-type": "text/html"})


def media_response(request):
    if request.url.path.endswith((".mp4", ".webm")):
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypisom", headers={"content-type": "video/mp4"})
    return httpx.Response(200, content=image_bytes(), headers={"content-type": "image/png"})


def test_unknown_url_is_routed_to_other_platform():
    intent = parse_intent("https://example.org/gallery")
    assert intent.url == "https://example.org/gallery"
    assert intent.identifier_platform == "other"


def test_combined_media_limits_accept_the_desktop_total():
    request = SearchRequest(query="https://example.org/", max_results=2000, image_limit=1000, video_limit=1000, media_type="all")
    assert request.max_results == 2000
    with pytest.raises(ValueError, match="single media type"):
        SearchRequest(query="https://example.org/", max_results=2000, media_type="images")


def test_webpage_adapter_extracts_main_and_lazy_images():
    html = '''<html><head><title>Gallery</title><meta property="og:image" content="/cover.jpg"></head><body><img data-src="/photo-1.webp" alt="one"><img src="https://cdn.example/second.png"></body></html>'''

    async def run():
        def handler(request):
            return html_response(html) if request.url.path == "/gallery" else media_response(request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await WebPageAdapter(client).search(parse_intent("https://example.org/gallery"), 10, True)
        assert len(items) == 2
        assert all(item.platform == Platform.OTHER for item in items)
        assert items[0].image_url == "https://example.org/photo-1.webp"
        assert items[1].image_url == "https://cdn.example/second.png"
        assert all(item.width == 800 for item in items)

    asyncio.run(run())


def test_content_extraction_excludes_ui_even_with_image_extensions():
    html = '''<meta property="og:image" content="/logo.png">
    <header><img src="/hash-header.jpg"></header><nav><img src="/hash-nav.jpg"></nav>
    <main><img src="/logo.png"><img src="/user-icon.png"><img src="/photo.jpg" alt="内容照片">
    <div class="toolbar"><img src="/hash-toolbar.png"></div>
    <div hidden><img src="/hidden.jpg"></div><img src="/hash.png" class="avatar">
    <div class="popup"><video src="/ad.mp4"></video></div></main>
    <footer><img src="/footer.jpg"></footer>'''
    result = extract_page(html, "https://example.org/")
    assert [item.image_url for item in result.items] == ["https://example.org/photo.jpg"]


def test_metadata_cannot_reintroduce_header_branding():
    result = extract_page('<meta property="og:image" content="/hash.png"><header><img src="/hash.png"></header>', "https://example.org/")
    assert result.items == []


def test_original_and_best_responsive_image_are_selected_once():
    html = '''<main><a href="/original.jpg"><img src="/thumb.jpg" srcset="/small.jpg 400w, /big.jpg 1600w"></a>
    <picture><source srcset="/small.webp 400w, /large.webp 1600w"><img src="/fallback.jpg"></picture>
    <img src="/placeholder.png" data-src="/lazy.jpg" srcset="/medium.jpg 800w, /full.jpg 2400w">
    <img src="/tiny.jpg" data-original="/camera.jpg"></main>'''
    urls = [item.image_url for item in extract_page(html, "https://example.org/").items]
    assert urls == ["https://example.org/" + name for name in ("original.jpg", "large.webp", "full.jpg", "camera.jpg")]


def test_icon_and_product_badge_links_do_not_trigger_detail_crawls():
    html = '''<main><div class="content">
    <a href="/help"><img src="/support.svg"><h2>Support</h2></a>
    <a href="/products/other"><img src="/product-badge.png"></a>
    <a href="/company"><img src="/brand-lockup.jpg"></a>
    <a href="/post/artwork"><img src="/placeholder.svg" data-src="/artwork.jpg"></a>
    </div></main>'''
    result = extract_page(html, "https://example.org/")
    assert result.links == ["https://example.org/post/artwork"]
    assert [item.image_url for item in result.items] == ["https://example.org/artwork.jpg"]


def test_linked_original_keeps_the_image_caption():
    result = extract_page(
        '<main><a href="/full.jpg"><img data-src="/thumb.jpg" alt="Forest garden"></a></main>',
        "https://example.org/",
    )
    assert len(result.items) == 1
    assert result.items[0].image_url == "https://example.org/full.jpg"
    assert result.items[0].alt_text == "Forest garden"


def test_originals_take_priority_over_earlier_gallery_thumbnails():
    thumbnails = ''.join(f'<img src="/thumb-{index}.jpg">' for index in range(6))
    html = f'''<main>{thumbnails}
    <a href="/full.jpg"><img src="/preview.jpg"></a>
    <img src="/preview2.jpg" data-original="/camera.jpg">
    <video src="/clip.mp4"></video></main>'''

    def handler(request):
        return html_response(html) if request.url.path == "/" else media_response(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebPageAdapter(client).search_media(SearchRequest(
                query="https://example.org/", media_type="all", image_limit=2,
                video_limit=1, per_post_limit=2,
            ))
        assert [(item.media_type, item.image_url) for item in result.items] == [
            ("image", "https://example.org/full.jpg"),
            ("image", "https://example.org/camera.jpg"),
            ("video", "https://example.org/clip.mp4"),
        ]
        assert result.warnings == []

    asyncio.run(run())


def test_structured_video_does_not_treat_embed_page_or_poster_as_video():
    html = '''<main><video poster="/poster.jpg"><source src="/movie.mp4" type="video/mp4"><source src="/movie.webm"></video></main>
    <script type="application/ld+json">{"@graph":[
    {"@type":"VideoObject","contentUrl":"/second.mp4","embedUrl":"/player","thumbnailUrl":"/thumb.jpg"},
    {"@type":"Organization","logo":{"@type":"ImageObject","url":"/hash.png"}}]}</script>
    <meta property="og:video" content="https://player.example/embed/42">'''
    items = extract_page(html, "https://example.org/").items
    assert [(item.image_url, item.media_type) for item in items] == [
        ("https://example.org/movie.mp4", "video"), ("https://example.org/second.mp4", "video")]
    assert items[0].thumbnail_url == "https://example.org/poster.jpg"


def test_webpage_video_prefers_highest_non_hdr_hevc_source():
    html = '''<main><video poster="/poster.jpg">
    <source src="/8k-hdr-hevc.mp4" data-quality="8k hdr hevc">
    <source src="/4k-h264.mp4" data-quality="3840x2160 h264">
    <source src="/4k-hevc.mp4" data-quality="3840x2160 hevc">
    <source src="/1080p-hevc.mp4" data-quality="1920x1080 hevc">
    </video></main>'''
    items = extract_page(html, "https://example.org/").items
    assert len(items) == 1
    assert items[0].image_url == "https://example.org/4k-hevc.mp4"
    assert items[0].thumbnail_url == "https://example.org/poster.jpg"


def test_homepage_follows_only_bounded_content_links_and_keeps_both_media_types():
    calls = []
    homepage = '''<nav><a href="/navigation"><img src="/nav.jpg"></a></nav><main>
    <a href="/post/one"><img src="/thumb.jpg"></a><a href="/post/two"><img src="/thumb2.jpg"></a>
    <a href="https://outside.test/post"><img src="/external-card.jpg"></a></main>'''
    detail = '<article><img src="/full.jpg"><img src="/extra.jpg"><video src="/clip.mp4"></video><a href="/post/three"><img src="/third.jpg"></a></article>'

    def handler(request):
        calls.append(str(request.url))
        if request.url.path == "/":
            return html_response(homepage)
        if request.url.path == "/post/one":
            return html_response(detail)
        assert not request.url.path.startswith("/post/")
        return media_response(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebPageAdapter(client).search_media(SearchRequest(
                query="https://example.org/", media_type="all", image_limit=1, video_limit=1,
                max_posts=1, per_post_limit=1,
            ))
        assert [(item.media_type, item.image_url) for item in result.items] == [
            ("image", "https://example.org/full.jpg"), ("video", "https://example.org/clip.mp4")]
        assert all(item.permalink == "https://example.org/post/one" for item in result.items)
        assert "https://example.org/navigation" not in calls
        assert not any("outside.test" in url for url in calls)

    asyncio.run(run())


def test_real_dimensions_reject_hashed_icons_and_tracking_pixels():
    def handler(request):
        if request.url.path == "/":
            return html_response('<main><img src="/a"><img src="/b"><img src="/c"></main>')
        size = {"/a": (96, 96), "/b": (1, 1), "/c": (1200, 800)}[request.url.path]
        return httpx.Response(200, content=image_bytes(size), headers={"content-type": "image/png"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebPageAdapter(client).search_media(SearchRequest(query="https://example.org/", max_results=1))
        assert [item.image_url for item in result.items] == ["https://example.org/c"]

    asyncio.run(run())


def test_video_only_does_not_fetch_images_and_rejects_html_player():
    def handler(request):
        if request.url.path == "/":
            return html_response('<main><img src="/photo.jpg"><video src="/player"></video><video src="/movie.mp4"></video></main>')
        assert request.url.path != "/photo.jpg"
        return html_response("player") if request.url.path == "/player" else media_response(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebPageAdapter(client).search_media(SearchRequest(query="https://example.org/", media_type="videos"))
        assert [item.image_url for item in result.items] == ["https://example.org/movie.mp4"]

    asyncio.run(run())


def test_stream_playlist_returns_explicit_error_instead_of_fake_mp4():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: html_response('<video src="/stream.m3u8"></video>'))) as client:
            with pytest.raises(AdapterError, match="HLS/DASH"):
                await WebPageAdapter(client).search_media(SearchRequest(query="https://example.org/", media_type="videos"))

    asyncio.run(run())


def test_one_failed_detail_page_preserves_other_results():
    def handler(request):
        if request.url.path == "/":
            return html_response('<main><a href="/post/one"><img src="/cover.jpg"></a><a href="/post/two"><img src="/cover2.jpg"></a></main>')
        if request.url.path == "/post/one":
            return httpx.Response(503)
        if request.url.path == "/post/two":
            return html_response('<video src="/clip.mp4"></video>')
        return media_response(request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await WebPageAdapter(client).search_media(SearchRequest(query="https://example.org/", media_type="videos"))
        assert len(result.items) == 1
        assert any("详情页访问失败" in warning for warning in result.warnings)

    asyncio.run(run())


def test_dynamic_homepage_uses_rendered_content(monkeypatch):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: html_response('<main id="app"></main>') if request.url.path == "/" else media_response(request))) as client:
            adapter = WebPageAdapter(client, render_pages=True)

            async def render(url):
                return extract_page('<main><img data-src="/loaded.jpg"><video src="/loaded.mp4"></video></main>', url)

            monkeypatch.setattr(adapter, "_render_page", render)
            result = await adapter.search_media(SearchRequest(query="https://example.org/", media_type="all"))
        assert {item.media_type for item in result.items} == {"image", "video"}

    asyncio.run(run())
