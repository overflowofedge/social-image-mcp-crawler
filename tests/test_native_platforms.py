import asyncio

import httpx
import pytest

from social_image_mcp.bilibili import BilibiliApi
from social_image_mcp.weibo import WeiboApi, WeiboError
from social_image_mcp.intent import parse_intent
from social_image_mcp.models import CreatorFetchRequest, Platform


def _client(routes):
    def handler(request):
        for prefix, payload in routes.items():
            if request.url.path == prefix:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": -404, "message": "missing"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_bilibili_search_maps_cover_and_identifier():
    async def run():
        client = _client({"/x/web-interface/search/type": {"code": 0, "data": {"result": [{"bvid": "BV1abc", "pic": "//img.test/a.jpg", "title": "咖啡店"}]}}})
        async with client:
            items = await BilibiliApi(client).search(parse_intent("咖啡店"), 3)
        assert items[0].platform == Platform.BILIBILI
        assert items[0].image_url == "https://img.test/a.jpg"
        assert items[0].post_id == "BV1abc"
    asyncio.run(run())


def test_bilibili_search_retries_with_wbi_after_http_412():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/x/web-interface/search/type" and "w_rid" not in request.url.params:
            return httpx.Response(412)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={
                "code": -101,
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                    }
                },
            })
        return httpx.Response(200, json={"code": 0, "data": {"result": [
            {"bvid": "BV1signed", "pic": "//img.test/signed.jpg", "title": "签名搜索"}
        ]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).search(parse_intent("签名"), 1)

    result = asyncio.run(run())
    assert result[0].post_id == "BV1signed"
    assert [request.url.path for request in seen] == [
        "/x/web-interface/search/type", "/x/web-interface/nav", "/x/web-interface/search/type"
    ]
    assert "w_rid" in seen[-1].url.params
    assert "wts" in seen[-1].url.params


def test_bilibili_search_retries_after_json_412_code():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/x/web-interface/search/type" and "w_rid" not in request.url.params:
            return httpx.Response(200, json={"code": -412, "message": "请求被拦截"})
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json={
                "code": -101,
                "data": {"wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                }},
            })
        return httpx.Response(200, json={"code": 0, "data": {"result": [
            {"bvid": "BV1json412", "pic": "//img.test/json412.jpg", "title": "JSON challenge"}
        ]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).search(parse_intent("JSON challenge"), 1)

    result = asyncio.run(run())
    assert result[0].post_id == "BV1json412"
    assert "order" in seen[0].url.params
    assert "w_rid" in seen[-1].url.params


def test_bilibili_video_link_returns_original_video_when_requested():
    def handler(request):
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(200, json={"code": 0, "data": {
                "bvid": "BV1video", "pic": "//img.test/cover.jpg", "title": "视频",
                "owner": {"mid": 7, "name": "作者"}, "pages": [{"cid": 9}],
            }})
        if request.url.path == "/x/player/playurl":
            return httpx.Response(200, json={"code": 0, "data": {
                "durl": [{"url": "https://video.test/original.mp4"}],
            }})
        return httpx.Response(404, json={"code": -404})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).search(
                parse_intent("https://www.bilibili.com/video/BV1video"), 1, media_type="videos"
            )

    result = asyncio.run(run())
    assert len(result) == 1
    assert result[0].media_type == "video"
    assert result[0].image_url == "https://video.test/original.mp4"


def test_weibo_creator_request_accepts_numeric_uid():
    request = CreatorFetchRequest(platform=Platform.WEIBO, creator_id="123456789", download=False)
    assert request.creator_id == "123456789"


def test_bilibili_profile_url_is_parsed_as_creator():
    intent = parse_intent("https://space.bilibili.com/2")
    assert intent.identifier_platform == "bilibili"
    assert intent.identifier_scope == "creator"
    assert intent.identifier == "2"


def test_bilibili_creator_name_resolves_by_account_nickname():
    def handler(request):
        if request.url.path == "/x/web-interface/search/type":
            return httpx.Response(200, json={"code": 0, "data": {"result": [
                {"mid": 288159073, "uname": "机械人读书笔记", "is_upuser": 1},
            ]}})
        if request.url.path == "/x/web-interface/card":
            return httpx.Response(200, json={"code": 0, "data": {
                "card": {"mid": "288159073", "name": "机械人读书笔记"},
            }})
        return httpx.Response(404, json={"code": -404})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).resolve_identity(
                CreatorFetchRequest(
                    platform=Platform.BILIBILI,
                    creator_name="机械人读书笔记",
                    download=False,
                )
            )

    identity = asyncio.run(run())
    assert identity.canonical_id == "288159073"
    assert identity.matched_by == "exact_account_name"


def test_bilibili_creator_uses_filtered_video_search_when_timeline_is_challenged():
    def handler(request):
        if request.url.path == "/x/web-interface/search/type":
            search_type = request.url.params.get("search_type")
            if search_type == "bili_user":
                return httpx.Response(200, json={"code": 0, "data": {"result": [
                    {"mid": 42, "uname": "示例账号"},
                ]}})
            return httpx.Response(200, json={"code": 0, "data": {"result": [
                {"bvid": "BV1fallback", "mid": 42, "author": "示例账号",
                 "pic": "//img.test/fallback.jpg", "title": "公开搜索作品"},
                {"bvid": "BV1other", "mid": 99, "author": "其他账号",
                 "pic": "//img.test/other.jpg", "title": "不应混入"},
            ]}})
        if request.url.path == "/x/web-interface/card":
            return httpx.Response(200, json={"code": 0, "data": {
                "card": {"mid": "42", "name": "示例账号"},
            }})
        if request.url.path == "/x/space/wbi/arc/search":
            return httpx.Response(200, json={"code": -352, "message": "风控校验失败"})
        return httpx.Response(404, json={"code": -404, "message": "missing"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).fetch_creator(CreatorFetchRequest(
                platform=Platform.BILIBILI,
                creator_name="示例账号",
                max_posts=5,
                download=False,
            ))

    result = asyncio.run(run())
    assert [item.post_id for item in result.items] == ["BV1fallback"]
    assert result.posts_fetched == 1
    assert any("creator timeline unavailable" in warning for warning in result.warnings)


def test_bilibili_creator_fallback_searches_multiple_pages_for_exact_account(monkeypatch):
    seen_pages = []

    def handler(request):
        if request.url.path == "/x/web-interface/search/type":
            search_type = request.url.params.get("search_type")
            if search_type == "bili_user":
                return httpx.Response(200, json={"code": 0, "data": {"result": [
                    {"mid": 42, "uname": "示例账号"},
                ]}})
            page = int(request.url.params.get("page", "1"))
            seen_pages.append(page)
            rows = {
                1: [
                    {"bvid": "BVwrong", "mid": 99, "author": "示例账号", "pic": "//img.test/wrong.jpg"},
                    {"bvid": "BV1", "mid": 42, "author": "示例账号", "pic": "//img.test/1.jpg"},
                ],
                2: [
                    {"bvid": "BV2", "mid": 42, "author": "示例账号", "pic": "//img.test/2.jpg"},
                    {"bvid": "BV3", "mid": 42, "author": "示例账号", "pic": "//img.test/3.jpg"},
                ],
            }.get(page, [])
            return httpx.Response(200, json={"code": 0, "data": {"result": rows}})
        if request.url.path == "/x/web-interface/card":
            return httpx.Response(200, json={"code": 0, "data": {"card": {"mid": "42", "name": "示例账号"}}})
        if request.url.path == "/x/space/wbi/arc/search":
            return httpx.Response(200, json={"code": -352, "message": "风控校验失败"})
        return httpx.Response(404, json={"code": -404})

    monkeypatch.setenv("BILIBILI_CREATOR_SLEEP_SECONDS", "0.1")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).fetch_creator(CreatorFetchRequest(
                platform=Platform.BILIBILI, creator_name="示例账号",
                max_posts=3, max_images=3, download=False,
            ))

    result = asyncio.run(run())
    assert seen_pages == [1, 2]
    assert [item.post_id for item in result.items] == ["BV1", "BV2", "BV3"]


def test_bilibili_keyword_search_collects_multiple_pages(monkeypatch):
    seen_pages = []

    def handler(request):
        if request.url.path != "/x/web-interface/search/type":
            return httpx.Response(404, json={"code": -404})
        page = int(request.url.params.get("page", "1"))
        seen_pages.append(page)
        start = (page - 1) * 50
        count = 50 if page == 1 else 25
        rows = [
            {
                "bvid": f"BV{index}", "mid": 42, "author": "示例账号",
                "pic": f"//img.test/{index}.jpg", "title": "咖啡店室内",
            }
            for index in range(start, start + count)
        ]
        return httpx.Response(200, json={"code": 0, "data": {"result": rows}})

    monkeypatch.setenv("BILIBILI_SEARCH_SLEEP_SECONDS", "0.1")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).search(
                parse_intent("咖啡店室内"), 75, media_type="images", image_limit=75,
            )

    result = asyncio.run(run())
    assert seen_pages == [1, 2]
    assert len(result) == 75


def test_bilibili_nickname_resolves_from_video_author_when_user_search_is_challenged():
    def handler(request):
        if request.url.path == "/x/web-interface/search/type":
            search_type = request.url.params.get("search_type")
            if search_type == "bili_user":
                return httpx.Response(200, json={"code": -352, "message": "风控校验失败"})
            return httpx.Response(200, json={"code": 0, "data": {"result": [
                {"bvid": "BV1author", "mid": 73, "author": "视频作者",
                 "pic": "//img.test/author.jpg", "title": "作者作品"},
            ]}})
        if request.url.path == "/x/web-interface/card":
            return httpx.Response(200, json={"code": 0, "data": {
                "card": {"mid": "73", "name": "视频作者"},
            }})
        return httpx.Response(404, json={"code": -404, "message": "missing"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await BilibiliApi(client).resolve_identity(CreatorFetchRequest(
                platform=Platform.BILIBILI,
                creator_name="视频作者",
                download=False,
            ))

    identity = asyncio.run(run())
    assert identity.canonical_id == "73"
    assert identity.matched_by == "video_author_name"


def test_weibo_creator_fetches_images_and_videos_with_since_cursor():
    seen = []
    def handler(request):
        seen.append(dict(request.url.params))
        container = request.url.params.get("containerid")
        if container == "1005055984743446":
            return httpx.Response(200, json={"ok": 1, "data": {"userInfo": {"idstr": "5984743446", "screen_name": "凌云Tiger1"}}})
        if container == "1076035984743446":
            return httpx.Response(200, json={"ok": 1, "data": {"cards": [
                {"card_type": 9, "mblog": {"id": "one", "user": {"idstr": "5984743446"}, "pics": [{"large": {"url": "https://wx.test/photo.jpg"}}]}},
                {"card_type": 9, "mblog": {"id": "two", "user": {"idstr": "5984743446"}, "page_info": {"page_pic": {"url": "https://wx.test/cover.jpg"}, "media_info": {"mp4_720p_mp4": "https://video.test/high.mp4", "mp4_sd_mp4": "https://video.test/low.mp4"}}}},
                {"card_type": 9, "mblog": {"id": "foreign", "user": {"idstr": "99"}, "pics": [{"url": "https://wx.test/foreign.jpg"}]}},
            ], "cardlistInfo": {"since_id": "12345"}}})
        return httpx.Response(404)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = CreatorFetchRequest(platform=Platform.WEIBO, profile_url="https://weibo.com/u/5984743446", media_type="all", download=False)
            return await WeiboApi(client).fetch_creator(request)
    result = asyncio.run(run())
    assert [(item.post_id, item.media_type) for item in result.items] == [("one", "image"), ("two", "video")]
    assert result.items[1].image_url == "https://video.test/high.mp4"
    assert result.rejected_posts == 1
    assert result.next_cursor == '{"native":"12345","offset":0}'
    assert seen[-1]["since_id"] == "0"


def test_weibo_432_is_reported_as_login_or_verification():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(432))) as client:
            with pytest.raises(WeiboError, match="HTTP 432.*login or verification"):
                await WeiboApi(client).resolve_identity(CreatorFetchRequest(platform=Platform.WEIBO, creator_name="凌云Tiger1", download=False))
    asyncio.run(run())


def test_weibo_login_html_is_not_parsed_as_creator_data():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={"content-type": "text/html"}, text="<html>login</html>"))) as client:
            with pytest.raises(WeiboError, match="non-JSON response"):
                await WeiboApi(client).resolve_identity(CreatorFetchRequest(platform=Platform.WEIBO, profile_url="https://weibo.com/u/5984743446", download=False))
    asyncio.run(run())
