import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "douyin_cli_bridge",
    Path(__file__).resolve().parents[1] / "scripts" / "douyin_cli_bridge.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_records = _MODULE._records
_configure_stdio = _MODULE._configure_stdio
_raise_for_empty_search = _MODULE._raise_for_empty_search
_exact_record = _MODULE._exact_record
_load_cached_items = _MODULE._load_cached_items
_save_cached_items = _MODULE._save_cached_items
_cache_path = _MODULE._cache_path
_browser_endpoint = _MODULE._browser_endpoint
_browser_profile = _MODULE._browser_profile
_browser_user_matches = _MODULE._browser_user_matches
_browser_post_cursor = _MODULE._browser_post_cursor
_browser_items_from_payload = _MODULE._browser_items_from_payload
_looks_like_douyin_creator_url = _MODULE._looks_like_douyin_creator_url
_creator_items_from_profile_share = _MODULE._creator_items_from_profile_share
_fetch_creator_via_browser = _MODULE._fetch_creator_via_browser


def test_dy_cli_search_records_extract_aweme_info():
    payload = {"data": [{"aweme_info": {"aweme_id": "123", "images": []}}, {"other": True}]}
    assert _records(payload) == [{"aweme_id": "123", "images": []}]


def test_dy_cli_bridge_configures_utf8_stdio():
    _configure_stdio()


def test_dy_cli_verify_check_is_reported_as_an_error():
    import pytest

    with pytest.raises(RuntimeError, match="verify_check"):
        _raise_for_empty_search({"search_nil_info": {"search_nil_type": "verify_check"}})


def test_dy_cli_id_fallback_requires_exact_aweme_id():
    records = [{"aweme_id": "123"}, {"aweme_id": "1234"}]
    assert _exact_record(records, "123") == records[0]
    assert _exact_record(records, "999") is None


def test_dy_cli_cache_reuses_only_recent_exact_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("DY_CLI_RESULT_CACHE", str(tmp_path / "results.json"))
    items = [{"id": "123:1", "post_id": "123", "image_url": "https://img.test/a.jpg"}]
    _save_cached_items(items)
    assert _load_cached_items("123") == items
    assert _load_cached_items("1234") == []


def test_dy_cli_relative_cache_is_anchored_to_project(monkeypatch):
    monkeypatch.setenv("DY_CLI_RESULT_CACHE", ".cache/custom-results.json")
    assert _cache_path() == Path(__file__).resolve().parents[1] / ".cache" / "custom-results.json"


def test_douyin_creator_url_detection_distinguishes_profile_paths():
    assert _looks_like_douyin_creator_url("https://www.douyin.com/user/sec-1")
    assert _looks_like_douyin_creator_url("https://v.douyin.com/abc/") is False
    assert _looks_like_douyin_creator_url("https://www.douyin.com/video/123") is False


def test_douyin_profile_share_is_resolved_before_creator_collection(monkeypatch):
    class FakeClient:
        def resolve_creator_share_url(self, url):
            assert url == "https://v.douyin.com/home/"
            return "https://www.douyin.com/user/sec-1"

    candidate = {"id": "post:1", "image_url": "https://img.test/1.jpg"}
    monkeypatch.setattr(_MODULE, "_fetch_creator_with_fallback", lambda client, request, account=None: {"items": [candidate]})
    assert _creator_items_from_profile_share(FakeClient(), "https://v.douyin.com/home/", 3) == [candidate]


def test_browser_response_helpers_extract_profile_and_endpoint():
    profile = {"user": {"sec_uid": "sec-1", "nickname": "放学小野猪", "unique_id": "Gracebb0722"}}
    assert _browser_endpoint("https://www.douyin.com/aweme/v1/web/user/profile/other/?sec_user_id=sec-1") == "profile"
    assert _browser_profile(profile) == profile["user"]
    assert _browser_profile({"data": [{"user_info": profile["user"]}]}) == profile["user"]


def test_browser_user_search_requires_exact_account_id_and_dedupes_sec_uid():
    payload = {
        "data": [
            {"user_info": {"sec_uid": "sec-1", "unique_id": "Gracebb0722"}},
            {"user_info": {"sec_uid": "sec-1", "unique_id": "Gracebb0722"}},
            {"user_info": {"sec_uid": "sec-2", "unique_id": "Gracebb07220"}},
        ]
    }
    assert _browser_user_matches(payload, "Gracebb0722") == [payload["data"][0]["user_info"]]


def test_browser_user_search_matches_exact_nickname():
    payload = {"data": [
        {"user_info": {"sec_uid": "sec-1", "nickname": "放学小野猪"}},
        {"user_info": {"sec_uid": "sec-2", "nickname": "放学小野猪2"}},
    ]}
    assert _browser_user_matches(payload, "放学小野猪", nickname=True) == [payload["data"][0]["user_info"]]


def test_browser_post_cursor_normalizes_string_booleans_and_query_cursor():
    url = "https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id=sec-1&max_cursor=18"
    assert _browser_endpoint(url) == "posts"
    assert _browser_post_cursor(url, {"max_cursor": 36, "has_more": "0"}) == ("36", False, "18")
    assert _browser_post_cursor(url, {"maxCursor": "36", "hasMore": "true"}) == ("36", True, "18")


def test_browser_post_response_uses_collector_identity_filter():
    from social_image_mcp.models import CreatorFetchRequest, CreatorIdentity, Platform
    from social_image_mcp.creator_protocol import CreatorCollector

    identity = CreatorIdentity(
        platform=Platform.DOUYIN,
        requested_id="Gracebb0722",
        canonical_id="sec-1",
        name="放学小野猪",
        source="dy-cli-browser",
        matched_by="exact_account_id",
    )
    request = CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722", max_posts=3, max_images=10, download=False)
    collector = CreatorCollector(identity, request)
    payload = {
        "aweme_list": [
            {"aweme_id": "p1", "author": {"sec_uid": "sec-1"}, "images": [{"url_list": ["https://img.test/1.jpg"]}]},
            {"aweme_id": "foreign", "author": {"sec_uid": "sec-other"}, "images": [{"url_list": ["https://img.test/foreign.jpg"]}]},
        ],
        "max_cursor": 18,
        "has_more": "true",
    }
    response_url = "https://www.douyin.com/aweme/v1/web/aweme/post/?sec_user_id=sec-1&max_cursor=0"
    assert _browser_items_from_payload(payload, identity, request, collector, response_url) is True
    result = collector.result()
    assert result["post_ids"] == ["p1"]
    assert result["rejected_posts"] == 1
    assert result["next_cursor"] == '{"native":"18","offset":0}'


def test_browser_post_response_ignores_wrong_resume_cursor():
    from social_image_mcp.models import CreatorFetchRequest, CreatorIdentity, Platform
    from social_image_mcp.creator_protocol import CreatorCollector

    identity = CreatorIdentity(platform=Platform.DOUYIN, requested_id="sec-1", canonical_id="sec-1", source="test", matched_by="sec_uid")
    request = CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="sec-1", cursor='{"native":"18","offset":0}', download=False)
    collector = CreatorCollector(identity, request)
    payload = {"aweme_list": [], "max_cursor": 36, "has_more": False}
    assert _browser_items_from_payload(payload, identity, request, collector, "https://www.douyin.com/aweme/v1/web/aweme/post/?max_cursor=0") is False
    assert collector.pages_fetched == 0
