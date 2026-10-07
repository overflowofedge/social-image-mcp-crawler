from __future__ import annotations

"""Run MediaCrawler as a metadata-only JSON stdout source.

The vendor project owns browser/login/session handling.  This bridge replaces its
storage callbacks with in-memory capture callbacks, so MediaCrawler does not write
or download media; this MCP service remains the only downloader.
"""

import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from social_image_mcp.bridge_utils import normalize_native_record
from social_image_mcp.creator_protocol import CreatorCollector, creator_target
from social_image_mcp.models import CreatorFetchRequest, CreatorIdentity, Platform, SearchRequest

DY_ROOT = Path(os.getenv("DY_CLI_ROOT") or ROOT / "third_party" / "dy-cli").expanduser().resolve()
DY_SRC = DY_ROOT / "src"
if str(DY_SRC) not in sys.path:
    sys.path.insert(0, str(DY_SRC))


def _configure_stdio() -> None:
    """Keep JSON output valid on Windows consoles using a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


PLATFORM_MAP = {"douyin": "dy", "dy": "dy", "xhs": "xhs", "xiaohongshu": "xhs", "weibo": "wb", "wb": "wb"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MediaCrawler JSON bridge")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--query", default="")
    parser.add_argument("--item-id", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--root", default=os.getenv("MEDIA_CRAWLER_ROOT") or str(ROOT / "third_party" / "MediaCrawler"))
    parser.add_argument("--login-type", default=os.getenv("MEDIA_CRAWLER_LOGIN_TYPE", "qrcode"))
    parser.add_argument("--headless", default=os.getenv("MEDIA_CRAWLER_HEADLESS", "false"))
    parser.add_argument("--browser-path", default=os.getenv("MEDIA_CRAWLER_BROWSER_PATH", ""))
    return parser


def _resolve_browser_path(value: str = "") -> str:
    """Return a usable Chrome-family browser without assuming Chrome exists."""
    candidates = [Path(value).expanduser()] if value else []
    if sys.platform == "win32":
        candidates.extend([
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        ])
    return str(next((path.resolve() for path in candidates if path.is_file()), ""))


def _configure_vendor_browser(config: ModuleType, args: argparse.Namespace) -> None:
    """Use MediaCrawler's native CDP launcher with an isolated saved profile."""
    browser_path = _resolve_browser_path(args.browser_path)
    if not browser_path:
        raise RuntimeError(
            "No installed Edge or Chrome browser was found; set MEDIA_CRAWLER_BROWSER_PATH"
        )
    headless = str(args.headless).lower() in {"1", "true", "yes", "y"}
    config.ENABLE_CDP_MODE = True
    config.CDP_CONNECT_EXISTING = False
    config.CUSTOM_BROWSER_PATH = browser_path
    config.CDP_HEADLESS = headless
    config.AUTO_CLOSE_BROWSER = True
    config.SAVE_LOGIN_STATE = True


def _install_capture(platform: str, records: list[Any]) -> None:
    """Patch only the content callback; comments/media storage stay disabled."""
    def append_record(args: tuple[Any, ...], kwargs: dict[str, Any], *names: str) -> None:
        record = args[0] if args else next((kwargs[name] for name in names if kwargs.get(name) is not None), None)
        if not isinstance(record, dict):
            raise TypeError(f"MediaCrawler capture callback did not receive a record: {sorted(kwargs)}")
        records.append(record)

    if platform == "xhs":
        from store import xhs as module

        async def capture(*args: Any, **kwargs: Any) -> None:
            append_record(args, kwargs, "note_item", "note_detail")

        module.update_xhs_note = capture
    elif platform == "douyin":
        from store import douyin as module

        async def capture(*args: Any, **kwargs: Any) -> None:
            # Recent MediaCrawler releases call this callback with the named
            # ``aweme_item`` argument instead of a positional record.
            append_record(args, kwargs, "aweme_item", "note_item")

        module.update_douyin_aweme = capture
    elif platform == "weibo":
        from store import weibo as module

        async def capture(*args: Any, **kwargs: Any) -> None:
            append_record(args, kwargs, "note_item")

        module.update_weibo_note = capture


async def _fetch_weibo_creator(client: Any, request: CreatorFetchRequest) -> dict[str, Any]:
    requested = request.profile_url or request.creator_id or request.creator_name or ""
    if request.creator_name:
        name = request.creator_name.strip()
        if not name or len(name) > 200:
            raise ValueError("invalid Weibo nickname")
        data = await client.get(
            "/api/container/getIndex",
            {"containerid": f"100103type=3&q={name}", "page_type": "searchall", "page": 1},
        )
        matches: dict[str, dict[str, Any]] = {}
        def collect(value: Any) -> None:
            if isinstance(value, dict):
                user = value.get("user") if isinstance(value.get("user"), dict) else value
                uid = str(user.get("idstr") or user.get("id") or "")
                if user.get("screen_name") == name and uid.isdigit():
                    matches[uid] = user
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(data.get("cards") if isinstance(data, dict) else data)
        if len(matches) != 1:
            raise ValueError(f"creator_identity_unresolved: exact nickname matched {len(matches)} users; provide the full Weibo profile URL")
        target = next(iter(matches))
    else:
        target = creator_target("weibo", requested)
    response = await client.get_creator_info_by_id(creator_id=target)
    profile = response.get("userInfo") or {}
    if str(profile.get("idstr") or profile.get("id") or "") != target:
        raise ValueError("creator_identity_mismatch: Weibo profile UID differs from the requested UID")
    if request.creator_name and str(profile.get("screen_name") or "") != request.creator_name.strip():
        raise ValueError("creator_identity_mismatch: Weibo profile nickname differs from the requested name")
    identity = CreatorIdentity(platform=Platform.WEIBO, requested_id=requested, canonical_id=target,
                               name=str(profile.get("screen_name") or ""), profile_url=f"https://weibo.com/u/{target}",
                               source="media-crawler", matched_by="exact_account_name" if request.creator_name else "exact_uid")
    collector = CreatorCollector(identity, request)
    while True:
        try:
            page = await client.get_notes_by_creator(target, f"107603{target}", collector.native)
            if not isinstance(page.get("cards"), list):
                raise ValueError("Weibo creator response did not contain cards; check account access")
            rows = [row for row in page["cards"] if row.get("card_type") == 9 and isinstance(row.get("mblog"), dict)]
            next_native = (page.get("cardlistInfo") or {}).get("since_id")
            if not collector.consume(rows, next_native, next_native not in (None, "", "0", 0)):
                break
            await asyncio.sleep(max(0.1, float(os.getenv("MEDIA_CRAWLER_SLEEP_SECONDS", "0.25"))))
        except Exception as exc:
            if not collector.pages_fetched:
                raise
            collector.warnings.append(str(exc))
            break
    return collector.result()


async def _weibo_creator_session_ready(client: Any, request: CreatorFetchRequest) -> bool:
    """Check the requested creator, since /api/config can say logged out while the saved session works."""
    try:
        if request.creator_name:
            name = request.creator_name.strip()
            data = await client.get(
                "/api/container/getIndex",
                {"containerid": f"100103type=3&q={name}", "page_type": "searchall", "page": 1},
            )
            return isinstance(data, dict) and isinstance(data.get("cards"), list)
        target = creator_target("weibo", request.profile_url or request.creator_id)
        data = await client.get_creator_info_by_id(creator_id=target)
        profile = data.get("userInfo") if isinstance(data, dict) else None
        return isinstance(profile, dict) and str(profile.get("idstr") or profile.get("id") or "") == target
    except Exception:
        return False


async def _weibo_search_session_ready(client: Any, query: str) -> bool:
    """Use a real search/detail response when /api/config reports a false logout."""
    try:
        value = str(query or "").strip() or "微博"
        data = await client.get(
            "/api/container/getIndex",
            {"containerid": f"100103type=1&q={value}", "page_type": "searchall", "page": 1},
        )
        return isinstance(data, dict) and isinstance(data.get("cards"), list)
    except Exception:
        return False


def _resolve_douyin_identity(request: CreatorFetchRequest) -> CreatorIdentity:
    """Resolve handles through dy-cli before MediaCrawler uses sec_uid APIs."""
    from dy_cli.engines.api_client import DouyinAPIClient

    requested = request.profile_url or request.creator_id or request.creator_name
    if request.creator_name:
        from social_image_mcp.creator_protocol import douyin_identity
        client = DouyinAPIClient.from_config(None)
        try:
            return douyin_identity(client, requested, nickname=True)
        finally:
            client.close()
    target = creator_target("douyin", requested)
    client = DouyinAPIClient.from_config(None)
    try:
        if not client.cookie and not target.startswith("MS4w"):
            raise RuntimeError("dy-cli 未检测到抖音登录态，无法将抖音号转换为 sec_uid")
        if target.startswith("MS4w"):
            try:
                return douyin_identity(client, requested)
            except Exception:
                # A full profile URL already carries the canonical sec_uid. Let
                # MediaCrawler perform the account confirmation with its own
                # browser session when dy-cli's profile endpoint is challenged.
                return CreatorIdentity(
                    platform=Platform.DOUYIN,
                    requested_id=requested,
                    canonical_id=target,
                    profile_url=f"https://www.douyin.com/user/{target}",
                    source="media-crawler",
                    matched_by="profile_url",
                )
        return douyin_identity(client, requested)
    finally:
        client.close()


async def _fetch_douyin_creator(client: Any, request: CreatorFetchRequest, identity: CreatorIdentity) -> dict[str, Any]:
    profile_payload = await client.get_user_info(identity.canonical_id)
    profile = profile_payload.get("user_info") if isinstance(profile_payload, dict) and isinstance(profile_payload.get("user_info"), dict) else profile_payload
    if not isinstance(profile, dict):
        profile = {}
    profile_id = str(profile.get("sec_uid") or profile.get("sec_user_id") or identity.canonical_id)
    if profile_id != identity.canonical_id:
        raise ValueError("creator_identity_mismatch: MediaCrawler profile did not confirm sec_uid")
    confirmed = identity.model_copy(update={"name": str(profile.get("nickname") or identity.name), "source": "media-crawler"})
    collector = CreatorCollector(confirmed, request)
    while True:
        try:
            page = await client.get_user_aweme_posts(identity.canonical_id, max_cursor=collector.native)
            rows = page.get("aweme_list") if isinstance(page, dict) else None
            if not isinstance(rows, list):
                raise ValueError("Douyin creator response did not contain aweme_list; check account access")
            has_more = bool(page.get("has_more"))
            if not collector.consume(rows, page.get("max_cursor"), has_more):
                break
            await asyncio.sleep(max(0.1, float(os.getenv("MEDIA_CRAWLER_SLEEP_SECONDS", "0.25"))))
        except Exception as exc:
            if not collector.pages_fetched:
                raise
            collector.warnings.append(str(exc))
            break
    return collector.result()


async def _run(args: argparse.Namespace) -> list[dict[str, Any]] | dict[str, Any]:
    vendor_root = Path(args.root).expanduser().resolve()
    if not (vendor_root / "main.py").is_file():
        raise RuntimeError(f"MediaCrawler main.py not found: {vendor_root}")
    old_argv = sys.argv
    old_cwd = Path.cwd()
    creator_result = None
    original_creator_method = None
    original_weibo_pong = None
    creator_request = None
    search_request = None
    WeiboCrawler = None
    WeiboClient = None
    DouYinCrawler = None
    creator_cli_target = ""
    try:
        # MediaCrawler imports its JS signing files using relative paths.
        os.chdir(vendor_root)
        sys.path.insert(0, str(vendor_root))
        import config
        import main as media_main

        platform = {"dy": "douyin", "xhs": "xhs", "wb": "weibo"}[PLATFORM_MAP[args.platform.lower()]]
        creator_request = CreatorFetchRequest.model_validate_json(os.environ["SOCIAL_IMAGE_CREATOR_REQUEST"]) if os.getenv("SOCIAL_IMAGE_CREATOR_REQUEST") else None
        search_request = SearchRequest.model_validate_json(os.environ["SOCIAL_IMAGE_SEARCH_REQUEST"]) if os.getenv("SOCIAL_IMAGE_SEARCH_REQUEST") else None
        if platform == "weibo":
            from media_platform.weibo.client import WeiboClient

            async def weibo_session_pong(client):
                if creator_request:
                    return await _weibo_creator_session_ready(client, creator_request)
                return await _weibo_search_session_ready(client, args.query or args.url or args.item_id)

            original_weibo_pong = WeiboClient.pong
            WeiboClient.pong = weibo_session_pong
        if creator_request:
            if platform not in {"weibo", "douyin"} or creator_request.platform.value != platform:
                raise ValueError("MediaCrawler creator bridge supports only matching douyin or weibo requests")
            from media_platform.weibo.core import WeiboCrawler
            if platform == "douyin":
                from media_platform.douyin.core import DouYinCrawler

                identity = _resolve_douyin_identity(creator_request)
                creator_cli_target = identity.canonical_id

                async def bounded_douyin_creator(crawler):
                    nonlocal creator_result
                    creator_result = await _fetch_douyin_creator(crawler.dy_client, creator_request, identity)

                original_creator_method = DouYinCrawler.get_creators_and_videos
                DouYinCrawler.get_creators_and_videos = bounded_douyin_creator
            else:
                creator_cli_target = "0" if creator_request.creator_name else creator_target("weibo", creator_request.profile_url or creator_request.creator_id)
                async def bounded_weibo_creator(crawler):
                    nonlocal creator_result
                    creator_result = await _fetch_weibo_creator(crawler.wb_client, creator_request)

                original_creator_method = WeiboCrawler.get_creators_and_notes
                WeiboCrawler.get_creators_and_notes = bounded_weibo_creator
        records: list[Any] = []
        _install_capture(platform, records)
        # Keep a small delay by default; callers may tune it for their permitted
        # account/network instead of forcing an aggressive request loop.
        config.CRAWLER_MAX_SLEEP_SEC = float(os.getenv("MEDIA_CRAWLER_SLEEP_SECONDS", "0.25"))
        config.ENABLE_GET_MEIDAS = False
        # MediaCrawler's CDP launcher can start an isolated Edge/Chrome profile,
        # preserving login state without depending on the user's open browser.
        _configure_vendor_browser(config, args)

        crawler_type = "creator" if creator_request else ("detail" if (args.item_id or args.url) else "search")
        specified = args.url or args.item_id
        if platform == "xhs" and specified and not specified.startswith(("http://", "https://")):
            specified = f"https://www.xiaohongshu.com/explore/{specified}"

        argv = [
            "media-crawler-bridge",
            "--platform", PLATFORM_MAP[args.platform.lower()],
            "--lt", args.login_type,
            "--type", crawler_type,
            "--keywords", args.query,
            "--specified_id", specified,
            "--crawler_max_notes_count", str(max(1, min(
                creator_request.max_posts if creator_request else (
                    int(os.getenv("SOCIAL_IMAGE_SEARCH_MAX_POSTS", "0")) or args.limit
                ),
                500,
            ))),
            "--save_data_option", "jsonl",
            "--save_data_path", str(vendor_root / ".bridge-data"),
            "--get_comment", "false",
            "--get_sub_comment", "false",
            "--headless", args.headless,
        ]
        if creator_request:
            argv.extend(["--creator_id", creator_cli_target])
        sys.argv = argv
        # Vendor logs are redirected so stdout remains a strict JSON stream.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            await media_main.main()
    finally:
        if original_weibo_pong and WeiboClient:
            WeiboClient.pong = original_weibo_pong
        if original_creator_method:
            if platform == "douyin" and DouYinCrawler:
                DouYinCrawler.get_creators_and_videos = original_creator_method
            elif WeiboCrawler:
                WeiboCrawler.get_creators_and_notes = original_creator_method
        sys.argv = old_argv
        os.chdir(old_cwd)
        if "media_main" in locals():
            with contextlib.suppress(Exception):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    await media_main.async_cleanup()

    if creator_request:
        if creator_result is None:
            raise RuntimeError("MediaCrawler did not execute creator retrieval")
        return creator_result

    normalized: list[dict[str, Any]] = []
    media_type = search_request.media_type if search_request else "images"
    for record in records:
        normalized.extend(normalize_native_record(
            platform,
            record,
            "media-crawler",
            media_type=media_type,
            include_video_covers=media_type == "images",
        ))
    return normalized[: max(1, args.limit)]


def main() -> int:
    _configure_stdio()
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(f"media-crawler bridge failed: {exc}", file=sys.stderr)
        return 2
    if isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    for item in result:
        print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
