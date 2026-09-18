from __future__ import annotations

"""Metadata-only bridge for the GitHub dy-cli project.

The dy-cli repository owns Douyin request signing, search and detail parsing.
This bridge only converts its native records into the MCP JSON contract; image
bytes are still downloaded and validated by social-image-mcp.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse


ROOT = Path(__file__).resolve().parents[1]
DY_ROOT = Path(os.getenv("DY_CLI_ROOT") or ROOT / "third_party" / "dy-cli").expanduser().resolve()
DY_SRC = DY_ROOT / "src"
if str(DY_SRC) not in sys.path:
    sys.path.insert(0, str(DY_SRC))

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from social_image_mcp.bridge_utils import normalize_native_record
from social_image_mcp.creator_protocol import CreatorCollector, douyin_identity
from social_image_mcp.models import CreatorFetchRequest


def _configure_stdio() -> None:
    """Keep JSON output valid on Windows consoles using a legacy code page."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="dy-cli JSON bridge")
    parser.add_argument("--query", default="")
    parser.add_argument("--item-id", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--account", default=None)
    return parser


def _cache_path() -> Path:
    configured = os.getenv("DY_CLI_RESULT_CACHE")
    path = Path(configured).expanduser() if configured else Path(".cache") / "dy-cli-results.json"
    return path if path.is_absolute() else ROOT / path


def _load_cached_items(item_id: str, max_age_seconds: int = 6 * 60 * 60) -> list[dict[str, Any]]:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entry = payload.get(item_id) if isinstance(payload, dict) else None
        if not isinstance(entry, dict) or time.time() - float(entry.get("saved_at", 0)) > max_age_seconds:
            return []
        items = entry.get("items")
        return items if isinstance(items, list) and all(isinstance(item, dict) for item in items) else []
    except (OSError, ValueError, TypeError):
        return []


def _save_cached_items(items: list[dict[str, Any]]) -> None:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        post_id = str(item.get("post_id") or item.get("id") or "")
        if post_id:
            grouped.setdefault(post_id, []).append(item)
    if not grouped:
        return
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(existing, dict):
            existing = {}
        now = time.time()
        for post_id, post_items in grouped.items():
            existing[post_id] = {"saved_at": now, "items": post_items}
        # Keep the file bounded while retaining the newest exact-ID entries.
        existing = dict(sorted(existing.items(), key=lambda pair: float(pair[1].get("saved_at", 0)), reverse=True)[:500])
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        # Cache is an optimization; a read-only directory must not break search.
        return


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("data") or []
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        aweme = row.get("aweme_info")
        if isinstance(aweme, dict):
            result.append(aweme)
        elif "aweme_id" in row or "images" in row:
            result.append(row)
    return result


def _exact_record(records: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    for record in records:
        if str(record.get("aweme_id") or record.get("awemeId") or record.get("id") or "") == item_id:
            return record
    return None


def _raise_for_empty_search(payload: dict[str, Any]) -> None:
    nil_info = payload.get("search_nil_info") or {}
    nil_type = nil_info.get("search_nil_type") if isinstance(nil_info, dict) else None
    if nil_type == "verify_check":
        raise RuntimeError("抖音触发 verify_check，本次请求未获得候选；请稍后重试或先使用作品 URL/ID")
    raise RuntimeError("抖音搜索成功但没有返回图文候选；请更换关键词或稍后重试")


def _fetch_creator(client: Any, request: CreatorFetchRequest) -> dict[str, Any]:
    identity = douyin_identity(client, request.profile_url or request.creator_id or request.creator_name, nickname=bool(request.creator_name))
    collector = CreatorCollector(identity, request)
    while True:
        try:
            payload = client.get_user_posts(identity.canonical_id, max_cursor=int(collector.native), count=20)
            rows = payload.get("aweme_list")
            if not isinstance(rows, list):
                raise ValueError("creator timeline did not contain aweme_list; check account access")
            if not collector.consume(rows, payload.get("max_cursor"), bool(payload.get("has_more"))):
                break
            time.sleep(max(0.1, float(os.getenv("DOUYIN_CREATOR_SLEEP_SECONDS", "0.25"))))
        except Exception as exc:
            if not collector.pages_fetched:
                raise
            collector.warnings.append(str(exc))
            break
    return collector.result()


def _looks_like_douyin_creator_url(url: str) -> bool:
    """Return true for explicit Douyin profile/share URLs.

    ``v.douyin.com`` is also used for作品 shares, so short links are only
    treated as creator links after ``resolve_share_url`` has identified them
    as a homepage. Full ``/user`` and ``/share/user`` links are unambiguous.
    """
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in {"douyin.com", "www.douyin.com", "v.douyin.com"}:
        return False
    path = parsed.path.rstrip("/").lower()
    return path.startswith("/user/") or path.startswith("/share/user/")


def _creator_items_from_profile_share(client: Any, url: str, limit: int, account: str | None = None) -> list[dict[str, Any]]:
    """Resolve a homepage share and expose creator images to search callers."""
    resolver = getattr(client, "resolve_creator_share_url", None)
    if not callable(resolver):
        raise RuntimeError("当前 dy-cli 不支持抖音主页短链接解析，请更新桌面应用后重试")
    profile_url = str(resolver(url))
    request = CreatorFetchRequest(
        platform="douyin",
        profile_url=profile_url,
        max_posts=max(20, min(100, max(1, limit))),
        max_images=max(1, min(200, limit)),
        media_type="images",
        download=False,
    )
    result = _fetch_creator_with_fallback(client, request, account)
    items = [item for item in result.get("items", []) if isinstance(item, dict)]
    return items[: max(1, limit)]


def _browser_storage_state(account: str | None = None) -> Path | None:
    """Locate the Playwright storage state produced by dy-cli login."""
    configured = os.getenv("DOUYIN_BROWSER_STORAGE_STATE") or os.getenv("DY_CLI_STORAGE_STATE")
    if configured:
        path = Path(configured).expanduser()
    else:
        try:
            from dy_cli.utils.config import get_cookie_file

            path = Path(get_cookie_file(account)).expanduser()
        except Exception:
            path = Path.home() / ".dy" / "cookies" / "default.json"
    return path if path.is_file() else None


def _browser_endpoint(url: str) -> str:
    path = urlparse(url).path
    if path.endswith("/user/profile/other/"):
        return "profile"
    if path.endswith("/aweme/post/"):
        return "posts"
    if path.endswith("/discover/search/"):
        return "user_search"
    return ""


def _browser_profile(payload: Any) -> dict[str, Any] | None:
    """Extract a user object from profile or user-search JSON responses."""
    if not isinstance(payload, dict):
        return None
    direct = payload.get("user")
    if isinstance(direct, dict) and direct.get("sec_uid"):
        return direct
    for key in ("user_info", "user_info_list", "user_list", "users", "data", "result"):
        child = payload.get(key)
        if isinstance(child, dict):
            if child.get("sec_uid"):
                return child
            if profile := _browser_profile(child):
                return profile
        elif isinstance(child, list):
            for row in child:
                if isinstance(row, dict):
                    candidate = row.get("user_info") if isinstance(row.get("user_info"), dict) else row
                    if isinstance(candidate, dict) and candidate.get("sec_uid"):
                        return candidate
                    if profile := _browser_profile(row):
                        return profile
    return None


def _browser_user_matches(payload: Any, requested: str, nickname: bool = False) -> list[dict[str, Any]]:
    """Return exact account-ID matches from a browser user-search response."""
    matches: dict[str, dict[str, Any]] = {}

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            sec_uid = str(value.get("sec_uid") or value.get("secUid") or "")
            keys = ("nickname", "nick_name", "display_name") if nickname else ("unique_id", "short_id", "uid")
            if sec_uid and any(str(value.get(key) or "") == requested for key in keys):
                matches[sec_uid] = value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(payload)
    return list(matches.values())


def _browser_post_cursor(url: str, payload: dict[str, Any]) -> tuple[str, bool, str]:
    """Read pagination values while tolerating minor endpoint shape changes."""
    query = parse_qs(urlparse(url).query)
    requested = str((query.get("max_cursor") or [""])[0])
    native = payload.get("max_cursor")
    if native in (None, ""):
        native = payload.get("maxCursor")
    has_more = payload.get("has_more")
    if has_more is None:
        has_more = payload.get("hasMore")
    if isinstance(has_more, str):
        has_more = has_more.strip().lower() in {"1", "true", "yes"}
    else:
        has_more = bool(has_more)
    return str(native if native is not None else ""), has_more, requested


def _browser_items_from_payload(
    payload: dict[str, Any],
    identity: Any,
    request: CreatorFetchRequest,
    collector: CreatorCollector,
    response_url: str = "",
) -> bool:
    """Consume one captured post response; return whether it was a valid page."""
    rows = payload.get("aweme_list") or payload.get("awemeList")
    if not isinstance(rows, list):
        return False
    next_native, has_more, requested_cursor = _browser_post_cursor(response_url, payload)
    # On resume, ignore the initial page until the browser asks for the saved
    # native cursor. This preserves CreatorCollector's offset semantics.
    if collector.native != "0" and requested_cursor and requested_cursor != collector.native:
        return False
    return collector.consume(rows, next_native, has_more)


async def _fetch_creator_via_browser(request: CreatorFetchRequest, account: str | None = None) -> dict[str, Any]:
    """Collect Douyin creator responses inside a real logged-in browser.

    The browser is responsible for dynamic webid/msToken/a-bogus/security
    headers. We only observe the public JSON responses emitted by the profile
    page and never attempt to bypass a challenge or login wall.
    """
    from playwright.async_api import async_playwright

    requested = request.profile_url or request.creator_id or request.creator_name or ""
    target = requested
    if target.startswith(("http://", "https://")):
        target = target.rstrip("/").rsplit("/", 1)[-1]
    target = target.removeprefix("douyin-user:").removeprefix("douyin-user：")
    storage_state = _browser_storage_state(account)
    timeout_ms = max(10_000, int(os.getenv("DOUYIN_BROWSER_TIMEOUT_MS", os.getenv("BROWSER_TIMEOUT_MS", "30000"))))
    headless = os.getenv("DOUYIN_BROWSER_HEADLESS", os.getenv("BROWSER_HEADLESS", "true")).lower() in {"1", "true", "yes"}
    executable = os.getenv("DOUYIN_BROWSER_PATH") or os.getenv("MEDIA_CRAWLER_BROWSER_PATH")

    launch_options: dict[str, Any] = {"headless": headless}
    if executable and Path(executable).is_file():
        launch_options["executable_path"] = executable
    elif os.getenv("DOUYIN_BROWSER_CHANNEL") or os.getenv("BROWSER_CHANNEL"):
        launch_options["channel"] = os.getenv("DOUYIN_BROWSER_CHANNEL") or os.getenv("BROWSER_CHANNEL")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**launch_options)
        context_options: dict[str, Any] = {"viewport": {"width": 1440, "height": 1000}}
        if storage_state:
            context_options["storage_state"] = str(storage_state)
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        captured: list[tuple[str, dict[str, Any]]] = []
        capture_tasks: set[asyncio.Task[Any]] = set()

        async def capture(response: Any) -> None:
            endpoint = _browser_endpoint(response.url)
            if not endpoint:
                return
            try:
                # Decode the completed response body in the task rather than
                # calling response.json() directly from the event callback.
                body = await response.body()
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                return
            if isinstance(payload, dict):
                captured.append((response.url, payload))

        def schedule_capture(response: Any) -> None:
            task = asyncio.create_task(capture(response))
            capture_tasks.add(task)
            task.add_done_callback(capture_tasks.discard)

        page.on("response", schedule_capture)
        try:
            # A handle URL is accepted by the web app in most regions. If it
            # does not emit a profile response, the exact user-search route is
            # used below to resolve it without guessing among similar names.
            profile_url = f"https://www.douyin.com/user/{quote(target, safe='') }"
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=timeout_ms)

            identity_data: dict[str, Any] | None = None
            deadline = time.monotonic() + min(timeout_ms / 1000, 12)
            while time.monotonic() < deadline and not identity_data:
                for response_url, payload in captured:
                    if _browser_endpoint(response_url) == "profile":
                        candidate = _browser_profile(payload)
                        if candidate and candidate.get("sec_uid"):
                            identity_data = candidate
                            break
                if identity_data:
                    break
                await page.wait_for_timeout(max(100, int(os.getenv("DOUYIN_BROWSER_POLL_MS", "400"))))

            # A canonical sec_uid came from the signed API identity lookup or
            # from the resolved profile URL. Recent Douyin web responses may
            # omit the profile object entirely (or return it in a shape that
            # no longer exposes sec_uid), but the account identifier remains
            # authoritative. Continue with that ID so the post timeline can
            # still be captured instead of failing during identity discovery.
            if not identity_data and target.startswith("MS4w"):
                identity_data = {"sec_uid": target, "nickname": ""}

            if not identity_data and not target.startswith("MS4w"):
                captured.clear()
                search_url = f"https://www.douyin.com/search/{quote(target, safe='')}?type=user"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=timeout_ms)
                deadline = time.monotonic() + min(timeout_ms / 1000, 12)
                while time.monotonic() < deadline:
                    matches: list[dict[str, Any]] = []
                    for response_url, payload in captured:
                        if _browser_endpoint(response_url) == "user_search":
                            matches.extend(_browser_user_matches(payload, target, nickname=bool(request.creator_name)))
                    unique = {str(row.get("sec_uid")): row for row in matches if row.get("sec_uid")}
                    if len(unique) == 1:
                        identity_data = next(iter(unique.values()))
                        break
                    await page.wait_for_timeout(max(100, int(os.getenv("DOUYIN_BROWSER_POLL_MS", "400"))))
                if not identity_data:
                    kind = "nickname" if request.creator_name else "account ID"
                    raise RuntimeError(f"creator_identity_unresolved: browser user search did not return one exact {kind}")
                captured.clear()
                await page.goto(f"https://www.douyin.com/user/{quote(str(identity_data['sec_uid']), safe='')}", wait_until="domcontentloaded", timeout=timeout_ms)

            if not identity_data or not identity_data.get("sec_uid"):
                raise RuntimeError("creator_identity_unresolved: browser profile did not expose sec_uid")
            canonical = str(identity_data["sec_uid"])
            if target.startswith("MS4w") and canonical != target:
                raise RuntimeError("creator_identity_mismatch: browser profile sec_uid differs from requested ID")
            if not target.startswith("MS4w"):
                keys = ("nickname", "nick_name", "display_name") if request.creator_name else ("unique_id", "short_id", "uid")
                if not any(str(identity_data.get(key) or "") == target for key in keys):
                    raise RuntimeError("creator_identity_mismatch: browser profile account identity differs from requested value")

            from social_image_mcp.models import CreatorIdentity, Platform

            identity = CreatorIdentity(
                platform=Platform.DOUYIN,
                requested_id=requested,
                canonical_id=canonical,
                name=str((identity_data or {}).get("nickname") or ""),
                profile_url=f"https://www.douyin.com/user/{canonical}",
                source="dy-cli-browser",
                matched_by="sec_uid" if target.startswith("MS4w") else ("exact_account_name" if request.creator_name else "exact_account_id"),
            )
            collector = CreatorCollector(identity, request)
            consumed: set[tuple[str, str]] = set()
            for _ in range(10):
                await page.wait_for_timeout(max(150, int(os.getenv("DOUYIN_BROWSER_POLL_MS", "400"))))
                progressed = False
                for response_url, payload in list(captured):
                    if _browser_endpoint(response_url) != "posts":
                        continue
                    rows = payload.get("aweme_list") or payload.get("awemeList") or []
                    first_id = str(rows[0].get("aweme_id") or "") if rows and isinstance(rows[0], dict) else ""
                    next_native, has_more, requested_cursor = _browser_post_cursor(response_url, payload)
                    key = (requested_cursor or "0", first_id)
                    if key in consumed:
                        continue
                    consumed.add(key)
                    if _browser_items_from_payload(payload, identity, request, collector, response_url):
                        progressed = True
                    if collector.posts_fetched >= request.max_posts or collector.pages_fetched >= 10:
                        break
                    if not has_more and collector.pages_fetched:
                        break
                if collector.posts_fetched >= request.max_posts or (collector.pages_fetched and collector.next_cursor is None):
                    break
                # The profile uses infinite scroll to request the next cursor.
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                if not progressed and not captured:
                    await page.wait_for_timeout(max(150, int(os.getenv("DOUYIN_BROWSER_POLL_MS", "400"))))
            if not collector.pages_fetched:
                raise RuntimeError("creator_source_error: browser profile did not return an aweme_list response")
            return collector.result()
        finally:
            if capture_tasks:
                await asyncio.gather(*capture_tasks, return_exceptions=True)
            await context.close()
            await browser.close()


def _fetch_creator_with_fallback(client: Any, request: CreatorFetchRequest, account: str | None = None) -> dict[str, Any]:
    """Use dy-cli first, then let the logged-in web app collect its own API responses."""
    http_error: Exception | None = None
    try:
        return _fetch_creator(client, request)
    except Exception as exc:
        http_error = exc

    # A completed exact lookup is authoritative. Browser fallback cannot
    # disambiguate a duplicate nickname or turn zero exact matches into one.
    if http_error and "creator_identity_unresolved:" in str(http_error):
        raise http_error

    # The browser route is intentionally attempted after the native client. It
    # is slower, but can still work when the API client's manually signed
    # request is rejected with 403 or a stale web signature.
    try:
        result = asyncio.get_event_loop().run_until_complete(_fetch_creator_via_browser(request, account))
        if http_error and result.get("warnings") is None:
            # Keep the successful response clean; the source identity already
            # records that browser capture was used. Do not turn recovery into
            # a partial creator job merely because the first route was blocked.
            result["warnings"] = []
        return result
    except Exception as browser_error:
        raise RuntimeError(f"dy-cli creator routes failed: http={http_error}; browser={browser_error}") from browser_error


def _sync_fetch(args: argparse.Namespace) -> list[dict[str, Any]] | dict[str, Any]:
    from dy_cli.engines.api_client import DouyinAPIClient, DouyinAPIError
    from dy_cli.utils.signature import close_sign_page

    # dy-cli's synchronous client signs requests through Playwright. Keep one
    # event loop alive for the whole worker call so the signature page is
    # created and closed on the same loop instead of becoming a leaked task.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = DouyinAPIClient.from_config(args.account)
    try:
        if creator_json := os.getenv("SOCIAL_IMAGE_CREATOR_REQUEST"):
            request = CreatorFetchRequest.model_validate_json(creator_json)
            if request.platform.value != "douyin":
                raise ValueError("dy-cli creator platform must be douyin")
            if not client.cookie:
                raise RuntimeError("dy-cli login is required for creator retrieval")
            return _fetch_creator_with_fallback(client, request, args.account)
        profile_share_url: str | None = None
        if args.url:
            # A homepage share is intentionally not accepted by
            # ``resolve_share_url`` (that method is for one作品). Remember
            # this case and route it through the creator collector below.
            try:
                client.resolve_share_url(args.url)
            except Exception:
                if _looks_like_douyin_creator_url(args.url):
                    profile_share_url = args.url
                else:
                    # Short homepage links do not have a stable path. Let the
                    # dedicated resolver confirm them instead of relying on
                    # the wording of the作品 resolver's exception.
                    try:
                        client.resolve_creator_share_url(args.url)
                    except Exception:
                        pass
                    else:
                        profile_share_url = args.url

        if args.item_id or args.url:
            # A recent exact result is safe to reuse even if the login cookie
            # has temporarily expired; no new platform request is made.
            cached_id = args.item_id
            if not cached_id and args.url:
                try:
                    cached_id = client.resolve_share_url(args.url)
                except Exception:
                    cached_id = None
            if cached_id and (cached := _load_cached_items(cached_id)) and len(cached) >= max(1, args.limit):
                # The service applies the user's final media limit. Keep the
                # complete cached gallery here so an exact post can be
                # traversed without silently dropping later frames.
                return cached

        if not client.cookie:
            raise RuntimeError("dy-cli 未检测到抖音登录态，请先运行 scripts\\douyin_login.ps1 完成一次扫码登录")

        if profile_share_url:
            return _creator_items_from_profile_share(client, profile_share_url, args.limit, args.account)

        if args.item_id or args.url:
            item_id = args.item_id or client.resolve_share_url(args.url)
            try:
                record = client.get_video_detail(item_id)
            except DouyinAPIError as detail_error:
                # Some regions can search but cannot access the detail API.
                # Retry through search only with an exact aweme_id match; never
                # return a merely similar result for an ID request.
                try:
                    payload = client.search(item_id, search_type="atlas", count=20)
                    record = _exact_record(_records(payload), item_id)
                except DouyinAPIError:
                    record = None
                if not record:
                    raise RuntimeError(
                        f"抖音作品 {item_id} 详情不可用：{detail_error}；精确搜索也未命中。"
                        "请检查作品是否公开，并为 dy-cli 配置可访问抖音的网络代理"
                    ) from detail_error
            if not isinstance(record, dict):
                return []
            result = normalize_native_record("douyin", record, "dy-cli")
            _save_cached_items(result)
            return result

        if not args.query.strip():
            raise RuntimeError("关键词不能为空；请传入 --query 或 --item-id/--url")
        # Prefer atlas (image-text) search, but Douyin may return
        # ``verify_check`` or an empty atlas page while ordinary search still
        # works.  Falling back to general search lets us return video covers
        # and any image-text records instead of reporting a misleading zero.
        payload = client.search(args.query, search_type="atlas", count=max(1, min(args.limit, 50)))
        galleries = [normalize_native_record("douyin", record, "dy-cli") for record in _records(payload)]
        galleries = [gallery for gallery in galleries if gallery]
        if not galleries:
            fallback = client.search(args.query, search_type="general", count=max(1, min(args.limit, 50)))
            fallback_galleries = [normalize_native_record("douyin", record, "dy-cli") for record in _records(fallback)]
            fallback_galleries = [gallery for gallery in fallback_galleries if gallery]
            if fallback_galleries:
                payload = fallback
                galleries = fallback_galleries
            else:
                # Report the more useful verification error if both routes
                # failed; otherwise retain the native empty-search message.
                _raise_for_empty_search(payload if payload.get("search_nil_info") else fallback)

        # Round-robin preserves post diversity before adding extra frames from
        # large galleries, giving the reranker meaningfully different choices.
        result: list[dict[str, Any]] = []
        image_index = 0
        while len(result) < args.limit:
            added = False
            for gallery in galleries:
                if image_index < len(gallery):
                    result.append(gallery[image_index])
                    added = True
                    if len(result) >= args.limit:
                        break
            if not added:
                break
            image_index += 1
        result = result[: max(1, args.limit)]
        _save_cached_items(result)
        return result
    except DouyinAPIError as exc:
        raise RuntimeError(f"dy-cli 抖音请求失败: {exc}") from exc
    finally:
        client.close()
        try:
            loop.run_until_complete(close_sign_page())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()


async def _run(args: argparse.Namespace) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_sync_fetch, args)


def main() -> int:
    _configure_stdio()
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(f"dy-cli bridge failed: {exc}", file=sys.stderr)
        return 2
    if isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    for item in result:
        print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
