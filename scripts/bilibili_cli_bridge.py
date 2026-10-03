from __future__ import annotations

"""Bilibili JSON CLI bridge.

The application starts one short lived process per Bilibili request, just as
it does for the Douyin bridge. This keeps cookies, rate limits and failures in
the Bilibili lane and prevents an API exception from poisoning other sources.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from social_image_mcp.bilibili import BilibiliApi
from social_image_mcp.intent import parse_intent
from social_image_mcp.models import CreatorFetchRequest, SearchRequest


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bilibili JSON bridge")
    parser.add_argument("--query", default="")
    parser.add_argument("--item-id", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--health-check", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> None:
    timeout = float(os.getenv("BILIBILI_SOURCE_TIMEOUT_SECONDS", os.getenv("NATIVE_API_TIMEOUT_SECONDS", "12")))
    cookie = os.getenv("BILIBILI_COOKIE") or ""
    async with httpx.AsyncClient() as client:
        api = BilibiliApi(client, cookie=cookie, timeout_seconds=timeout)
        if args.health_check:
            # ``/nav`` is an account endpoint and returns -101 for a perfectly
            # usable anonymous session. Probe the public video search route so
            # startup diagnostics reflect the actual CLI retrieval path.
            payload = await api._get_wbi(
                "/x/web-interface/search/type",
                {
                    "search_type": "video", "keyword": "测试", "page": 1,
                    "page_size": 1, "order": "totalrank",
                },
            )
            print(json.dumps({
                "ok": True,
                "authenticated": bool(cookie),
                "detail": "Bilibili public search API reachable",
            }, ensure_ascii=False, separators=(",", ":")))
            return
        raw_creator = os.getenv("SOCIAL_IMAGE_CREATOR_REQUEST")
        if raw_creator:
            request = CreatorFetchRequest.model_validate_json(raw_creator)
            result = await api.fetch_creator(request)
            print(json.dumps({
                "identity": result.identity.model_dump(mode="json") | {"source": "bilibili-cli"},
                "items": [item.model_dump(mode="json") for item in result.items],
                "posts_fetched": result.posts_fetched,
                "next_cursor": result.next_cursor,
                "post_ids": list(result.post_ids),
                "rejected_posts": result.rejected_posts,
                "pages_fetched": result.pages_fetched,
                "warnings": list(result.warnings),
            }, ensure_ascii=False, separators=(",", ":")))
            return

        raw = args.url or args.item_id or args.query
        raw_request = os.getenv("SOCIAL_IMAGE_SEARCH_REQUEST")
        request = SearchRequest.model_validate_json(raw_request) if raw_request else None
        items = await api.search(
            parse_intent(raw),
            max(1, min(args.limit, 100)),
            media_type=request.media_type if request else "images",
            image_limit=request.image_limit if request else None,
            video_limit=request.video_limit if request else None,
        )
        for item in items:
            print(json.dumps(item.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")))


def main() -> int:
    _configure_stdio()
    args = _parser().parse_args()
    try:
        asyncio.run(_run(args))
    except Exception as exc:
        print(f"bilibili-cli bridge failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
