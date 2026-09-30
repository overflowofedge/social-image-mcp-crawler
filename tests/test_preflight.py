import json
import importlib.util
from pathlib import Path


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
