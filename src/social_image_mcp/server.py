from __future__ import annotations

import argparse
import json
import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .models import CreatorFetchRequest, DownloadRequest, Platform, SearchRequest
from .intent import parse_intent, requested_media_limit
from .service import SocialImageService

service = SocialImageService()


@asynccontextmanager
async def _lifespan(_: FastMCP):
    await service.start()
    try:
        yield {}
    finally:
        await service.close()


mcp = FastMCP(
    name="social-image-mcp",
    instructions="Semantic social platform image search and download",
    lifespan=_lifespan,
    log_level="WARNING",
)


def _configure_stdio() -> None:
    """Use UTF-8 for the JSON-RPC stream on Windows."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


@mcp.tool(description="Search social platforms by a keyword, content ID, or post URL. Set media_type=images (default), videos, or all to choose downloadable media. Paste a full non-social http(s) webpage URL and select platform=other to extract images from that page for download. For an X creator, use @username, from:username, or x-user:username so the request uses the user's media timeline; do not use a plain X keyword search for a creator timeline. Douyin queries such as douyin-user:<handle> or douyin-name:<nickname> route to exact account retrieval; media_type=videos can return original video files for creator queries.")
async def search_images(query: str, platforms: list[str] | None = None, max_results: int = 20, min_width: int = 0, min_height: int = 0, safe_mode: bool = True, use_cache: bool = True, download: bool = False, output_dir: str | None = None, retrieval_mode: str = "sources", content_query: str | None = None, filter_mode: str = "off", quality_mode: str = "fast", media_type: str = "images", creator_name: str | None = None, creator_id: str | None = None, image_limit: int | None = None, video_limit: int | None = None, per_post_limit: int | None = None, max_posts: int = 20) -> dict[str, Any]:
    intent = parse_intent(query)
    max_results = requested_media_limit(query, max_results)
    if creator_name or creator_id:
        if not platforms or len(platforms) != 1:
            raise ValueError("按昵称采集时请只选择一个平台")
        return await service.fetch_creator(CreatorFetchRequest(
            platform=Platform(platforms[0]), creator_name=creator_name, creator_id=creator_id,
            max_images=image_limit or max_results, max_videos=video_limit,
            max_posts=max_posts,
            per_post_limit=per_post_limit, media_type=media_type,
            content_query=content_query, filter_mode=filter_mode,
            quality_mode=quality_mode, download=download, output_dir=output_dir,
        ))
    if intent.identifier_scope in {"creator", "creator_name"} and platforms != ["other"]:
        if platforms and platforms != [intent.identifier_platform]:
            raise ValueError("creator platform conflicts with platforms; use fetch_creator_images")
        return await service.fetch_creator(CreatorFetchRequest(
            platform=Platform(intent.identifier_platform),
            creator_id=intent.identifier if intent.identifier_scope == "creator" and intent.identifier_platform not in {"xhs", "instagram"} else None,
            creator_name=intent.identifier if intent.identifier_scope == "creator_name" else None,
            profile_url=intent.url if intent.identifier_scope == "creator" and intent.identifier_platform in {"xhs", "instagram"} else None,
            max_posts=max_posts,
            max_images=image_limit or max_results, max_videos=video_limit,
            per_post_limit=per_post_limit, min_width=min_width, min_height=min_height,
            safe_mode=safe_mode, content_query=content_query, filter_mode=filter_mode,
            quality_mode=quality_mode, media_type=media_type, download=download, output_dir=output_dir,
        ))
    selected = [Platform(value) for value in platforms] if platforms else None
    request = SearchRequest(query=query, platforms=selected, max_results=max_results, min_width=min_width, min_height=min_height, safe_mode=safe_mode, use_cache=use_cache, retrieval_mode=retrieval_mode, media_type=media_type, image_limit=image_limit, video_limit=video_limit, per_post_limit=per_post_limit)
    result = await service.search(request)
    if download and result["items"]:
        download_request = DownloadRequest(items=result["items"], output_dir=output_dir, min_width=min_width, min_height=min_height)
        records = await service.download(download_request.items, download_request.output_dir, download_request.max_concurrency, download_request.min_width, download_request.min_height)
        result["downloads"] = [record.model_dump(mode="json") for record in records]
        result["output_dir"] = str(service.settings.ensure_output_dir(output_dir).resolve())
    return result


@mcp.tool(description="Download creator media from Douyin, Weibo, Bilibili or X, NOT a post ID. Douyin accepts an exact account handle/UID/sec_uid via creator_id, an exact nickname via creator_name, or a full profile URL via profile_url. If a Douyin handle or nickname cannot be found, retry with the complete https://www.douyin.com/user/<sec_uid> profile URL because it is more stable. X accepts a username such as jwj180 or a full https://x.com/<username> profile URL; X requires a Bearer Token or authenticated gallery-dl cookie. Weibo and Bilibili accept numeric UID or full profile URL. media_type=images downloads image galleries, media_type=videos downloads original video files, and media_type=all returns both. By default this is fast account-only retrieval with no semantic or visual filtering. To keep only a content theme, provide content_query; the service classifies coarse objects such as person, clothing, landscape, scene, architecture and body regions, then applies include/exclude/required rules. Use filter_mode=optional to fall back when the local model is unavailable, or required to fail closed. Downloads are bounded by max_posts and max_images. Repeat with resume=true and the same options to continue pending media.")
async def fetch_creator_images(platform: str, creator_id: str | None = None, creator_name: str | None = None, profile_url: str | None = None,
                               max_posts: int = 20, max_images: int = 50, since: str | None = None,
                               max_videos: int | None = None, per_post_limit: int | None = None,
                               until: str | None = None, sort: str = "recent", include_video_covers: bool = False,
                               media_type: str = "images",
                               content_query: str | None = None, filter_mode: str = "off", quality_mode: str = "fast",
                               download: bool = True, output_dir: str | None = None, resume: bool = True,
                               cursor: str | None = None, min_width: int = 0, min_height: int = 0,
                               max_concurrency: int = 5, safe_mode: bool = True) -> dict[str, Any]:
    request = CreatorFetchRequest(platform=platform, creator_id=creator_id, creator_name=creator_name, profile_url=profile_url,
                                  max_posts=max_posts, max_images=max_images, max_videos=max_videos, per_post_limit=per_post_limit, since=since, until=until, sort=sort,
                                  include_video_covers=include_video_covers, content_query=content_query,
                                  media_type=media_type,
                                  filter_mode=filter_mode, quality_mode=quality_mode, download=download, output_dir=output_dir,
                                  resume=resume, cursor=cursor, min_width=min_width, min_height=min_height,
                                  max_concurrency=max_concurrency, safe_mode=safe_mode)
    return await service.fetch_creator(request)


@mcp.tool(description="Download ranked image candidates returned by search_images. Performs retries, image validation, minimum-size filtering and content-hash deduplication.")
async def download_images(items: list[dict[str, Any]], output_dir: str | None = None, max_concurrency: int = 5, min_width: int = 0, min_height: int = 0) -> dict[str, Any]:
    request = DownloadRequest(items=items, output_dir=output_dir, max_concurrency=max_concurrency, min_width=min_width, min_height=min_height)
    records = await service.download(request.items, request.output_dir, request.max_concurrency, request.min_width, request.min_height)
    return {"output_dir": str(service.settings.ensure_output_dir(request.output_dir).resolve()), "records": [record.model_dump(mode="json") for record in records]}


@mcp.tool(description="Inspect a single platform item by ID and return its image candidates.")
async def inspect_item(platform: str, item_id: str) -> dict[str, Any]:
    return await service.inspect(Platform(platform), item_id)


@mcp.tool(description="List supported platforms and their effective availability through recommended source projects or optional legacy adapters.")
def list_platforms() -> dict[str, Any]:
    return {"platforms": service.statuses()}


@mcp.tool(description="List recommended source projects and their configuration status. Sources are used for candidate recall; AI ranking is handled by this MCP server.")
def list_sources() -> dict[str, Any]:
    return {"sources": service.source_statuses()}


@mcp.tool(description="Record whether a returned image matched the user's intent. This local feedback is used to rerank the same type of future results.")
def submit_feedback(query: str, platform: str, candidate_id: str, accepted: bool, image_url: str = "") -> dict[str, Any]:
    return service.record_feedback(query, Platform(platform), candidate_id, accepted, image_url)


def _diagnostics() -> dict[str, Any]:
    object_filter = service.object_detector.status if service.object_detector else {
        "configured": bool(service.settings.object_model or service.settings.vision_model),
        "ready": False,
        "backend": "yolo" if service.settings.object_model else ("clip" if service.settings.vision_model else None),
        "model": service.settings.object_model or service.settings.vision_model,
        "label_map_size": 0,
        "error": None,
    }
    return {
        "package": "social-image-mcp",
        "python": sys.version.split()[0],
        "tools": [tool.name for tool in mcp._tool_manager.list_tools()],
        "platforms": service.statuses(),
        "sources": service.source_statuses(),
        "object_filter": object_filter,
        "message": "MCP stdio is ready; connect with an MCP client instead of typing JSON manually",
    }


def main() -> None:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Social Image MCP server")
    parser.add_argument("--check", action="store_true", help="Print installation and source diagnostics as JSON, then exit")
    parser.add_argument("--version", action="store_true", help="Print the package version, then exit")
    args = parser.parse_args()
    if args.version:
        print("0.1.0")
        return
    if args.check:
        print(json.dumps(_diagnostics(), ensure_ascii=False, indent=2))
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        print("social-image-mcp is an MCP stdio server. Use --check for diagnostics, or launch it from an MCP client.", file=sys.stderr)
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
