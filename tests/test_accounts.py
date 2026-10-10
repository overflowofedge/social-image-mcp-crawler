import asyncio
import json
import threading
import time

import httpx
import pytest

from scripts.account_login import LoginManager, _weibo_authenticated
from social_image_mcp.accounts import cookie_header, has_auth, read_session, save_session, session_path
from social_image_mcp.models import Platform
from social_image_mcp.sources import SourceError, SourceHub
from social_image_mcp.weibo import WeiboApi


def state(platform, value="test-session", expires=-1):
    domain, name = (".douyin.com", "sessionid") if platform == "douyin" else (".weibo.cn", "SUB")
    return {"cookies": [{"domain": domain, "name": name, "value": value, "expires": expires,
                         "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax"}], "origins": []}


@pytest.fixture(autouse=True)
def isolated_sessions(monkeypatch, tmp_path):
    monkeypatch.setenv("DOUYIN_COOKIE_FILE", str(tmp_path / "douyin.json"))
    monkeypatch.delenv("DOUYIN_BROWSER_STORAGE_STATE", raising=False)
    monkeypatch.delenv("DY_CLI_STORAGE_STATE", raising=False)
    monkeypatch.setenv("WEIBO_BROWSER_STORAGE_STATE", str(tmp_path / "weibo.json"))


@pytest.mark.parametrize("platform", ["douyin", "weibo"])
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
    save_session("douyin", state("douyin"), tmp_path)
    save_session("weibo", state("weibo"), tmp_path)
    mixed = state("weibo")
    mixed["cookies"].extend(state("douyin")["cookies"])
    save_session("weibo", mixed, tmp_path)
    previous = session_path("weibo", tmp_path).read_bytes()
    with pytest.raises(ValueError):
        save_session("weibo", {"cookies": []}, tmp_path)
    assert session_path("weibo", tmp_path).read_bytes() == previous
    assert read_session("weibo", tmp_path) == state("weibo")
    assert read_session("douyin", tmp_path) == state("douyin")
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


def test_login_deduplicates_same_platform_and_reports_saved_session_after_restart(tmp_path):
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
    first = manager.start("douyin")
    assert started.wait(1)
    second = manager.start("douyin", force=True)
    assert first["task_id"] == second["task_id"]
    release.set()
    final = wait_terminal(manager, "douyin")
    assert final["state"] == "completed" and final["session_available"]
    assert calls == ["douyin"] and updated == ["douyin"]
    assert manager.start("douyin")["state"] == "completed"
    assert LoginManager(tmp_path).status("douyin")["state"] == "logged_in"
    public = json.dumps(manager.statuses())
    assert "test-session" not in public and str(tmp_path) not in public
    session_path("douyin", tmp_path).unlink()
    assert manager.status("douyin")["state"] == "login_required"
    manager.close()


def test_login_cancel_preserves_saved_session_and_does_not_cancel_other_platform(tmp_path):
    save_session("douyin", state("douyin", "previous"), tmp_path)

    async def login(platform, root, cancel, update):
        update("waiting", "等待扫码")
        while not cancel.is_set():
            await asyncio.sleep(0.01)
        raise asyncio.CancelledError()

    manager = LoginManager(tmp_path, login)
    manager.start("douyin", force=True)
    manager.start("weibo")
    manager.cancel("douyin")
    result = wait_terminal(manager, "douyin")
    assert result["state"] == "cancelled" and result["session_available"]
    assert manager.status("weibo")["active"]
    assert read_session("douyin", tmp_path)["cookies"][0]["value"] == "previous"
    manager.close()
    assert manager.status("weibo")["state"] == "cancelled"


@pytest.mark.parametrize("error,expected", [(RuntimeError("secret-token"), "failed"), (TimeoutError("扫码超时"), "timeout")])
def test_failed_login_does_not_erase_previous_session_or_expose_error_secrets(tmp_path, error, expected):
    save_session("weibo", state("weibo"), tmp_path)

    async def login(*args):
        raise error

    manager = LoginManager(tmp_path, login)
    manager.start("weibo", force=True)
    result = wait_terminal(manager, "weibo")
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


def test_new_login_clears_only_that_platform_cooldown(tmp_path):
    hub = SourceHub("weibo", "xhs", "gallery-dl", douyin_source_command="dy", verification_path=str(tmp_path / "verification.json"))
    hub.douyin_source._failed(Platform.DOUYIN, "login required", "creator-one")
    hub.weibo_source._failed(Platform.WEIBO, "login required")
    hub.session_updated(Platform.DOUYIN)
    hub.douyin_source._check_cooldown(Platform.DOUYIN, "creator-one")
    assert not hub.douyin_source.status.verified
    with pytest.raises(SourceError):
        hub.weibo_source._check_cooldown(Platform.WEIBO)


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
