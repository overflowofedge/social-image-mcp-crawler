import json
import importlib.util
from pathlib import Path

import httpx


_SPEC = importlib.util.spec_from_file_location("preflight", Path(__file__).resolve().parents[1] / "scripts" / "preflight.py")
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def _write_state(path: Path, *cookies: dict):
    path.write_text(json.dumps({"cookies": list(cookies)}), encoding="utf-8")


def test_cookie_state_requires_douyin_auth_cookie(tmp_path):
    path = tmp_path / "default.json"
    _write_state(path, {"name": "ttwid", "value": "x", "domain": ".douyin.com", "expires": -1})
    assert _MODULE.inspect_cookie_file(path, now=100)["state"] == "missing_auth"


def test_cookie_state_detects_expired_auth_cookie(tmp_path):
    path = tmp_path / "default.json"
    _write_state(path, {"name": "sessionid", "value": "x", "domain": ".douyin.com", "expires": 99})
    assert _MODULE.inspect_cookie_file(path, now=100)["state"] == "expired"


def test_cookie_state_accepts_unexpired_auth_cookie(tmp_path):
    path = tmp_path / "default.json"
    _write_state(path, {"name": "sessionid", "value": "x", "domain": ".douyin.com", "expires": 101})
    assert _MODULE.inspect_cookie_file(path, now=100)["state"] == "valid"


def test_cookie_state_handles_malformed_json(tmp_path):
    path = tmp_path / "default.json"
    path.write_text("{broken", encoding="utf-8")
    assert _MODULE.inspect_cookie_file(path)["state"] == "invalid"


def test_weibo_preflight_distinguishes_reachable_api_from_login(monkeypatch):
    seen = {}
    def fake_get(url, *, headers, **kwargs):
        seen["cookie"] = headers.get("Cookie")
        return httpx.Response(200, json={"data": {"login": False}})
    monkeypatch.setenv("WEIBO_COOKIE", "sensitive-session")
    monkeypatch.setattr(httpx, "get", fake_get)
    result = _MODULE._check_weibo_api()
    assert result == {"ok": True, "authenticated": False, "creator_lookup_ok": False, "detail": "API reachable; no native Weibo login session"}
    assert seen["cookie"] == "sensitive-session"
    assert "sensitive-session" not in str(result)


def test_weibo_preflight_reports_missing_bridge(tmp_path):
    result = _MODULE._check_weibo_bridge(tmp_path, Path(__file__))
    assert result["configured"] is False
    assert result["ok"] is False


def test_weibo_preflight_does_not_mark_logged_in_but_blocked_lookup_ready(monkeypatch):
    def fake_get(url, **kwargs):
        if url.endswith("/api/config"):
            return httpx.Response(200, json={"data": {"login": True}})
        return httpx.Response(432)
    monkeypatch.delenv("WEIBO_COOKIE", raising=False)
    monkeypatch.setattr(httpx, "get", fake_get)
    result = _MODULE._check_weibo_api()
    assert result["authenticated"] is True
    assert result["creator_lookup_ok"] is False
    assert "HTTP 432" in result["detail"]
