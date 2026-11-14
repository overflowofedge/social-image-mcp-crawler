import time

from social_image_mcp.models import Platform  # noqa: F401  (keep module imports stable)
from scripts.web_login import WebLoginManager, web_session_path


def test_web_session_path_maps_domains_and_rejects_non_web():
    root = Path_stub = None
    from pathlib import Path
    root = Path(".")
    assert web_session_path(root, "https://jkforum.net/p/1.html").name == "jkforum.net.json"
    assert web_session_path(root, "https://www.lcoser02.cc/x").name == "www.lcoser02.cc.json"
    assert web_session_path(root, "ftp://example.test/x") is None
    assert web_session_path(root, "not-a-url") is None


def test_login_manager_reports_saved_when_login_writes_state(tmp_path):
    saved_events = []

    async def fake_login(url, session, cancel, update, timeout):
        update("waiting", "请在浏览器窗口中登录站点。")
        time.sleep(0.1)
        session.write_text('{"cookies": [{"name": "sid"}]}', encoding="utf-8")
        saved_events.append(url)

    manager = WebLoginManager(root=tmp_path, login=fake_login, timeout_seconds=2)
    url = "https://example.test/thread"
    status = manager.start(url)
    assert status["ok"] is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and status.get("state") != "saved":
        time.sleep(0.2)
        status = manager.status(url)
    assert status["state"] == "saved"
    assert status["session_available"] is True
    assert saved_events == [url]


def test_login_manager_fails_without_saved_state(tmp_path):
    async def fake_login(url, session, cancel, update, timeout):
        update("waiting", "等待登录。")  # writes nothing

    manager = WebLoginManager(root=tmp_path, login=fake_login, timeout_seconds=1)
    url = "https://example.test/thread"
    manager.start(url)
    deadline = time.monotonic() + 5
    status = {"state": "opening"}
    while time.monotonic() < deadline and status.get("state") != "failed":
        time.sleep(0.2)
        status = manager.status(url)
    assert status["state"] == "failed"
    assert status["session_available"] is False


def test_login_manager_rejects_invalid_urls(tmp_path):
    manager = WebLoginManager(root=tmp_path, login=lambda *a, **k: None)
    status = manager.start("not-a-url")
    assert status["ok"] is False and status["state"] == "invalid"
