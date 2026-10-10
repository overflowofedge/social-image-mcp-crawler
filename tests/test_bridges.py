import argparse
import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from social_image_mcp.bridge_utils import extract_media_urls, extract_video_urls, normalize_native_record
from social_image_mcp.models import CreatorFetchRequest, Platform


_MEDIA_SPEC = importlib.util.spec_from_file_location(
    "media_crawler_bridge",
    Path(__file__).resolve().parents[1] / "scripts" / "media_crawler_bridge.py",
)
assert _MEDIA_SPEC and _MEDIA_SPEC.loader
_MEDIA_MODULE = importlib.util.module_from_spec(_MEDIA_SPEC)
_MEDIA_SPEC.loader.exec_module(_MEDIA_MODULE)

_XHS_SPEC = importlib.util.spec_from_file_location(
    "xhs_downloader_bridge",
    Path(__file__).resolve().parents[1] / "scripts" / "xhs_downloader_bridge.py",
)
assert _XHS_SPEC and _XHS_SPEC.loader
_XHS_MODULE = importlib.util.module_from_spec(_XHS_SPEC)
_XHS_SPEC.loader.exec_module(_XHS_MODULE)


def test_xhs_native_record_prefers_original_image_urls():
    record = {
        "note_id": "n1",
        "title": "咖啡店室内",
        "user": {"nickname": "作者"},
        "image_list": [
            {"url_default": "https://img.test/original.jpg", "url": "https://img.test/fallback.jpg"}
        ],
    }
    items = normalize_native_record("xhs", record, "media-crawler")
    assert len(items) == 1
    assert items[0]["id"] == "n1"
    assert items[0]["image_url"] == "https://img.test/original.jpg"
    assert items[0]["author"] == "作者"


def test_xhs_native_record_extracts_original_video_key():
    record = {
        "note_id": "video-1",
        "type": "video",
        "video": {"consumer": {"origin_video_key": "path/original.mp4"}},
        "image_list": [{"url_default": "https://img.test/cover.jpg"}],
    }

    items = normalize_native_record("xhs", record, "media-crawler", media_type="videos")

    assert len(items) == 1
    assert items[0]["media_type"] == "video"
    assert items[0]["image_url"] == "https://sns-video-bd.xhscdn.com/path/original.mp4"
    assert items[0]["thumbnail_url"] == "https://img.test/cover.jpg"


def test_xhs_video_fallback_selects_best_non_hdr_stream():
    record = {
        "type": "video",
        "video": {"media": {"stream": {"h264": [
            {"master_url": "https://video.test/720.mp4", "width": 1280, "height": 720, "video_bitrate": 800},
            {"master_url": "https://video.test/1080.mp4", "width": 1920, "height": 1080, "video_bitrate": 1800},
        ]}}},
    }

    assert extract_video_urls("xhs", record) == ["https://video.test/1080.mp4"]


def test_douyin_native_record_extracts_gallery_and_cover():
    record = {
        "aweme_id": "42",
        "desc": "咖啡店",
        "images": [{"url_list": ["https://img.test/a.jpg", "https://img.test/b.jpg"]}],
    }
    # url_list is a set of CDN variants for one image; choose the highest/final variant.
    assert extract_media_urls("douyin", record) == ["https://img.test/b.jpg"]

    cover = {"aweme_id": "43", "video": {"raw_cover": {"url_list": ["https://img.test/cover.jpg"]}}}
    assert extract_media_urls("douyin", cover) == ["https://img.test/cover.jpg"]


def test_douyin_video_uses_one_best_cover_instead_of_duplicate_variants():
    record = {
        "video": {
            "raw_cover": {"url_list": ["https://img.test/raw.jpg"]},
            "origin_cover": {"url_list": ["https://img.test/low.jpg"]},
            "dynamic_cover": {"url_list": ["https://img.test/dynamic.jpg"]},
        }
    }

    assert extract_media_urls("douyin", record) == ["https://img.test/raw.jpg"]


def test_douyin_native_record_accepts_image_list_alias():
    record = {"aweme_id": "2", "image_list": [{"url_list": ["https://img.test/alias.jpg"]}]}
    assert extract_media_urls("douyin", record) == ["https://img.test/alias.jpg"]


def test_douyin_native_record_extracts_original_video_when_requested():
    record = {"aweme_id": "44", "video": {"play_addr": {"url_list": ["https://cdn.test/video.mp4"]}}}
    assert extract_video_urls("douyin", record) == ["https://cdn.test/video.mp4"]
    items = normalize_native_record("douyin", record, "dy-cli", media_type="videos")
    assert items[0]["media_type"] == "video"
    assert items[0]["image_url"].endswith("video.mp4")


def test_douyin_media_type_all_keeps_image_and_video_records():
    record = {
        "aweme_id": "45",
        "images": [{"url_list": ["https://img.test/a.jpg"]}],
        "video": {"play_addr": {"url_list": ["https://cdn.test/video.mp4"]}},
    }
    items = normalize_native_record("douyin", record, "dy-cli", media_type="all")
    assert {item["media_type"] for item in items} == {"image", "video"}


def test_douyin_video_extraction_drops_music_track_urls():
    record = {"aweme_id": "45", "video": {
        "play_addr": {"url_list": ["https://cdn.test/audio.mp3"]},
        "bit_rate": [{"play_addr": {"url_list": ["https://cdn.test/video?id=45"]}}],
    }}
    assert extract_video_urls("douyin", record) == ["https://cdn.test/video?id=45"]
    del record["video"]["bit_rate"]
    assert extract_video_urls("douyin", record) == []


def test_douyin_video_prefers_highest_non_hdr_hevc_variant():
    record = {"video": {
        "width": 7680,
        "height": 4320,
        "bit_rate": [
            {"gear_name": "8k_hdr_hevc", "width": 7680, "height": 4320, "codec_type": "h265", "hdr_type": 1,
             "bit_rate": 90000000, "play_addr": {"url_list": ["https://cdn.test/8k-hdr-hevc.mp4"]}},
            {"gear_name": "4k_h264", "width": 3840, "height": 2160, "codec_type": "h264",
             "bit_rate": 60000000, "play_addr": {"url_list": ["https://cdn.test/4k-h264.mp4"]}},
            {"gear_name": "4k_hevc", "width": 3840, "height": 2160, "codec_type": "h265",
             "bit_rate": 50000000, "play_addr": {"url_list": ["https://cdn.test/4k-hevc.mp4"]}},
            {"gear_name": "1080p_hevc", "width": 1920, "height": 1080, "codec_type": "h265",
             "bit_rate": 20000000, "play_addr": {"url_list": ["https://cdn.test/1080p-hevc.mp4"]}},
        ],
    }}
    assert extract_video_urls("douyin", record) == ["https://cdn.test/4k-hevc.mp4"]


def test_weibo_video_prefers_original_non_hdr_hevc_variant():
    record = {"mblog": {"page_info": {"media_info": {
        "mp4_8k_hdr_hevc": "https://video.test/8k-hdr-hevc.mp4",
        "mp4_4k_h264": "https://video.test/4k-h264.mp4",
        "mp4_4k_hevc": "https://video.test/4k-hevc.mp4",
        "mp4_1080p_hevc": "https://video.test/1080p-hevc.mp4",
    }}}}
    assert extract_video_urls("weibo", record) == ["https://video.test/4k-hevc.mp4"]


def test_native_images_prefer_original_quality_field():
    record = {"note_id": "n1", "image_list": [{
        "url_default": "https://img.test/default.jpg",
        "original_url": "https://img.test/original.jpg",
    }]}
    items = normalize_native_record("xhs", record, "media-crawler")
    assert items[0]["image_url"] == "https://img.test/original.jpg"


def test_gallery_candidates_have_unique_ids_and_keep_post_id():
    record = {
        "aweme_id": "42",
        "images": [
            {"url_list": ["https://img.test/a.jpg"]},
            {"url_list": ["https://img.test/b.jpg"]},
        ],
    }
    items = normalize_native_record("douyin", record, "dy-cli")
    assert [item["id"] for item in items] == ["42:1", "42:2"]
    assert [item["post_id"] for item in items] == ["42", "42"]


def test_weibo_native_record_extracts_pic_urls_and_strips_html():
    record = {
        "mblog": {
            "id": "w1",
            "text": "咖啡店<br/>室内",
            "user": {"screen_name": "博主"},
            "pics": [{"url": "https://wx.test/pic.jpg", "pid": "p1"}],
        }
    }
    items = normalize_native_record("weibo", record, "media-crawler")
    assert items[0]["id"] == "w1"
    assert items[0]["title"] == "咖啡店室内"
    assert items[0]["image_url"] == "https://wx.test/pic.jpg"


def test_weibo_bridge_resolves_exact_nickname_and_keeps_video_only_post():
    class Client:
        async def get(self, path, params):
            return {"cards": [{"user": {"idstr": "5984743446", "screen_name": "凌云Tiger1"}}]}
        async def get_creator_info_by_id(self, creator_id):
            return {"userInfo": {"idstr": creator_id, "screen_name": "凌云Tiger1"}}
        async def get_notes_by_creator(self, creator, container, since):
            return {"cards": [{"card_type": 9, "mblog": {"id": "v1", "user": {"idstr": creator}, "page_info": {"media_info": {"mp4_hd_mp4": "https://video.test/v1.mp4"}}}}], "cardlistInfo": {"since_id": 0}}
    request = CreatorFetchRequest(platform=Platform.WEIBO, creator_name="凌云Tiger1", media_type="videos", download=False)
    result = asyncio.run(_MEDIA_MODULE._fetch_weibo_creator(Client(), request))
    assert result["identity"]["canonical_id"] == "5984743446"
    assert result["identity"]["matched_by"] == "exact_account_name"
    assert [(item["post_id"], item["media_type"]) for item in result["items"]] == [("v1", "video")]


def test_weibo_bridge_session_probe_uses_creator_endpoint_instead_of_api_config():
    class Client:
        async def get_creator_info_by_id(self, creator_id):
            return {"userInfo": {"idstr": creator_id}}

    request = CreatorFetchRequest(platform=Platform.WEIBO, profile_url="https://weibo.com/u/5984743446", download=False)
    assert asyncio.run(_MEDIA_MODULE._weibo_creator_session_ready(Client(), request)) is True


def test_weibo_bridge_search_probe_uses_search_response_instead_of_api_config():
    class Client:
        async def get(self, path, params):
            assert path == "/api/container/getIndex"
            return {"cards": []}

    assert asyncio.run(_MEDIA_MODULE._weibo_search_session_ready(Client(), "咖啡")) is True


def test_media_bridge_missing_vendor_is_a_clear_error(tmp_path: Path):
    script = Path(__file__).parents[1] / "scripts" / "media_crawler_bridge.py"
    result = subprocess.run(
        [sys.executable, str(script), "--platform", "xhs", "--query", "test", "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "main.py not found" in result.stderr
    assert result.stdout == ""


def test_media_bridge_douyin_capture_accepts_named_aweme_item(monkeypatch):
    store = ModuleType("store")
    douyin = ModuleType("store.douyin")
    store.douyin = douyin
    monkeypatch.setitem(sys.modules, "store", store)
    monkeypatch.setitem(sys.modules, "store.douyin", douyin)
    records = []

    _MEDIA_MODULE._install_capture("douyin", records)
    asyncio.run(douyin.update_douyin_aweme(aweme_item={"aweme_id": "42"}))

    assert records == [{"aweme_id": "42"}]


def test_media_bridge_uses_explicit_browser_in_native_cdp_mode(tmp_path: Path):
    browser = tmp_path / "msedge.exe"
    browser.write_bytes(b"")
    config = SimpleNamespace()
    args = argparse.Namespace(browser_path=str(browser), headless="false")

    _MEDIA_MODULE._configure_vendor_browser(config, args)

    assert config.ENABLE_CDP_MODE is True
    assert config.CDP_CONNECT_EXISTING is False
    assert config.CUSTOM_BROWSER_PATH == str(browser.resolve())
    assert config.CDP_HEADLESS is False
    assert config.AUTO_CLOSE_BROWSER is True
    assert config.SAVE_LOGIN_STATE is True


def test_weibo_bridge_imports_desktop_session_before_building_native_client(monkeypatch, tmp_path):
    from social_image_mcp.accounts import save_session
    monkeypatch.setenv("WEIBO_BROWSER_STORAGE_STATE", str(tmp_path / "session.json"))
    save_session("weibo", {"cookies": [{"name": "SUB", "value": "new-login", "domain": ".weibo.cn", "path": "/", "expires": -1}]}, tmp_path)
    (tmp_path / "main.py").write_text("")
    seen = []

    class Context:
        async def add_cookies(self, cookies):
            seen.extend(cookies)

    class Crawler:
        browser_context = Context()
        async def create_weibo_client(self, *_args):
            assert seen and seen[0]["value"] == "new-login"
            return "client"

    class Client:
        async def pong(self):
            return True

    original = Crawler.create_weibo_client
    core = ModuleType("media_platform.weibo.core")
    core.WeiboCrawler = Crawler
    client = ModuleType("media_platform.weibo.client")
    client.WeiboClient = Client
    main = ModuleType("main")

    async def run():
        assert await Crawler().create_weibo_client(None) == "client"

    async def cleanup():
        pass

    main.main, main.async_cleanup = run, cleanup
    for name, module in (("config", ModuleType("config")), ("main", main), (core.__name__, core), (client.__name__, client)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(_MEDIA_MODULE, "_configure_vendor_browser", lambda *_: None)
    monkeypatch.setattr(_MEDIA_MODULE, "_install_capture", lambda *_: None)
    monkeypatch.delenv("SOCIAL_IMAGE_CREATOR_REQUEST", raising=False)
    monkeypatch.delenv("SOCIAL_IMAGE_SEARCH_REQUEST", raising=False)
    args = _MEDIA_MODULE._parser().parse_args(["--platform", "weibo", "--query", "test", "--root", str(tmp_path)])
    assert asyncio.run(_MEDIA_MODULE._run(args)) == []
    assert Crawler.create_weibo_client is original


def test_xhs_bridge_uses_saved_login_in_metadata_client(monkeypatch, tmp_path):
    from social_image_mcp.accounts import save_session
    monkeypatch.setenv("XHS_BROWSER_STORAGE_STATE", str(tmp_path / "session.json"))
    save_session("xhs", {"cookies": [{"name": "web_session", "value": "saved-account",
                 "domain": ".xiaohongshu.com", "path": "/", "expires": -1}]}, tmp_path)
    (tmp_path / "source").mkdir()
    seen = []

    class Settings:
        def __init__(self, **kwargs):
            pass
        def run(self):
            return {"cookie": "legacy-cookie"}

    class XHS:
        def __init__(self, **options):
            seen.append(options)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def extract(self, url, **kwargs):
            assert kwargs == {"download": False, "check_record": False}
            return [{"作品ID": "note", "下载地址": ["https://sns-img.xhscdn.com/photo.jpg"]}]

    module = ModuleType("source")
    module.XHS, module.Settings = XHS, Settings
    monkeypatch.setitem(sys.modules, "source", module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(_XHS_MODULE, "_prepare_vendor_imports", lambda: None)
    args = _XHS_MODULE._parser().parse_args(["--url", "https://www.xiaohongshu.com/explore/note", "--root", str(tmp_path)])
    items = asyncio.run(_XHS_MODULE._run(args))
    assert len(items) == 1
    assert seen[0]["cookie"] == "web_session=saved-account"
    assert seen[0]["image_download"] is False and seen[0]["video_download"] is False


def test_xhs_bridge_keyword_mode_is_empty_without_importing_vendor():
    script = Path(__file__).parents[1] / "scripts" / "xhs_downloader_bridge.py"
    result = subprocess.run(
        [sys.executable, str(script), "--query", "咖啡店", "--root", str(Path("does-not-exist"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_xhs_downloader_bridge_labels_video_download_urls():
    items = _XHS_MODULE._normalize_results([{
        "作品ID": "video-note",
        "作品类型": "视频",
        "作品标题": "测试视频",
        "下载地址": ["https://sns-video-bd.xhscdn.com/test-video"],
    }], "", "https://www.xiaohongshu.com/explore/video-note", 1)

    assert items == [{
        "id": "video-note",
        "image_url": "https://sns-video-bd.xhscdn.com/test-video",
        "media_type": "video",
        "title": "测试视频",
        "description": "",
        "author": "",
        "permalink": "https://www.xiaohongshu.com/explore/video-note",
        "source": "xhs-downloader",
    }]
