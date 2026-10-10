from __future__ import annotations

"""Metadata-only bridge for XHS-Downloader detail extraction."""

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XHS-Downloader JSON bridge")
    parser.add_argument("--query", default="")
    parser.add_argument("--item-id", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--root", default=os.getenv("XHS_DOWNLOADER_ROOT") or str(ROOT / "third_party" / "XHS-Downloader"))
    return parser


def _as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _urls(data: dict[str, Any]) -> list[str]:
    values = data.get("下载地址") or data.get("download_urls") or data.get("imageList") or []
    if isinstance(values, str):
        values = values.split()
    result: list[str] = []
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict):
                value = value.get("urlDefault") or value.get("url") or value.get("url_default")
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                result.append(value)
    return list(dict.fromkeys(result))


def _prepare_vendor_imports() -> None:
    """Provide only the unused FastMCP symbol required by the vendor import."""
    try:
        from fastmcp import FastMCP  # noqa: F401
    except (ImportError, RuntimeError):
        module = ModuleType("fastmcp")

        class UnavailableFastMCP:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("XHS-Downloader MCP server support is unavailable in bridge mode")

        module.FastMCP = UnavailableFastMCP
        sys.modules["fastmcp"] = module


def _normalize_results(results: list[dict[str, Any]], item_id: str, url: str, limit: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for data in results:
        note_id = _as_text(data.get("作品ID") or item_id)
        title = _as_text(data.get("作品标题") or data.get("作品描述"))
        permalink = _as_text(data.get("作品链接") or url)
        author = _as_text(data.get("作者昵称"))
        work_type = _as_text(data.get("作品类型") or data.get("type")).casefold()
        media_type = "video" if work_type in {"视频", "video"} else "image"
        for media_url in _urls(data):
            normalized.append({
                "id": note_id or "unknown",
                "image_url": media_url,
                "media_type": media_type,
                "title": title,
                "description": _as_text(data.get("作品描述")),
                "author": author,
                "permalink": permalink,
                "source": "xhs-downloader",
            })
    return normalized[: max(1, limit)]


async def _run(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not args.url and not args.item_id:
        # Keyword recall belongs to MediaCrawler; this source is intentionally a
        # high-resolution detail enhancer rather than a second search engine.
        return []
    vendor_root = Path(args.root).expanduser().resolve()
    if not (vendor_root / "source").is_dir():
        raise RuntimeError(f"XHS-Downloader source package not found: {vendor_root}")
    url = args.url or f"https://www.xiaohongshu.com/explore/{args.item_id}"
    sys.path.insert(0, str(vendor_root))
    _prepare_vendor_imports()
    from source import XHS, Settings

    settings = Settings(root=vendor_root / "Volume")
    options = settings.run().copy()
    options.update({"image_download": False, "video_download": False, "live_download": False, "download_record": False})
    from social_image_mcp.accounts import cookie_header, read_session
    saved = cookie_header("xhs", read_session("xhs", ROOT), "https://www.xiaohongshu.com/")
    if saved:
        options["cookie"] = saved
    results: list[dict[str, Any]] = []
    # XHS-Downloader logs through its own console helper. Keep stdout clean for
    # the JSON contract, including constructor/context-manager messages.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        async with XHS(**options) as client:
            extracted = await client.extract(url, download=False, check_record=False)
            if isinstance(extracted, list):
                results.extend(item for item in extracted if isinstance(item, dict))
            elif isinstance(extracted, dict):
                results.append(extracted)

    return _normalize_results(results, args.item_id, url, args.limit)


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:
        print(f"xhs-downloader bridge failed: {exc}", file=sys.stderr)
        return 2
    for item in result:
        print(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
