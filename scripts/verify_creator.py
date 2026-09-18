from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify creator media retrieval through the current MCP stdio server")
    parser.add_argument("--platform", required=True, choices=("douyin", "weibo", "bilibili", "x"))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--creator-id", help="Douyin handle/sec_uid or numeric Weibo UID")
    target.add_argument("--creator-name", help="Exact Douyin nickname")
    target.add_argument("--profile-url", help="Full Douyin or Weibo creator profile URL")
    parser.add_argument("--max-posts", type=int, default=5)
    parser.add_argument("--max-images", type=int, default=10)
    parser.add_argument("--output-dir", default="downloads/verify-creator")
    parser.add_argument("--download", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--cursor")
    parser.add_argument("--media-type", choices=("images", "videos", "all"), default="images")
    return parser


async def main(args: argparse.Namespace) -> int:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "scripts" / "run_mcp.py")],
        env=env,
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    request = {
        "platform": args.platform,
        "max_posts": args.max_posts,
        "max_images": args.max_images,
        "download": args.download,
        "resume": args.resume,
        "output_dir": args.output_dir,
        "media_type": args.media_type,
    }
    if args.creator_id:
        request["creator_id"] = args.creator_id
    if args.profile_url:
        request["profile_url"] = args.profile_url
    if args.creator_name:
        request["creator_name"] = args.creator_name
    if args.cursor:
        request["cursor"] = args.cursor

    started = time.perf_counter()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(
                "fetch_creator_images", request,
                read_timeout_seconds=timedelta(seconds=150),
            )
            payload = json.loads(result.content[0].text)

    downloads = payload.get("downloads", [])
    successful_statuses = {"downloaded", "existing", "duplicate"}
    report = {
        "tools": [tool.name for tool in tools.tools],
        "platform": payload.get("platform"),
        "media_type": payload.get("media_type"),
        "identity": payload.get("identity"),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "item_count": len(payload.get("items", [])),
        "posts_fetched": payload.get("posts_fetched", 0),
        "post_ids": payload.get("post_ids", []),
        "pages_fetched": payload.get("pages_fetched", 0),
        "pending_images": payload.get("pending_images", 0),
        "has_more": payload.get("has_more", False),
        "next_cursor": payload.get("next_cursor"),
        "semantic_applied": payload.get("semantic_applied"),
        "vision_applied": payload.get("vision_applied"),
        "status": payload.get("status"),
        "warnings": payload.get("warnings", []),
        "error": payload.get("error"),
        "downloads": [
            {key: item.get(key) for key in ("candidate_id", "media_type", "path", "width", "height", "status", "error")}
            for item in downloads
        ],
    }
    reasons: list[str] = []
    if report["error"]:
        reasons.append(f"error={report['error'].get('code', 'unknown')}")
    if not report["identity"]:
        reasons.append("identity_missing")
    if report["item_count"] == 0:
        reasons.append("item_count=0")
    if args.download and not any(item.get("status") in successful_statuses for item in downloads):
        reasons.append("no_successful_download")
    report["ok"] = not reasons
    if reasons:
        report["failure_reasons"] = reasons
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(_parser().parse_args())))
