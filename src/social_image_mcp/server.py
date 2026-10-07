from __future__ import annotations

import argparse
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .models import CreatorFetchRequest, DownloadRequest, ImageCandidate, Platform, SearchRequest
from .intent import parse_intent, requested_media_limit
from .progress import report_progress
from .service import SocialImageService
from .storage import StorageAgent

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


@mcp.tool(description="Search social platforms by a keyword, content ID, or post URL. Set media_type=images (default), videos, or all to choose downloadable media. Paste a full non-social http(s) webpage URL and select platform=other to retrieve content images and direct video files from that homepage and its same-site content pages; max_posts bounds detail pages. Icons and logos are filtered. For an X creator, use @username, from:username, or x-user:username so the request uses the user's media timeline; do not use a plain X keyword search for a creator timeline. Douyin queries such as douyin-user:<handle> or douyin-name:<nickname> route to exact account retrieval; media_type=videos can return original video files for creator queries.")
async def search_images(query: str, platforms: list[str] | None = None, max_results: int = 20, min_width: int = 0, min_height: int = 0, safe_mode: bool = True, use_cache: bool = True, download: bool = False, output_dir: str | None = None, retrieval_mode: str = "sources", content_query: str | None = None, filter_mode: str = "off", quality_mode: str = "fast", media_type: str = "images", creator_name: str | None = None, creator_id: str | None = None, image_limit: int | None = None, video_limit: int | None = None, per_post_limit: int | None = None, max_posts: int = 20) -> dict[str, Any]:
    report_progress("preparing", "正在识别输入内容和目标平台。")
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
            include_video_covers=media_type == "images",
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
            quality_mode=quality_mode, media_type=media_type,
            include_video_covers=media_type == "images",
            download=download, output_dir=output_dir,
        ))
    selected = [Platform(value) for value in platforms] if platforms else None
    image_target = (image_limit or max_results) if media_type != "videos" else 0
    video_target = (video_limit or max_results) if media_type != "images" else 0
    # Download completion is measured by successfully saved files, not by the
    # number of URLs returned by a platform. Within max_posts, retain every
    # candidate that can replace an expired, duplicate or rejected URL. Exact
    # post URLs remain bounded to their fixed media set.
    if download and intent.identifier_scope != "post":
        images_per_post = per_post_limit or 10
        candidate_image_limit = min(1000, max(image_target, max_posts * images_per_post)) if image_target else None
        candidate_video_limit = min(1000, max(video_target, max_posts)) if video_target else None
    else:
        candidate_image_limit = image_target or None
        candidate_video_limit = video_target or None
    candidate_total = (candidate_image_limit or 0) + (candidate_video_limit or 0)
    request = SearchRequest(
        query=query, platforms=selected, max_results=max(1, candidate_total),
        min_width=min_width, min_height=min_height, safe_mode=safe_mode,
        use_cache=use_cache, retrieval_mode=retrieval_mode, media_type=media_type,
        image_limit=candidate_image_limit, video_limit=candidate_video_limit,
        per_post_limit=per_post_limit, max_posts=max_posts,
    )
    report_progress("retrieving", "正在检索平台内容；若来源支持分页，程序会自动继续读取后续页面。")
    result = await service.search(request)
    items = result.get("items") or []
    report_progress(
        "retrieving",
        f"检索已返回 {len(items)} 个候选媒体。",
        images_found=sum(1 for item in items if item.get("media_type") != "video"),
        videos_found=sum(1 for item in items if item.get("media_type") == "video"),
        pages_fetched=int(result.get("pages_fetched") or 0),
        posts_fetched=int(result.get("posts_fetched") or 0),
    )
    if download and result["items"]:
        candidates = [ImageCandidate.model_validate(item) for item in result["items"]]
        records = []
        attempted: list[ImageCandidate] = []
        targets = {"image": image_target, "video": video_target}
        batch_sizes = {
            "image": max(1, int(getattr(service.settings, "image_download_batch_size", 10))),
            "video": max(1, int(service.settings.video_download_batch_size)),
        }
        storage_root = output_dir or getattr(service.settings, "output_dir", "downloads")
        storage = StorageAgent(storage_root)
        grouped: dict[tuple[str, str], list[ImageCandidate]] = {}
        for item in candidates:
            account_dir = storage.account_dir(
                item.platform,
                candidate=item,
                query=query,
                override=storage_root,
            )
            grouped.setdefault((item.platform.value, str(account_dir)), []).append(item)
        records_by_group: dict[tuple[str, str], list[dict]] = {}
        info_paths: list[str] = []
        for (platform_name, account_dir_text), group in grouped.items():
            successful = {"image": 0, "video": 0}
            account_dir = Path(account_dir_text)
            total_target = image_target + video_target
            for kind in ("image", "video"):
                pool = [item for item in group if item.media_type == kind]
                cursor = 0
                batch_number = 0
                while successful[kind] < targets[kind] and cursor < len(pool):
                    remaining = targets[kind] - successful[kind]
                    batch_size = min(batch_sizes[kind], remaining)
                    batch = pool[cursor:cursor + batch_size]
                    cursor += len(batch)
                    if not batch:
                        break
                    batch_number += 1
                    attempted.extend(batch)
                    media_name = "视频" if kind == "video" else "图片"
                    report_progress(
                        "downloading",
                        f"{platform_name}/{account_dir.name}：正在下载第 {batch_number} 批{media_name}（本批 {len(batch)} 个）；完成后再处理下一批。",
                        download_completed=len(records), download_total=total_target,
                    )
                    concurrency = (
                        min(len(batch), max(1, int(service.settings.video_download_concurrency)))
                        if kind == "video" else min(len(batch), max(1, int(getattr(service.settings, "image_download_concurrency", 3))))
                    )
                    batch_records = await service.download(
                        batch, account_dir, concurrency, min_width, min_height,
                        resume=True,
                    )
                    records.extend(batch_records)
                    records_by_group.setdefault((platform_name, account_dir_text), []).extend(
                        record.model_dump(mode="json") for record in batch_records
                    )
                    successful[kind] += sum(
                        record.status in {"downloaded", "existing"}
                        for record in batch_records
                    )
                    report_progress(
                        "downloading",
                        f"{platform_name}/{account_dir.name}：第 {batch_number} 批{media_name}已完成，当前有效 {successful[kind]}/{targets[kind]} 个。",
                        download_completed=len(records), download_total=total_target,
                    )
            info_paths.append(str(storage.write_index(
                account_dir, platform_name,
                records=records_by_group.get((platform_name, account_dir_text), []),
            )))
        result["items"] = [item.model_dump(mode="json") for item in attempted]
        result["downloads"] = [record.model_dump(mode="json") for record in records]
        result["output_dirs"] = {
            f"{platform}/{Path(path).name}": path
            for platform, path in ((key[0], key[1]) for key in grouped)
        }
        result["info_paths"] = info_paths
        result["output_dir"] = next(iter(result["output_dirs"].values()), str(Path(storage_root).resolve()))
        counts = {name: 0 for name in ("downloaded", "existing", "duplicate", "rejected", "failed")}
        for record in records:
            counts[record.status if record.status in counts else "failed"] += 1
        report_progress(
            "finalizing", "下载已完成，正在整理结果。",
            download_completed=len(records), download_total=total_target, **counts,
        )
    else:
        report_progress("finalizing", "检索已完成，正在整理结果。")
    return result


@mcp.tool(description="Download creator media from Douyin, Weibo, Bilibili or X, NOT a post ID. Douyin accepts an exact account handle/UID/sec_uid via creator_id, an exact nickname via creator_name, or a full profile URL via profile_url. If a Douyin handle or nickname cannot be found, retry with the complete https://www.douyin.com/user/<sec_uid> profile URL because it is more stable. X accepts a username such as jwj180 or a full https://x.com/<username> profile URL; X requires a Bearer Token or authenticated gallery-dl cookie. Weibo accepts a numeric UID or full profile URL; Bilibili accepts a numeric UID, full profile URL, or exact account nickname via creator_name. media_type=images downloads image galleries, media_type=videos downloads original video files, and media_type=all returns both. By default this is fast account-only retrieval with no semantic or visual filtering. To keep only a content theme, provide content_query; the service classifies coarse objects such as person, clothing, landscape, scene, architecture and body regions, then applies include/exclude/required rules. Use filter_mode=optional to fall back when the local model is unavailable, or required to fail closed. Downloads are bounded by max_posts and max_images. Repeat with resume=true and the same options to continue pending media.")
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
    storage_root = request.output_dir or getattr(service.settings, "output_dir", "downloads")
    storage = StorageAgent(storage_root)
    batch_sizes = {
        "image": max(1, int(getattr(service.settings, "image_download_batch_size", 10))),
        "video": max(1, int(getattr(service.settings, "video_download_batch_size", 3))),
    }
    groups: dict[tuple[str, str], list[ImageCandidate]] = {}
    for item in request.items:
        account_dir = storage.account_dir(item.platform, candidate=item, override=storage_root)
        groups.setdefault((item.platform.value, str(account_dir)), []).append(item)
    records = []
    info_paths = []
    output_dirs = {}
    for (platform_name, account_dir_text), group in groups.items():
        account_dir = Path(account_dir_text)
        group_records = []
        for kind in ("image", "video"):
            pool = [item for item in group if item.media_type == kind]
            for offset in range(0, len(pool), batch_sizes[kind]):
                batch = pool[offset:offset + batch_sizes[kind]]
                concurrency = min(
                    len(batch),
                    max(1, int(getattr(service.settings, "video_download_concurrency", 1)))
                    if kind == "video" else max(1, int(request.max_concurrency)),
                )
                report_progress(
                    "downloading",
                    f"{platform_name}/{account_dir.name}：正在下载第 {offset // batch_sizes[kind] + 1} 批{'视频' if kind == 'video' else '图片'}（本批 {len(batch)} 个）。",
                )
                batch_records = await service.download(
                    batch, account_dir, concurrency, request.min_width, request.min_height,
                    resume=True,
                )
                group_records.extend(batch_records)
                records.extend(batch_records)
        report_progress(
            "downloading",
            f"{platform_name}/{account_dir.name}：下载完成，共处理 {len(group_records)} 个文件。",
        )
        info_paths.append(str(storage.write_index(
            account_dir, platform_name,
            records=[record.model_dump(mode="json") for record in group_records],
        )))
        output_dirs[f"{platform_name}/{account_dir.name}"] = str(account_dir)
    return {
        "output_dir": next(iter(output_dirs.values()), str(Path(storage_root).resolve())),
        "output_dirs": output_dirs,
        "info_paths": info_paths,
        "records": [record.model_dump(mode="json") for record in records],
    }


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
