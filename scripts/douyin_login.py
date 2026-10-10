"""Launch the native dy-cli login using the browser available on this PC."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "third_party/dy-cli/src"))


def configure_login_browser() -> None:
    from playwright.async_api import BrowserType
    from preflight import _browser_channel

    channel = os.getenv("DOUYIN_BROWSER_CHANNEL") or _browser_channel()
    if not channel:
        return
    original = BrowserType.launch

    async def launch(browser_type, *args, **kwargs):
        if browser_type.name == "chromium" and not kwargs.get("executable_path") and not kwargs.get("channel"):
            kwargs["channel"] = channel
        return await original(browser_type, *args, **kwargs)

    BrowserType.launch = launch


if __name__ == "__main__":
    configure_login_browser()
    from dy_cli.main import cli
    cli(["login", *sys.argv[1:]])
