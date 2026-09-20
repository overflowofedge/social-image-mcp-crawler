import asyncio
import json
import os
import sys

import pytest

from social_image_mcp.models import CreatorFetchRequest, Platform, SearchRequest
from social_image_mcp.sources import ExternalJsonSource, GalleryDlSource, SourceError, SourceHub, SourceVerificationStore, _decode_process_output, _embedded_error, normalize_source_output
from social_image_mcp.feedback import FeedbackStore
from social_image_mcp.intent import parse_intent
from social_image_mcp.models import ImageCandidate


def test_gallery_dl_json_line_is_normalized_to_candidate():
    output = '[3, "https://cdn.test/photo.jpg", {"id": "p1", "title": "coffee shop", "width": 1800, "height": 1200, "url": "https://x.com/i/status/1"}]\n'
    items = normalize_source_output(Platform.X, output, "gallery-dl", 5)
    assert len(items) == 1
    assert items[0].id == "p1"
    assert items[0].image_url.endswith("photo.jpg")
    assert items[0].source_payload["source"] == "gallery-dl"
    assert items[0].width == 1800


def test_gallery_dl_video_record_keeps_video_media_type():
    output = '[3, "https://video.twimg.com/test.mp4?tag=12", {"tweet_id":"42","extension":"mp4","width":1280,"height":720}]\n'
    items = normalize_source_output(Platform.X, output, "gallery-dl", 5)
    assert len(items) == 1
    assert items[0].media_type == "video"
    assert items[0].image_url.startswith("https://video.twimg.com/")


def test_normalized_video_keeps_its_type_without_a_file_extension():
    output = json.dumps({"id": "clip", "media_type": "video", "image_url": "https://cdn.test/play?id=1",
                         "thumbnail_url": "https://cdn.test/cover.jpg", "url": "https://site.test/post/1"})
    items = normalize_source_output(Platform.DOUYIN, output, "dy-cli", 10)
    assert [(item.media_type, item.image_url) for item in items] == [("video", "https://cdn.test/play?id=1")]
    assert items[0].thumbnail_url == "https://cdn.test/cover.jpg"


def test_source_pool_reserves_space_for_later_videos():
    output = json.dumps([
        {"id": "photo-1", "image_url": "https://cdn.test/1.jpg"},
        {"id": "photo-2", "image_url": "https://cdn.test/2.jpg"},
        {"id": "clip", "media_type": "video", "image_url": "https://cdn.test/play?id=1"},
    ])
    assert [item.id for item in normalize_source_output(Platform.DOUYIN, output, "dy-cli", 1, "all")] == ["photo-1", "clip"]
    assert [item.id for item in normalize_source_output(Platform.DOUYIN, output, "dy-cli", 1, "videos")] == ["clip"]


def test_source_hub_passes_mixed_request_to_the_bridge(tmp_path):
    script = tmp_path / "source.py"
    script.write_text('''import json, os
request = json.loads(os.environ["SOCIAL_IMAGE_SEARCH_REQUEST"])
assert request["media_type"] == "all"
assert request["max_posts"] == 17
assert request["video_limit"] == 2
print(json.dumps({"id":"v1", "media_type":"video", "image_url":"https://cdn.test/play?id=1"}))
''', encoding="utf-8")

    async def run():
        source = SourceHub(None, None, "not-installed-gallery", None,
                           douyin_source_command=f'"{sys.executable}" "{script}"',
                           verification_path=str(tmp_path / "verification.json"))
        request = SearchRequest(query="https://v.douyin.com/home/", media_type="all", max_posts=17, video_limit=2)
        items = await source.search(Platform.DOUYIN, parse_intent(request.query), 4, request=request)
        assert [(item.id, item.media_type) for item in items] == [("v1", "video")]

    asyncio.run(run())


def test_source_process_output_decodes_utf8_and_windows_gb18030():
    assert _decode_process_output("微博账号不存在".encode("utf-8")) == "微博账号不存在"
    assert _decode_process_output("微博账号不存在".encode("gb18030")) == "微博账号不存在"


def test_gallery_dl_directory_and_queue_messages_are_not_images():
    output = (
        '[2, {"id": "post", "display_url": "https://cdn.test/not-yet.jpg"}]\n'
        '[6, "https://x.com/user/posts", {"id": "queue"}]\n'
        '[3, "https://cdn.test/real.jpg", {"tweet_id": "42", "content": "coffee shop"}]\n'
    )

    items = normalize_source_output(Platform.X, output, "gallery-dl", 5)

    assert len(items) == 1
    assert items[0].id == "42"
    assert items[0].image_url == "https://cdn.test/real.jpg"
    assert items[0].title == "coffee shop"


def test_source_hub_reports_recommended_projects_without_platform_credentials():
    statuses = SourceHub(None, None, "definitely-not-installed-gallery-dl", None).statuses()
    names = {status["name"] for status in statuses}
    assert names == {"dy-cli", "media-crawler", "xhs-downloader", "gallery-dl"}
    assert all(status["configured"] is False for status in statuses)
    assert all(status["verified"] is False for status in statuses)


def test_feedback_store_changes_candidate_bias(tmp_path):
    store = FeedbackStore(str(tmp_path / "feedback.sqlite3"))
    intent = parse_intent("咖啡店")
    item = ImageCandidate(id="p1", platform=Platform.X, image_url="https://cdn.test/p1.jpg")
    store.record(intent, item, True)
    assert store.bias(intent, item) > 0
    store.record(intent, item, False)
    assert store.bias(intent, item) < 0


def test_external_source_command_contract(tmp_path):
    script = tmp_path / "source.py"
    script.write_text("import json; print(json.dumps({'id': 'n1', 'image_url': 'https://cdn.test/n1.jpg', 'title': 'coffee shop', 'width': 1200, 'height': 800}))", encoding="utf-8")

    async def run():
        command = f'"{sys.executable}" "{script}"'
        source = ExternalJsonSource("media-crawler", command, (Platform.XHS,))
        items = await source.search(Platform.XHS, parse_intent("咖啡店"), 5)
        assert len(items) == 1
        assert items[0].source_payload["source"] == "media-crawler"
        assert items[0].width == 1200

    asyncio.run(run())


def test_external_source_empty_json_is_not_marked_verified(tmp_path):
    script = tmp_path / "empty_source.py"
    script.write_text("print('{}')", encoding="utf-8")

    async def run():
        command = f'"{sys.executable}" "{script}"'
        source = ExternalJsonSource("media-crawler", command, (Platform.WEIBO,))
        try:
            await source.search(Platform.WEIBO, parse_intent("咖啡"), 5)
        except Exception as exc:
            assert "no image candidates" in str(exc)
        else:
            raise AssertionError("empty output must not verify a source")
        assert source.status.verified is False

    asyncio.run(run())


def test_gallery_cli_structured_auth_error_is_not_silently_empty():
    assert _embedded_error([[ -1, {"error": "AuthRequired", "message": "cookies needed"} ]]) == "AuthRequired: cookies needed"


def test_gallery_source_uses_bounded_native_search_targets():
    source = GalleryDlSource("gallery-dl", cookies_from_browser="edge")

    x_command = source._command(Platform.X, parse_intent("咖啡店室内"), 7)
    assert x_command[x_command.index("--range") + 1] == "1-7"
    assert "output.jsonl=true" in x_command
    assert "filter%3Amedia" in x_command[-1]
    assert x_command[-1].startswith("https://x.com/search?")

    instagram_targets = source._targets(Platform.INSTAGRAM, parse_intent("咖啡店室内，横图，不要水印"))
    assert instagram_targets == [
        "https://www.instagram.com/explore/tags/coffeeshop/",
        "https://www.instagram.com/explore/tags/interior/",
    ]

    weibo_target = source._target(Platform.WEIBO, parse_intent("weibo:Mx123"))
    assert weibo_target == "https://weibo.com/detail/Mx123"

    file_source = GalleryDlSource("gallery-dl", cookies_from_browser="edge", cookies_file=".cache/session.txt")
    file_command = file_source._command(Platform.X, parse_intent("x:123456789"), 1)
    assert "--cookies" in file_command
    assert "--cookies-from-browser" not in file_command


def test_source_hub_skips_gallery_dl_for_weibo_keyword_search():
    async def run():
        hub = SourceHub(None, None, "gallery-dl", None)
        try:
            await hub.search(Platform.WEIBO, parse_intent("咖啡"), 1)
        except Exception as exc:
            assert "No recommended source configured" in str(exc)
        else:
            raise AssertionError("gallery-dl must not be used for Weibo keyword search")

    asyncio.run(run())


def test_source_hub_does_not_chain_douyin_fallback_by_default():
    async def run():
        hub = SourceHub("media-crawler", None, "gallery-dl", None, douyin_source_command="dy-cli")
        expected = object()

        async def dy_fetch(request):
            return expected

        async def media_fetch(request):
            raise AssertionError("MediaCrawler fallback must be opt-in when dy-cli is configured")

        hub.douyin_source.fetch_creator = dy_fetch
        hub.media_crawler.fetch_creator = media_fetch
        result = await hub.fetch_creator(CreatorFetchRequest(platform=Platform.DOUYIN, creator_id="Gracebb0722"))
        assert result is expected

    asyncio.run(run())


def test_failed_source_is_temporarily_skipped_without_restarting(tmp_path):
    counter = tmp_path / "starts.txt"
    script = tmp_path / "failing.py"
    script.write_text(
        "from pathlib import Path\n"
        f"p=Path({str(counter)!r})\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )

    async def run():
        source = ExternalJsonSource(
            "failing-source",
            f'"{sys.executable}" "{script}"',
            (Platform.XHS, Platform.WEIBO),
            failure_cooldown_seconds=120,
        )
        for _ in range(2):
            try:
                await source.search(Platform.XHS, parse_intent("咖啡店"), 1)
            except Exception:
                pass
        assert counter.read_text() == "x"
        # Cooldowns are per platform, so a different platform still starts.
        try:
            await source.search(Platform.WEIBO, parse_intent("咖啡店"), 1)
        except Exception:
            pass
        assert counter.read_text() == "xx"

    asyncio.run(run())


def test_verification_evidence_survives_restart_and_tracks_latest_failure(tmp_path):
    path = tmp_path / "verification.json"
    first = SourceVerificationStore(path, ttl_seconds=3600)
    first.record_success("dy-cli", "douyin")

    restarted = SourceVerificationStore(path, ttl_seconds=3600)
    status = restarted.status("dy-cli", "douyin")
    assert status["verified"] is True
    assert status["ready"] is True
    assert status["last_verified_at"]

    restarted.record_failure("dy-cli", "douyin", "verify_check")
    latest = SourceVerificationStore(path, ttl_seconds=3600).status("dy-cli", "douyin")
    assert latest["verified"] is True
    assert latest["ready"] is False
    assert latest["last_result"] == "failure"
    assert latest["last_error"] == "verify_check"


def test_verification_write_failure_does_not_fail_a_successful_source_result(tmp_path, monkeypatch):
    path = tmp_path / "verification.json"
    store = SourceVerificationStore(path, ttl_seconds=3600)

    def fail_replace(source, target):
        raise OSError("verification directory is temporarily locked")

    monkeypatch.setattr(os, "replace", fail_replace)
    store.record_success("dy-cli", "douyin")

    assert store.status("dy-cli", "douyin")["ready"] is True
    assert not path.with_suffix(".json.tmp").exists()


def test_source_detail_uses_persisted_failure_after_restart(tmp_path):
    path = tmp_path / "verification.json"
    SourceVerificationStore(path, ttl_seconds=3600).record_failure(
        "dy-cli", "douyin", "verify_check detected"
    )

    restarted = ExternalJsonSource(
        "dy-cli",
        f'"{sys.executable}" -c "print(1)"',
        (Platform.DOUYIN,),
        verification_store=SourceVerificationStore(path, ttl_seconds=3600),
    )

    assert restarted.status.verified is False
    assert restarted.status.detail == "Last request failed: verify_check detected"


def test_creator_source_records_success_and_failure(tmp_path):
    success = tmp_path / "creator_success.py"
    success.write_text(
        "import json\n"
        "print(json.dumps({'identity': {'requested_id': '1', 'canonical_id': '1', 'name': 'demo', 'matched_by': 'exact_uid'}, 'items': [], 'posts_fetched': 0, 'pages_fetched': 0}))\n",
        encoding="utf-8",
    )
    failure = tmp_path / "creator_failure.py"
    failure.write_text("raise SystemExit(2)\n", encoding="utf-8")

    async def run():
        path = tmp_path / "verification.json"
        store = SourceVerificationStore(path, ttl_seconds=3600)
        request = CreatorFetchRequest(platform=Platform.WEIBO, creator_id="1", download=False)
        good = ExternalJsonSource("creator-good", f'"{sys.executable}" "{success}"', (Platform.WEIBO,), failure_cooldown_seconds=0, verification_store=store)
        result = await good.fetch_creator(request)
        assert result.identity.canonical_id == "1"
        assert good.status.ready_platforms == ("weibo",)

        bad = ExternalJsonSource("creator-bad", f'"{sys.executable}" "{failure}"', (Platform.WEIBO,), failure_cooldown_seconds=0, verification_store=store)
        with pytest.raises(SourceError):
            await bad.fetch_creator(request)
        assert bad.status.ready_platforms == ()
        assert bad.status.platform_status["weibo"]["last_result"] == "failure"

    asyncio.run(run())


def test_creator_source_records_malformed_envelope_failure(tmp_path):
    malformed = tmp_path / "creator_malformed.py"
    malformed.write_text("print('not-json')\n", encoding="utf-8")

    async def run():
        source = ExternalJsonSource(
            "creator-malformed", f'"{sys.executable}" "{malformed}"',
            (Platform.WEIBO,), failure_cooldown_seconds=120,
        )
        with pytest.raises(SourceError, match="identity envelope"):
            await source.fetch_creator(CreatorFetchRequest(platform=Platform.WEIBO, creator_id="1", download=False))
        status = source.status.platform_status["weibo"]
        assert status["last_result"] == "failure"
        assert "identity envelope" in status["last_error"]

    asyncio.run(run())
