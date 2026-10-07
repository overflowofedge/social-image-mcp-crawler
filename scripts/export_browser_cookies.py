"""Export cookies from a Playwright persistent browser profile for gallery-dl."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


def _netscape_line(cookie: dict) -> str:
    domain = str(cookie.get("domain") or "")
    include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
    path = str(cookie.get("path") or "/")
    secure = "TRUE" if cookie.get("secure") else "FALSE"
    expiry = cookie.get("expires")
    try:
        expires = str(max(0, int(float(expiry)))) if expiry not in (None, "") else "0"
    except (TypeError, ValueError, OverflowError):
        expires = "0"
    name = str(cookie.get("name") or "")
    value = str(cookie.get("value") or "")
    return "\t".join((domain, include_subdomains, path, secure, expires, name, value))


async def _export(profile_root: Path, output: Path, browser: str) -> int:
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_root),
            channel=browser,
            headless=True,
            args=["--profile-directory=Default"],
        )
        try:
            cookies = await context.cookies(["https://www.instagram.com/"])
        finally:
            await context.close()
    cookies = [cookie for cookie in cookies if ".instagram.com" in str(cookie.get("domain") or "")]
    if not cookies:
        raise RuntimeError("no Instagram cookies were readable from the browser profile")
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File", *(_netscape_line(cookie) for cookie in cookies)]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"exported {len(cookies)} Instagram cookies to {output}")
    return len(cookies)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--browser", default="msedge")
    args = parser.parse_args()
    asyncio.run(_export(Path(args.profile).expanduser().resolve(), Path(args.output).expanduser().resolve(), args.browser))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
