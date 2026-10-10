import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from playwright.async_api import BrowserType

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("douyin_login", ROOT / "scripts/douyin_login.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_native_cli_login_uses_edge_without_bundled_chromium(monkeypatch):
    seen = []
    async def original(browser, **kwargs):
        seen.append(kwargs)
        return "browser"
    monkeypatch.setattr(BrowserType, "launch", original)
    monkeypatch.setenv("DOUYIN_BROWSER_CHANNEL", "msedge")
    MODULE.configure_login_browser()
    assert asyncio.run(BrowserType.launch(SimpleNamespace(name="chromium"), headless=False)) == "browser"
    assert seen == [{"headless": False, "channel": "msedge"}]


def test_native_cli_login_preserves_explicit_browser_path(monkeypatch):
    seen = []
    async def original(browser, **kwargs):
        seen.append(kwargs)
    monkeypatch.setattr(BrowserType, "launch", original)
    monkeypatch.setenv("DOUYIN_BROWSER_CHANNEL", "msedge")
    MODULE.configure_login_browser()
    asyncio.run(BrowserType.launch(SimpleNamespace(name="chromium"), executable_path="custom-browser.exe"))
    assert seen == [{"executable_path": "custom-browser.exe"}]
