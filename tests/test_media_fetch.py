import asyncio

import httpx
import pytest

from social_image_mcp.media_fetch import (
    get_media, media_request_headers, parse_referrer_policy, referrer_for, stream_media,
)
from social_image_mcp.models import ImageCandidate, Platform

PAGE = "https://www.site.test/post/123.html"
FULL = "https://user:pw@www.site.test/post/123.html?x=1#frag"
CLEAN = "https://www.site.test/post/123.html?x=1"
PLACEHOLDER = b"the-image-hosts-placeholder"


@pytest.mark.parametrize(("policy", "target", "expected"), [
    ("no-referrer", "https://cdn.test/a.jpg", ""),
    ("unsafe-url", "https://cdn.test/a.jpg", CLEAN),
    ("no-referrer-when-downgrade", "https://cdn.test/a.jpg", CLEAN),
    ("no-referrer-when-downgrade", "http://cdn.test/a.jpg", ""),
    ("origin", "https://cdn.test/a.jpg", "https://www.site.test"),
    ("strict-origin", "https://cdn.test/a.jpg", "https://www.site.test"),
    ("strict-origin", "http://cdn.test/a.jpg", ""),
    ("origin-when-cross-origin", "https://www.site.test/other.jpg", CLEAN),
    ("origin-when-cross-origin", "https://cdn.test/a.jpg", "https://www.site.test"),
    ("strict-origin-when-cross-origin", "https://www.site.test/other.jpg", CLEAN),
    ("strict-origin-when-cross-origin", "https://cdn.test/a.jpg", "https://www.site.test"),
    ("strict-origin-when-cross-origin", "http://www.site.test/a.jpg", ""),
    # A policy this code does not know leaves the caller's header alone.
    ("made-up-policy", "https://cdn.test/a.jpg", FULL),
])
def test_referrer_for_follows_each_policy(policy, target, expected):
    assert referrer_for(policy, FULL, target) == expected


def test_referrer_for_drops_a_referrer_that_is_not_a_web_page():
    assert referrer_for("strict-origin-when-cross-origin", "android-app://app/x", "https://cdn.test/a.jpg") == ""
    assert referrer_for("no-referrer", "", "https://cdn.test/a.jpg") == ""


@pytest.mark.parametrize(("value", "expected"), [
    ("no-referrer", "no-referrer"),
    ("strict-origin, NO-REFERRER", "no-referrer"),
    ("bogus, origin", "origin"),
    ("bogus", None),
    ("", None),
    (None, None),
])
def test_parse_referrer_policy_keeps_the_last_recognized_name(value, expected):
    assert parse_referrer_policy(value) == expected


def _image_host(log, *, policy="no-referrer"):
    """The image host that replaced a real gallery with one placeholder.

    The image URL needs the page Referer and redirects to a CDN; the redirect
    response names the policy for that next hop, and the CDN refuses any
    request that carries a Referer. Asked without a Referer, the image URL
    redirects to a placeholder instead and answers 200.
    """
    def handler(request):
        log.append((request.url.host, request.headers.get("referer")))
        headers = {"location": f"https://cdn.test{request.url.path}"}
        if policy:
            headers["referrer-policy"] = policy
        if request.url.host == "lost.test":
            return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=PLACEHOLDER, request=request)
        if request.url.host == "img.test":
            if request.headers.get("referer"):
                return httpx.Response(302, headers=headers, request=request)
            return httpx.Response(302, headers={**headers, "location": "https://lost.test/lost.jpg"}, request=request)
        if request.headers.get("referer"):
            return httpx.Response(403, text="forbidden", request=request)
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"real-image", request=request)
    return handler


def test_redirect_following_sends_the_referer_a_browser_would():
    log = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_image_host(log))) as client:
            response = await get_media(client, "https://img.test/a.jpg", headers={"Referer": PAGE})
        assert response.status_code == 200
        assert response.content == b"real-image"
        assert log == [("img.test", PAGE), ("cdn.test", None)]
        assert response.extensions["redirect_chain"] == ["https://img.test/a.jpg"]

    asyncio.run(run())


def test_plain_httpx_redirects_fail_on_the_same_chain():
    """The regression this module exists for: re-sending the Referer on the
    CDN hop is refused, and dropping it from the start yields the placeholder."""
    log = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_image_host(log))) as client:
            refused = await client.get("https://img.test/a.jpg", headers={"Referer": PAGE}, follow_redirects=True)
            assert refused.status_code == 403
            replaced = await client.get("https://img.test/a.jpg", follow_redirects=True)
            assert replaced.status_code == 200
            assert replaced.content == PLACEHOLDER

    asyncio.run(run())


def test_without_a_policy_header_the_referer_is_kept_as_before():
    log = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_image_host(log, policy=None))) as client:
            response = await get_media(client, "https://img.test/a.jpg", headers={"Referer": PAGE})
        assert log == [("img.test", PAGE), ("cdn.test", PAGE)]
        # The CDN refuses it, exactly as it would refuse a browser: that is a
        # real failure to report, not something to work around.
        assert response.status_code == 403

    asyncio.run(run())


def test_a_default_policy_header_narrows_the_referer_to_its_origin():
    log = []

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_image_host(log, policy="strict-origin-when-cross-origin"))) as client:
            response = await get_media(client, "https://img.test/a.jpg", headers={"Referer": PAGE})
        assert log == [("img.test", PAGE), ("cdn.test", "https://www.site.test")]
        assert response.status_code == 403

    asyncio.run(run())


def test_redirects_drop_credentials_only_when_leaving_the_origin():
    seen = {}

    def handler(request):
        seen[str(request.url)] = request.headers.get("authorization")
        if request.url.path in {"/away", "/upgrade"}:
            target = "https://other.test/x" if request.url.path == "/away" else "https://site.test/x"
            return httpx.Response(302, headers={"location": target}, request=request)
        return httpx.Response(200, text="ok", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await get_media(client, "https://site.test/away", headers={"Authorization": "Bearer t"})
            await get_media(client, "http://site.test/upgrade", headers={"Authorization": "Bearer t"})

    asyncio.run(run())
    assert seen["https://other.test/x"] is None
    assert seen["https://site.test/x"] == "Bearer t"


def test_a_direct_response_is_requested_once_and_closed_after_use():
    calls = []

    def handler(request):
        calls.append((str(request.url), dict(request.headers)))
        return httpx.Response(200, content=b"image", headers={"content-type": "image/jpeg"}, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await get_media(client, "https://cdn.test/a.jpg", headers={"Referer": PAGE})
        assert len(calls) == 1 and response.content == b"image"
        assert response.extensions["redirect_chain"] == []
        assert response.is_closed

    asyncio.run(run())


def test_cookies_set_by_an_early_hop_reach_the_later_one():
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        if request.url.path == "/start":
            return httpx.Response(302, headers=[("location", "/x"), ("set-cookie", "sid=1; Path=/")], request=request)
        return httpx.Response(200, text="ok", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await get_media(client, "https://site.test/start")

    asyncio.run(run())
    assert seen == [None, "sid=1"]


def test_a_redirect_loop_is_reported_instead_of_spinning():
    def handler(request):
        return httpx.Response(302, headers={"location": "/loop"}, request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.TooManyRedirects):
                await get_media(client, "https://site.test/loop")

    asyncio.run(run())


def test_streaming_leaves_the_final_response_readable_for_the_caller():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://cdn.test/a"}, request=request)
        return httpx.Response(200, content=b"0123456789", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with stream_media(client, "https://site.test/start") as response:
                body = await response.aread()
            assert body == b"0123456789"
            assert response.is_closed

    asyncio.run(run())


@pytest.mark.parametrize(("platform", "expected"), [
    ("x", {"Referer": "https://x.com/"}),
    ("weibo", {"Referer": PAGE}),
    ("douyin", {"Referer": PAGE, "Origin": "https://www.douyin.com"}),
    ("bilibili", {"Referer": PAGE, "Origin": "https://www.bilibili.com"}),
    ("other", {"Referer": PAGE}),
])
def test_each_platform_keeps_its_request_headers(platform, expected):
    item = ImageCandidate(id="a", platform=Platform(platform), image_url="https://cdn.test/a.jpg", permalink=PAGE)
    headers = media_request_headers(item)
    assert {key: headers[key] for key in expected} == expected


def test_a_page_declared_referrer_policy_decides_what_its_images_send():
    url = "https://cdn.test/a.jpg"
    declared = lambda policy: ImageCandidate(  # noqa: E731
        id="a", platform=Platform.OTHER, image_url=url, permalink=PAGE,
        source_payload={"referrer_policy": policy},
    )
    assert "Referer" not in media_request_headers(declared("no-referrer"))
    assert media_request_headers(declared("origin"))["Referer"] == "https://www.site.test"
    assert media_request_headers(declared("not-a-policy"))["Referer"] == PAGE
