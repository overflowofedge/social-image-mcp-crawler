import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from social_image_mcp.bridge_utils import extract_media_urls, extract_video_urls, normalize_native_record


_MEDIA_SPEC = importlib.util.spec_from_file_location(
    "media_crawler_bridge",
    Path(__file__).resolve().parents[1] / "scripts" / "media_crawler_bridge.py",
)
assert _MEDIA_SPEC and _MEDIA_SPEC.loader
_MEDIA_MODULE = importlib.util.module_from_spec(_MEDIA_SPEC)
_MEDIA_SPEC.loader.exec_module(_MEDIA_MODULE)


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
