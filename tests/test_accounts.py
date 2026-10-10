import asyncio
import http.cookiejar
import json
import threading
import time

import httpx
import pytest

from scripts.account_login import LoginManager, _bilibili_authenticated, _weibo_authenticated, _xhs_account_response
from social_image_mcp.accounts import AUTH_NAMES, DOMAINS, PLATFORMS, QR_PLATFORMS, cookie_header, has_auth, read_session, save_session, session_path
from social_image_mcp.bilibili import BilibiliApi
from social_image_mcp.models import Platform
from social_image_mcp.sources import SourceError, SourceHub
from social_image_mcp.weibo import WeiboApi


def state(platform, value="test-session", expires=-1):
    domain = ".weibo.cn" if platform == "weibo" else "." + DOMAINS[platform][0]
    name = "sessionid" if platform == "douyin" else sorted(AUTH_NAMES[platform])[0]
    return {"cookies": [{"domain": domain, "name": name, "value": value, "expires": expires,
                         "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"}], "origins": []}


@pytest.fixture(autouse=True)
def isolated_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("DOUYIN_COOKIE_FILE", str(tmp_path / "douyin.json"))
    monkeypatch.delenv("DOUYIN_BROWSER_STORAGE_STATE", raising=False)
    monkeypatch.delenv("DY_CLI_STORAGE_STATE", raising=False)
    for platform in PLATFORMS:
        if platform != "douyin":
            monkeypatch.setenv(f"{platform.upper()}_BROWSER_STORAGE_STATE", str(tmp_path / f"{platform}.json"))
    for platform in ("x", "instagram"):
        monkeypatch.delenv(f"{platform.upper()}_GALLERY_DL_COOKIES_FILE", raising=False)
    from social_image_mcp import sources
    monkeypatch.setattr(sources, "ACCOUNT_ROOT", tmp_path)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_session_requires_auth_and_rejects_expired_or_wrong_domain(platform):
    assert has_auth(platform, state(platform))
    assert not has_auth(platform, state(platform, expires=time.time() - 5))
    wrong = state(platform)
    wrong["cookies"][0]["domain"] += ".untrusted.test"
    assert not has_auth(platform, wrong)
    visitor = state(platform)
    visitor["cookies"][0]["name"] = "uid_tt" if platform == "douyin" else "WBPSESS"
    assert not has_auth(platform, visitor)


def test_sessions_save_independently_and_invalid_relogin_preserves_old_file(tmp_path):
    for platform in PLATFORMS:
        save_session(platform, state(platform), tmp_path)
    mixed = state("weibo")
    mixed["cookies"].extend(state("douyin")["cookies"])
    save_session("weibo", mixed, tmp_path)
    previous = session_path("weibo", tmp_path).read_bytes()
    with pytest.raises(ValueError):
        save_session("weibo", {"cookies": []}, tmp_path)
    assert session_path("weibo", tmp_path).read_bytes() == previous
    for platform in PLATFORMS:
        assert read_session(platform, tmp_path) == state(platform)
    assert not list(tmp_path.glob("*.tmp"))


def test_malformed_session_is_not_logged_in(tmp_path):
    session_path("weibo", tmp_path).write_text('{"cookies": null}')
    assert not has_auth("weibo", read_session("weibo", tmp_path))
    session_path("weibo", tmp_path).write_text("broken")
    assert read_session("weibo", tmp_path) == {}


def test_header_keeps_mobile_and_desktop_cookie_domains_separate():
    cookies = state("weibo")
    cookies["cookies"].append({"domain": ".weibo.com", "name": "SUB", "value": "desktop", "expires": -1})
    assert cookie_header("weibo", cookies, "https://m.weibo.cn/api/config") == "SUB=test-session"
    assert cookie_header("weibo", cookies, "https://weibo.com/ajax/config") == "SUB=desktop"
    assert cookie_header("weibo", cookies, "https://untrusted.test/") == ""


def wait_terminal(manager, platform):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        data = manager.status(platform)
        if not data["active"]:
            return data
        time.sleep(0.01)
    raise AssertionError("login worker did not finish")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_login_deduplicates_same_platform_and_reports_saved_session_after_restart(tmp_path, platform):
    started, release = threading.Event(), threading.Event()
    calls, updated = [], []

    async def login(platform, root, cancel, update):
        calls.append(platform)
        update("waiting", "等待扫码")
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        save_session(platform, state(platform), root)

    manager = LoginManager(tmp_path, login, updated.append)
    first = manager.start(platform)
    assert started.wait(1)
    second = manager.start(platform, force=True)
    assert first["task_id"] == second["task_id"]
    release.set()
    final = wait_terminal(manager, platform)
    assert final["state"] == "completed" and final["session_available"]
    assert calls == [platform] and updated == [platform]
    assert manager.start(platform)["state"] == "completed"
    assert LoginManager(tmp_path).status(platform)["state"] == "logged_in"
    assert final["login_label"] == ("扫码登录" if platform in QR_PLATFORMS else "网页登录")
    public = json.dumps(manager.statuses())
    assert "test-session" not in public and str(tmp_path) not in public
    session_path(platform, tmp_path).unlink()
    assert manager.status(platform)["state"] == "login_required"
    manager.close()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_login_cancel_preserves_saved_session_and_does_not_cancel_other_platform(tmp_path, platform):
    other = "weibo" if platform != "weibo" else "douyin"
    save_session(platform, state(platform, "previous"), tmp_path)

    async def login(platform, root, cancel, update):
        update("waiting", "等待扫码")
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        raise asyncio.CancelledError()

    manager = LoginManager(tmp_path, login)
    manager.start(platform, force=True)
    manager.start(other)
    manager.cancel(platform)
    result = wait_terminal(manager, platform)
    assert result["state"] == "cancelled" and result["session_available"]
    assert manager.status(other)["active"]
    assert read_session(platform, tmp_path)["cookies"][0]["value"] == "previous"
    manager.close()
    assert manager.status(other)["state"] == "cancelled"


@pytest.mark.parametrize("error,expected", [(RuntimeError("secret-token"), "failed"), (TimeoutError("扫码超时"), "timeout")])
@pytest.mark.parametrize("platform", PLATFORMS)
def test_failed_login_does_not_erase_previous_session_or_expose_error_secrets(tmp_path, error, expected, platform):
    save_session(platform, state(platform), tmp_path)

    async def login(*args):
        raise error

    manager = LoginManager(tmp_path, login)
    manager.start(platform, force=True)
    result = wait_terminal(manager, platform)
    assert result["state"] == expected and result["session_available"]
    assert "secret-token" not in json.dumps(result)


def test_weibo_request_reads_updated_session_without_restarting_client(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"ok": 1, "data": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = WeiboApi(client, "SUB=legacy")
            await api._get("/api/container/getIndex", {})
            save_session("weibo", state("weibo", "new-login"), tmp_path)
            await api._get("/api/container/getIndex", {})

    asyncio.run(run())
    assert seen == ["SUB=legacy", "SUB=new-login"]


@pytest.mark.parametrize("platform", [Platform(value) for value in PLATFORMS])
def test_new_login_clears_only_that_platform_cooldown(tmp_path, platform):
    hub = SourceHub("weibo", "xhs", "gallery-dl", douyin_source_command="dy",
                    bilibili_source_command="bili", verification_path=str(tmp_path / "verification.json"))
    source = hub._platform_sources[platform]
    other_platform = Platform.WEIBO if platform != Platform.WEIBO else Platform.DOUYIN
    other = hub._platform_sources[other_platform]
    source._failed(platform, "login required")
    other._failed(other_platform, "login required")
    hub.session_updated(platform)
    source._check_cooldown(platform)
    assert not source.status.verified
    with pytest.raises(SourceError):
        other._check_cooldown(other_platform)


def test_bilibili_request_reads_updated_session_without_restarting_client(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = BilibiliApi(client, "SESSDATA=legacy")
            await api._get("/x/web-interface/nav", {})
            save_session("bilibili", state("bilibili", "new-login"), tmp_path)
            await api._get("/x/web-interface/nav", {})

    asyncio.run(run())
    assert seen == ["SESSDATA=legacy", "SESSDATA=new-login"]


@pytest.mark.parametrize("platform", ["x", "instagram"])
def test_gallery_session_updates_without_restart_and_does_not_export_other_platform(tmp_path, platform):
    from social_image_mcp.intent import parse_intent
    from social_image_mcp.sources import GalleryDlSource
    source = GalleryDlSource("gallery-dl", cookies_from_browser="edge")
    mixed = state(platform, "first")
    other = "instagram" if platform == "x" else "x"
    mixed["cookies"].extend(state(other, "other-secret")["cookies"])
    save_session(platform, mixed, tmp_path)
    command = source._command(Platform(platform), parse_intent("coffee"), 1)
    exported = tmp_path / f".cache/gallery-dl-{platform}-cookies.txt"
    assert command[command.index("--cookies") + 1] == str(exported)
    assert "--cookies-from-browser" not in command
    assert "first" in exported.read_text() and "other-secret" not in exported.read_text()
    jar = http.cookiejar.MozillaCookieJar(str(exported))
    jar.load(ignore_discard=True)
    assert [(cookie.name, cookie.value) for cookie in jar] == [(next(iter(AUTH_NAMES[platform])), "first")]
    from gallery_dl.util import cookiestxt_load
    with exported.open() as stream:
        assert [(cookie.name, cookie.value) for cookie in cookiestxt_load(stream)] == [(next(iter(AUTH_NAMES[platform])), "first")]
    save_session(platform, state(platform, "second"), tmp_path)
    source._command(Platform(platform), parse_intent("coffee"), 1)
    assert "second" in exported.read_text() and "first" not in exported.read_text()
    session_path(platform, tmp_path).unlink()
    assert has_auth(platform, read_session(platform, tmp_path))
    assert read_session(platform, tmp_path)["cookies"][0]["value"] == "second"


@pytest.mark.parametrize("platform", ["x", "instagram"])
def test_legacy_netscape_zero_expiry_is_session_cookie_but_expired_auth_is_rejected(tmp_path, platform):
    exported = tmp_path / f".cache/gallery-dl-{platform}-cookies.txt"
    exported.parent.mkdir()
    domain, name = "." + DOMAINS[platform][0], next(iter(AUTH_NAMES[platform]))
    template = "# Netscape HTTP Cookie File\n" + domain + "\tTRUE\t/\tTRUE\t{}\t" + name + "\tlegacy\n"
    exported.write_text(template.format(0))
    assert has_auth(platform, read_session(platform, tmp_path))
    exported.write_text(template.format(int(time.time()) - 30))
    assert not has_auth(platform, read_session(platform, tmp_path))


@pytest.mark.parametrize("payload,expected", [
    ({"data": {"guest": True, "user_id": "guest-id"}}, False),
    ({"data": {"guest": True, "result": {"success": True}}}, False),
    ({"data": {"guest": False, "user_id": "account-id"}}, True),
    ({"data": {"result": {"success": True}}}, True),
    ({"success": False, "data": {"result": {"success": True}}}, False),
    ({"data": None}, False),
])
def test_xhs_login_requires_account_confirmation_instead_of_guest_session(payload, expected):
    assert _xhs_account_response(payload) is expected


@pytest.mark.parametrize("payload,expected", [
    ({"code": 0, "data": {"isLogin": True}}, True),
    ({"code": 0, "data": {"isLogin": False}}, False),
    ({"code": -101, "data": {"isLogin": True}}, False),
    ({"code": 0, "data": None}, False),
])
def test_bilibili_login_verifies_official_account_endpoint(payload, expected):
    class Response:
        status = 200
        async def json(self):
            return payload

    class Request:
        async def get(self, url, **kwargs):
            assert url == "https://api.bilibili.com/x/web-interface/nav"
            return Response()

    class Context:
        request = Request()

    assert asyncio.run(_bilibili_authenticated(Context())) is expected


@pytest.mark.parametrize("platform,confirmed,close_fails", [("douyin", True, False), ("xhs", False, False), ("xhs", True, False),
                                              ("bilibili", False, False), ("bilibili", True, False),
                                              ("x", True, False), ("instagram", True, False),
                                              ("instagram", True, True)])
def test_browser_worker_saves_only_confirmed_sessions_and_closes_context(monkeypatch, tmp_path, platform, confirmed, close_fails):
    from playwright import async_api
    from scripts import account_login
    closed = []
    updates = []
    clicks = []

    class Locator:
        @property
        def first(self):
            return self
        async def click(self, **kwargs):
            clicks.append("login")
        async def is_visible(self):
            return False

    class Response:
        url = "https://edith.xiaohongshu.com/api/sns/web/v1/user/selfinfo"
        status = 200
        async def json(self):
            return {"code": 0, "data": {"isLogin": confirmed, "result": {"success": confirmed}, "guest": not confirmed}}

    class Page:
        async def goto(self, url, **kwargs):
            assert url == account_login.LOGIN_URLS[platform]
            if platform == "xhs":
                await context.listener(Response())
        def get_by_text(self, *args, **kwargs):
            return Locator()
        def locator(self, *args):
            return Locator()

    class Request:
        async def get(self, *args, **kwargs):
            return Response()

    class Context:
        pages = [Page()]
        request = Request()
        def on(self, event, listener):
            self.listener = listener
        async def new_page(self):
            return self.pages[0]
        async def storage_state(self):
            return state(platform)
        async def close(self):
            closed.append("context")
            if close_fails:
                raise RuntimeError("context already closed")

    context = Context()

    class Browser:
        async def new_context(self):
            return context
        def is_connected(self):
            return True
        async def close(self):
            closed.append("browser")
        async def launch(self, **kwargs):
            return self

    class Playwright:
        chromium = Browser()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(async_api, "async_playwright", Playwright)
    run = account_login._login_browser_session(platform, tmp_path, threading.Event(),
                                               lambda *args: updates.append(args), timeout=0.02)
    if confirmed or platform == "bilibili":
        if close_fails:
            with pytest.raises(RuntimeError, match="context already closed"):
                asyncio.run(run)
        else:
            asyncio.run(run)
        assert read_session(platform, tmp_path) == state(platform)
        assert updates[-1][0] == "saving"
    else:
        with pytest.raises(TimeoutError):
            asyncio.run(run)
        assert not session_path(platform, tmp_path).exists()
    assert closed == ["context", "browser"]
    if platform == "douyin":
        assert clicks == ["login"]


def test_weibo_login_verification_requires_official_logged_in_response():
    class Response:
        status = 200
        async def json(self):
            return {"data": {"login": False}}

    class Request:
        async def get(self, url, **kwargs):
            return Response()

    class Context:
        request = Request()

    assert not asyncio.run(_weibo_authenticated(Context()))


def test_weibo_login_uses_one_sso_page_and_does_not_open_mobile_login(monkeypatch, tmp_path):
    from scripts import account_login
    monkeypatch.setenv("WEIBO_BROWSER_STORAGE_STATE", str(tmp_path / "weibo.json"))
    opened_urls = []
    new_pages = []

    class Response:
        status = 200
        async def json(self):
            return {"data": {"login": True}}

    class Request:
        async def get(self, url, **kwargs):
            assert url == "https://weibo.com/ajax/config"
            return Response()

    class Page:
        async def goto(self, url, **kwargs):
            opened_urls.append(url)

    page = Page()

    class Context:
        pages = [page]
        request = Request()
        async def new_page(self):
            new_pages.append(page)
            self.pages.append(page)
            return page
        async def storage_state(self):
            return {"cookies": [{"name": "SUB", "value": "single-sso", "domain": ".weibo.com",
                                  "path": "/", "expires": -1}], "origins": []}
        async def close(self):
            pass

    context = Context()

    class Browser:
        def is_connected(self):
            return True
        async def new_context(self):
            return context
        async def close(self):
            pass

    browser = Browser()

    class Chromium:
        async def launch(self, **kwargs):
            return browser

    class Playwright:
        chromium = Chromium()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Playwright())
    monkeypatch.setattr("scripts.preflight._browser_channel", lambda: None)
    asyncio.run(account_login._login_browser_session(
        "weibo", tmp_path, threading.Event(), lambda *_: None, timeout=2,
    ))

    assert opened_urls == [account_login.LOGIN_URLS["weibo"]]
    # The single page is the SSO window itself; no second m.weibo login page.
    assert len(new_pages) == 1
    assert read_session("weibo", tmp_path)["cookies"][0]["value"] == "single-sso"


def test_cancel_interrupts_even_a_stalled_login_navigation(monkeypatch, tmp_path):
    from scripts import account_login
    started, cleaned = threading.Event(), threading.Event()

    async def stalled(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(account_login, "_login_browser_session", stalled)
    manager = LoginManager(tmp_path)
    manager.start("weibo")
    assert started.wait(1)
    manager.cancel("weibo")
    assert wait_terminal(manager, "weibo")["state"] == "cancelled"
    assert cleaned.wait(1)


def test_login_session_is_not_complete_when_worker_returns_without_saving(tmp_path):
    async def no_session(*args):
        return

    manager = LoginManager(tmp_path, no_session)
    manager.start("weibo")
    final = wait_terminal(manager, "weibo")
    assert final["state"] == "failed" and not final["session_available"]
