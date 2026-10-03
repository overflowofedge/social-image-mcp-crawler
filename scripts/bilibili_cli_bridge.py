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


async def _fetch_creator_pages(api: BilibiliApi, request: CreatorFetchRequest) -> dict:
    """Collect bounded creator pages inside the isolated Bilibili process."""
    cursor = request.cursor
    remaining = request.max_posts
    identity = None
    items: dict[str, object] = {}
    post_ids: list[str] = []
    warnings: list[str] = []
    rejected_posts = 0
    posts_fetched = 0
    pages_fetched = 0
    page_limit = min(50, max(1, (request.max_posts + 9) // 10))
    while remaining > 0 and pages_fetched < page_limit:
        page_request = request.model_copy(update={"cursor": cursor, "max_posts": remaining})
        result = await api.fetch_creator(page_request)
        identity = identity or result.identity
        for item in result.items:
            items[item.stable_key] = item
        post_ids.extend(result.post_ids)
        warnings.extend(result.warnings)
        rejected_posts += result.rejected_posts
        posts_fetched += result.posts_fetched
        pages_fetched += result.pages_fetched
        remaining -= result.posts_fetched
        cursor = result.next_cursor
        if not cursor or result.posts_fetched <= 0:
            break
        await asyncio.sleep(max(0.1, float(os.getenv("BILIBILI_CREATOR_SLEEP_SECONDS", "0.5"))))
    if identity is None:
        raise RuntimeError("Bilibili creator retrieval returned no identity")
    return {
        "identity": identity.model_dump(mode="json") | {"source": "bilibili-cli"},
        "items": [item.model_dump(mode="json") for item in items.values()],
        "posts_fetched": posts_fetched,
        "next_cursor": cursor,
        "post_ids": list(dict.fromkeys(post_ids)),
        "rejected_posts": rejected_posts,
        "pages_fetched": pages_fetched,
        "warnings": list(dict.fromkeys(warnings)),
    }


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
            result = await _fetch_creator_pages(api, request)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return

        raw = args.url or args.item_id or args.query
        raw_request = os.getenv("SOCIAL_IMAGE_SEARCH_REQUEST")
        request = SearchRequest.model_validate_json(raw_request) if raw_request else None
        items = await api.search(
            parse_intent(raw),
            max(1, min(args.limit, 1000)),
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
