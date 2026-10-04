from __future__ import annotations

"""Small, bounded Bilibili public API client.

The client only reads public metadata and media URLs. It does not solve WBI
challenges, bypass login walls, or download media; the shared downloader owns
validation, retries and persistence.
"""

import asyncio
import hashlib
import html
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any
import httpx
from urllib.parse import urlencode

from .creator_protocol import creator_target, pack_cursor, unpack_cursor
from .models import CreatorFetchRequest, CreatorIdentity, ImageCandidate, Platform


class BilibiliError(RuntimeError):
    pass


@dataclass(frozen=True)
class BilibiliCreatorResult:
    identity: CreatorIdentity
    items: list[ImageCandidate]
    posts_fetched: int
    next_cursor: str | None = None
    post_ids: tuple[str, ...] = ()
    rejected_posts: int = 0
    pages_fetched: int = 0
    warnings: tuple[str, ...] = ()


def _text(value: Any) -> str:
    value = "" if value is None else str(value)
    value = html.unescape(value)
    return re.sub(r"<[^>]+>", "", value).strip()


def _url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("//"):
        return "https:" + value
    return value if value.startswith(("http://", "https://")) else ""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bvid(value: Any) -> str:
    value = _text(value)
    return value if value else ""


def _aid_params(identifier: str) -> dict[str, str]:
    identifier = identifier.strip()
    if identifier.lower().startswith("av"):
        return {"aid": identifier[2:]}
    if identifier.isdigit():
        return {"aid": identifier}
    return {"bvid": identifier}


class BilibiliApi:
    base_url = "https://api.bilibili.com"
    _mixin_key_table = (
        46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
        27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
        37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
        22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
    )

    def __init__(self, client: httpx.AsyncClient | None, cookie: str | None = None, timeout_seconds: float = 12) -> None:
        self.client = client
        self.cookie = cookie or ""
        self.timeout_seconds = max(2.0, float(timeout_seconds))
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }
        if self.cookie:
            self.headers["Cookie"] = self.cookie
        self._wbi_mixin_key: str | None = None
        self._wbi_key_loaded_at = 0.0

    async def _request_payload(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise BilibiliError("Bilibili API client is not initialized")
        try:
            response = await self.client.get(
                self.base_url + path,
                params=params,
                headers=self.headers,
                timeout=httpx.Timeout(self.timeout_seconds, connect=4.0),
                follow_redirects=True,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 412, 429):
                raise BilibiliError("bilibili public API temporarily rejected this request (rate limit or WBI verification); retry later or configure BILIBILI_COOKIE") from exc
            raise BilibiliError(f"bilibili api request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise BilibiliError("bilibili api returned a non-object response")
        return payload

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = await self._request_payload(path, params)
        code = payload.get("code", 0)
        if code not in (0, "0", None):
            message = _text(payload.get("message") or payload.get("msg") or "request rejected")
            raise BilibiliError(f"bilibili api error {code}: {message}")
        return payload

    async def _wbi_mixin(self, *, force: bool = False) -> str:
        """Load the public WBI key pair used by Bilibili search endpoints."""
        now = time.monotonic()
        if not force and self._wbi_mixin_key and now - self._wbi_key_loaded_at < 3600:
            return self._wbi_mixin_key
        payload = await self._request_payload("/x/web-interface/nav", {})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        image = data.get("wbi_img") if isinstance(data.get("wbi_img"), dict) else {}
        img_url = str(image.get("img_url") or "")
        sub_url = str(image.get("sub_url") or "")
        img_key = img_url.rsplit("/", 1)[-1].split(".", 1)[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".", 1)[0]
        if not img_key or not sub_key:
            raise BilibiliError("bilibili WBI keys are unavailable; configure BILIBILI_COOKIE or retry later")
        origin = img_key + sub_key
        self._wbi_mixin_key = "".join(origin[index] for index in self._mixin_key_table if index < len(origin))[:32]
        self._wbi_key_loaded_at = now
        return self._wbi_mixin_key

    async def _get_wbi(self, path: str, params: dict[str, Any], *, dm: bool = False) -> dict[str, Any]:
        """Try the public endpoint first and sign only after WBI rejection."""
        try:
            return await self._get(path, params)
        except BilibiliError as exc:
            if not self._is_wbi_rejection(exc):
                raise
        last_error: BilibiliError | None = None
        for attempt in range(2):
            mixin = await self._wbi_mixin(force=attempt > 0)
            signed = {key: value for key, value in params.items() if value is not None}
            signed["wts"] = int(time.time())
            signed.setdefault("web_location", 1550101)
            if dm:
                dm_chars = "ABCDEFGHIJK"
                signed.update({
                    "dm_img_list": "[]",
                    "dm_img_str": "".join(random.sample(dm_chars, 2)),
                    "dm_cover_img_str": "".join(random.sample(dm_chars, 2)),
                    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
                })
            filtered = {key: re.sub(r"[!'()*]", "", str(value)) for key, value in signed.items()}
            query = urlencode(sorted(filtered.items()))
            filtered["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
            try:
                return await self._get(path, filtered)
            except BilibiliError as exc:
                if not self._is_wbi_rejection(exc):
                    raise
                last_error = exc
        raise last_error or BilibiliError("bilibili WBI request failed")

    @staticmethod
    def _is_wbi_rejection(exc: BilibiliError) -> bool:
        """Recognize the several challenge codes used by Bilibili."""
        message = str(exc).lower()
        return any(marker in message for marker in (
            "temporarily rejected", "api error -412", "api error -403",
            "api error -352", "api error -799", "risk control", "验证码",
        ))

    async def _video_search_rows(self, query: str, page_size: int, page: int = 1) -> list[dict[str, Any]]:
        """Return normalized video rows with a second public endpoint fallback.

        Bilibili has two anonymous search responses in production. The typed
        endpoint is preferred because it keeps the row schema stable; the
        ``all/v2`` response is useful during transient 412 challenges and is
        the same list endpoint used by the web search page.
        """
        try:
            payload = await self._get_wbi(
                "/x/web-interface/search/type",
                {
                    "search_type": "video", "keyword": query, "page": max(1, page),
                    "page_size": min(max(page_size, 1), 50), "order": "totalrank",
                },
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            rows = data.get("result") if isinstance(data.get("result"), list) else []
            return [row for row in rows if isinstance(row, dict)]
        except BilibiliError as primary_error:
            try:
                payload = await self._get_wbi(
                    "/x/web-interface/search/all/v2",
                    {"keyword": query, "page": max(1, page), "order": "totalrank"},
                )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                blocks = data.get("result") if isinstance(data.get("result"), list) else []
                for block in blocks:
                    if not isinstance(block, dict) or block.get("result_type") != "video":
                        continue
                    rows = block.get("data") if isinstance(block.get("data"), list) else []
                    return [row for row in rows if isinstance(row, dict)][:max(page_size, 1)]
            except BilibiliError:
                pass
            raise primary_error

    async def _user_search_rows(self, query: str, page_size: int = 20) -> list[dict[str, Any]]:
        """Search Bilibili accounts by display nickname.

        The typed user endpoint is the authoritative result. ``all/v2`` is
        kept as a fallback because Bilibili occasionally returns an empty
        ``bili_user`` block while the same nickname is present in the web
        search response.
        """
        try:
            payload = await self._get_wbi(
                "/x/web-interface/search/type",
                {
                    "search_type": "bili_user", "keyword": query, "page": 1,
                    "page_size": min(max(page_size, 1), 50), "order": "totalrank",
                },
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            rows = data.get("result") if isinstance(data.get("result"), list) else []
            rows = [row for row in rows if isinstance(row, dict)]
            if rows:
                return rows
            primary_error: BilibiliError | None = None
        except BilibiliError as exc:
            primary_error = exc
        try:
            payload = await self._get_wbi(
                "/x/web-interface/search/all/v2",
                {"keyword": query, "page": 1, "order": "totalrank"},
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            blocks = data.get("result") if isinstance(data.get("result"), list) else []
            for block in blocks:
                if not isinstance(block, dict) or block.get("result_type") != "bili_user":
                    continue
                rows = block.get("data") if isinstance(block.get("data"), list) else []
                return [row for row in rows if isinstance(row, dict)][:max(page_size, 1)]
        except BilibiliError:
            pass
        if primary_error:
            raise primary_error
        return []

    @staticmethod
    def _cover_candidate(row: dict[str, Any], index: int = 1) -> ImageCandidate | None:
        post_id = _bvid(row.get("bvid") or row.get("arcurl", "").rstrip("/").rsplit("/", 1)[-1])
        if not post_id:
            post_id = _text(row.get("aid"))
        cover = _url(row.get("pic") or row.get("cover"))
        if not post_id or not cover:
            return None
        owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
        author = _text(owner.get("name") or row.get("author") or row.get("uname"))
        creator_id = _text(owner.get("mid") or row.get("mid")) or None
        permalink = _url(row.get("arcurl") or row.get("uri")) or f"https://www.bilibili.com/video/{post_id}"
        title = _text(row.get("title") or row.get("name"))
        description = _text(row.get("description") or row.get("desc") or row.get("tag"))
        published = row.get("pubdate") or row.get("created") or row.get("ctime")
        engagement = max(
            0.0,
            _number(row.get("play")) + _number(row.get("like")) * 4 + _number(row.get("favorites")) * 2,
        )
        return ImageCandidate(
            id=f"{post_id}:{index}",
            platform=Platform.BILIBILI,
            image_url=cover,
            thumbnail_url=cover,
            permalink=permalink,
            title=title,
            description=description,
            author=author,
            width=None,
            height=None,
            published_at=str(published) if published not in (None, "") else None,
            creator_id=creator_id,
            creator_name=author,
            post_id=post_id,
            media_index=index,
            engagement_score=engagement,
            source_payload={"source": "bilibili-api", "record": row},
        )

    async def _view(self, identifier: str) -> dict[str, Any]:
        try:
            payload = await self._get_wbi("/x/web-interface/view", _aid_params(identifier))
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        except BilibiliError as exc:
            # The view endpoint is frequently challenged while the signed
            # search endpoint remains available. Recover the public metadata
            # from an exact BV/AV search hit so a pasted link still works.
            try:
                rows = await self._video_search_rows(identifier, 20)
                normalized = identifier.lower()
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("bvid") or "").lower() == normalized or str(row.get("aid") or "") == identifier.lstrip("avAV"):
                        return row
                if rows and isinstance(rows[0], dict):
                    return rows[0]
            except BilibiliError:
                pass
            raise exc

    async def _video_url(self, row: dict[str, Any]) -> str | None:
        identifier = _bvid(row.get("bvid")) or _text(row.get("aid"))
        if not identifier:
            return None
        try:
            detail = await self._view(identifier)
            pages = detail.get("pages") if isinstance(detail.get("pages"), list) else []
            page = pages[0] if pages else {}
            cid = page.get("cid") if isinstance(page, dict) else None
            if not cid:
                page_payload = await self._get("/x/player/pagelist", _aid_params(identifier))
                page_rows = page_payload.get("data") if isinstance(page_payload.get("data"), list) else []
                cid = page_rows[0].get("cid") if page_rows and isinstance(page_rows[0], dict) else None
            if not cid:
                return None
            video_id_param = "bvid" if identifier.lower().startswith("bv") else "avid"
            payload = await self._get(
                "/x/player/playurl",
                # Progressive ``durl`` is a complete audio/video file. DASH
                # tracks require muxing and therefore cannot be handed to the
                # shared single-file downloader.
                {video_id_param: identifier, "cid": str(cid), "fnval": 1, "fnver": 0, "fourk": 1, "qn": 80},
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            durl = data.get("durl") if isinstance(data.get("durl"), list) else []
            urls = [_url(item.get("url")) for item in durl if isinstance(item, dict)]
            if urls:
                return urls[0]
        except BilibiliError:
            return None
        return None

    async def _original_video_candidate(self, row: dict[str, Any]) -> ImageCandidate | None:
        base = self._cover_candidate(row, 2)
        if base is None:
            return None
        video_url = await self._video_url(row)
        if not video_url:
            return None
        return base.model_copy(update={
            "id": f"{base.post_id}:video",
            "image_url": video_url,
            "media_type": "video",
            "thumbnail_url": base.image_url,
            "media_index": 2,
        })

    async def resolve_identity(self, request: CreatorFetchRequest) -> CreatorIdentity:
        requested = request.profile_url or request.creator_id or request.creator_name or ""
        if request.creator_name:
            name = request.creator_name.strip()
            try:
                rows = await self._user_search_rows(name)
            except BilibiliError:
                # Account search is often the first endpoint challenged by
                # Bilibili. Keep going because video search rows can still
                # carry an exact author name and UID.
                rows = []

            def row_name(row: dict[str, Any]) -> str:
                return _text(
                    row.get("uname") or row.get("name") or row.get("nickname")
                    or row.get("user_name") or row.get("username")
                )

            def row_mid(row: dict[str, Any]) -> str:
                return _text(row.get("mid") or row.get("uid") or row.get("user_id"))

            def normalized(value: str) -> str:
                return re.sub(r"\s+", "", value).casefold()

            requested_name = normalized(name)
            candidates = [(row_mid(row), row_name(row)) for row in rows if row_mid(row) and row_name(row)]
            exact = {mid: label for mid, label in candidates if normalized(label) == requested_name}
            if len(exact) == 1:
                target = next(iter(exact))
                matched_by = "exact_account_name"
            else:
                # Search results can contain a decorated display name or omit
                # the exact hit from the first block. Accept a unique partial
                # nickname match and keep ambiguity explicit.
                partial = {mid: label for mid, label in candidates if requested_name and requested_name in normalized(label)}
                if len(partial) == 1:
                    target = next(iter(partial))
                    matched_by = "account_name_contains"
                else:
                    target = ""
                    matched_by = ""
                    try:
                        video_rows = await self._video_search_rows(name, 50)
                    except BilibiliError:
                        video_rows = []
                    video_candidates = []
                    for row in video_rows:
                        owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
                        label = _text(owner.get("name") or row.get("author") or row.get("uname"))
                        mid = _text(owner.get("mid") or row.get("mid") or row.get("uid"))
                        if mid and label:
                            video_candidates.append((mid, label))
                    candidates = list(dict.fromkeys(candidates + video_candidates))
                    video_exact = {mid: label for mid, label in video_candidates if normalized(label) == requested_name}
                    video_partial = {mid: label for mid, label in video_candidates if requested_name and requested_name in normalized(label)}
                    if len(video_exact) == 1:
                        target = next(iter(video_exact))
                        matched_by = "video_author_name"
                    elif len(video_partial) == 1:
                        target = next(iter(video_partial))
                        matched_by = "video_author_name_contains"
                    else:
                        labels = ", ".join(f"{label}({mid})" for mid, label in candidates[:5])
                        detail = f"; candidates: {labels}" if labels else ""
                        raise BilibiliError(
                            f"creator_identity_unresolved: Bilibili nickname matched {len(exact) or len(partial) or len(video_exact) or len(video_partial)} users{detail}"
                        )
        else:
            target = creator_target("bilibili", requested)
            matched_by = "profile_url" if requested.startswith(("http://", "https://")) else "exact_uid"
        # The public card endpoint is both lighter and less likely to require
        # WBI signing than the full space profile endpoint.
        payload = await self._get_wbi("/x/web-interface/card", {"mid": target})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        profile = data.get("card") if isinstance(data.get("card"), dict) else {}
        confirmed = _text(profile.get("mid"))
        if confirmed and confirmed != target:
            raise BilibiliError("creator_identity_mismatch: Bilibili profile UID differs from the requested UID")
        return CreatorIdentity(
            platform=Platform.BILIBILI,
            requested_id=requested,
            canonical_id=target,
            name=_text(profile.get("name")),
            profile_url=f"https://space.bilibili.com/{target}",
            source="bilibili-api",
            matched_by=matched_by,
        )

    async def search(
        self,
        intent: Any,
        limit: int,
        safe_mode: bool = True,
        media_type: str = "images",
        *,
        image_limit: int | None = None,
        video_limit: int | None = None,
    ) -> list[ImageCandidate]:
        if intent.identifier and intent.identifier_platform in (None, "bilibili"):
            row = await self._view(intent.identifier)
            result: list[ImageCandidate] = []
            if media_type != "videos":
                if candidate := self._cover_candidate(row):
                    result.append(candidate)
            if media_type in ("videos", "all"):
                if candidate := await self._original_video_candidate(row):
                    result.append(candidate)
            return result
        query = intent.raw.strip()
        row_target = max(limit, image_limit or 0, video_limit or 0, 1)
        rows: list[dict[str, Any]] = []
        seen_rows: set[str] = set()
        page_limit = min(50, max(1, (row_target + 49) // 50 + 2))
        for page in range(1, page_limit + 1):
            page_rows = await self._video_search_rows(query, min(row_target, 50), page)
            added = 0
            for row in page_rows:
                row_id = _bvid(row.get("bvid")) or _text(row.get("aid"))
                if not row_id or row_id in seen_rows:
                    continue
                seen_rows.add(row_id)
                rows.append(row)
                added += 1
            if len(rows) >= row_target or not page_rows or not added:
                break
            await asyncio.sleep(max(0.1, float(os.getenv("BILIBILI_SEARCH_SLEEP_SECONDS", "0.25"))))
        result: list[ImageCandidate] = []
        image_target = max(1, image_limit or limit)
        video_target = max(1, video_limit or limit)
        video_tasks: list[asyncio.Task[ImageCandidate | None]] = []
        video_semaphore = asyncio.Semaphore(8)

        async def resolve_video(row: dict[str, Any]) -> ImageCandidate | None:
            async with video_semaphore:
                return await self._original_video_candidate(row)

        for row in rows:
            if not isinstance(row, dict):
                continue
            if media_type != "videos":
                candidate = self._cover_candidate(row)
                if candidate:
                    result.append(candidate)
            if media_type in ("videos", "all") and len(video_tasks) < video_target:
                video_tasks.append(asyncio.create_task(resolve_video(row)))
            if media_type == "images" and len(result) >= image_target:
                break
            if media_type == "all" and len(result) >= image_target and len(video_tasks) >= video_target:
                break
        if video_tasks:
            videos = await asyncio.gather(*video_tasks, return_exceptions=True)
            for video in videos:
                if isinstance(video, ImageCandidate):
                    result.append(video)
                if media_type == "videos" and len(result) >= video_target:
                    break
        if media_type == "videos":
            return result[:video_target]
        return result

    async def fetch_creator(self, request: CreatorFetchRequest) -> BilibiliCreatorResult:
        identity = await self.resolve_identity(request)
        native, _ = unpack_cursor(request.cursor)
        page = max(1, int(native or "1"))
        page_size = min(50, max(1, request.max_posts))
        warnings: list[str] = []
        fallback_only = False
        try:
            payload = await self._get_wbi(
                "/x/space/wbi/arc/search",
                {"mid": identity.canonical_id, "pn": page, "ps": page_size, "tid": 0, "keyword": ""},
                dm=True,
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            listing = data.get("list") if isinstance(data.get("list"), dict) else {}
            rows = listing.get("vlist") if isinstance(listing.get("vlist"), list) else []
            page_info = data.get("page") if isinstance(data.get("page"), dict) else {}
        except BilibiliError as exc:
            # Bilibili increasingly challenges the full creator timeline API
            # (-352/-799) even when public search and profile cards work. Use
            # the public video search as a bounded fallback before trying the
            # optional pinned-video endpoint; the search rows are filtered by
            # both UID and author name so another account cannot leak in.
            warnings.append(f"creator timeline unavailable: {exc}")
            normalized_name = re.sub(r"\s+", "", identity.name).casefold()
            matching_rows: list[dict[str, Any]] = []
            matching_ids: set[str] = set()
            empty_match_pages = 0
            search_page_size = max(page_size, 20)
            fallback_page_limit = min(50, max(3, request.max_posts))
            scanned_pages = 0
            for search_page in range(1, fallback_page_limit + 1):
                try:
                    search_rows = await self._video_search_rows(identity.name, search_page_size, search_page)
                except BilibiliError as search_exc:
                    if not matching_rows:
                        search_rows = []
                    else:
                        warnings.append(f"creator fallback search stopped at page {search_page}: {search_exc}")
                        break
                scanned_pages += 1
                new_matches = 0
                for row in search_rows:
                    owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
                    row_mid = _text(owner.get("mid") or row.get("mid"))
                    row_name = _text(owner.get("name") or row.get("author") or row.get("uname"))
                    exact_name = normalized_name and re.sub(r"\s+", "", row_name).casefold() == normalized_name
                    if (row_mid and row_mid != identity.canonical_id) or (not row_mid and not exact_name):
                        continue
                    row_id = _bvid(row.get("bvid")) or _text(row.get("aid"))
                    if not row_id or row_id in matching_ids:
                        continue
                    matching_ids.add(row_id)
                    matching_rows.append(row)
                    new_matches += 1
                if len(matching_rows) >= request.max_posts or not search_rows:
                    break
                empty_match_pages = 0 if new_matches else empty_match_pages + 1
                if empty_match_pages >= 3:
                    break
                await asyncio.sleep(max(0.1, float(os.getenv("BILIBILI_CREATOR_SLEEP_SECONDS", "0.5"))))
            if matching_rows:
                rows = matching_rows[:request.max_posts]
                page_info = {"count": len(matching_rows)}
                fallback_only = True
                if len(rows) < request.max_posts:
                    warnings.append(
                        f"creator fallback search scanned {scanned_pages} pages and matched "
                        f"{len(rows)} of {request.max_posts} requested posts"
                    )
            else:
                try:
                    fallback = await self._get("/x/space/top/arc", {"vmid": identity.canonical_id})
                except BilibiliError as fallback_exc:
                    raise BilibiliError(f"bilibili creator timeline failed: {exc}; public video search and top-video fallback failed: {fallback_exc}") from fallback_exc
                fallback_data = fallback.get("data") if isinstance(fallback.get("data"), dict) else {}
                rows = [fallback_data] if fallback_data else []
                page_info = {"count": len(rows)}
                fallback_only = True
        items: list[ImageCandidate] = []
        post_ids: list[str] = []
        video_tasks: list[tuple[dict[str, Any], asyncio.Task[ImageCandidate | None]]] = []
        video_semaphore = asyncio.Semaphore(max(1, min(8, request.max_concurrency)))

        async def resolve_video(row: dict[str, Any]) -> ImageCandidate | None:
            async with video_semaphore:
                return await self._original_video_candidate(row)

        for row in rows:
            if not isinstance(row, dict):
                continue
            post_id = _bvid(row.get("bvid")) or _text(row.get("aid"))
            if post_id:
                post_ids.append(post_id)
            if request.media_type != "videos":
                candidate = self._cover_candidate(row)
                if candidate:
                    items.append(candidate.model_copy(update={"creator_id": identity.canonical_id, "creator_name": identity.name, "author": identity.name}))
            if request.media_type in ("videos", "all"):
                video_tasks.append((row, asyncio.create_task(resolve_video(row))))
        if video_tasks:
            results = await asyncio.gather(*(task for _, task in video_tasks), return_exceptions=True)
            for (row, _), result in zip(video_tasks, results):
                if isinstance(result, Exception) or result is None:
                    warnings.append(f"video URL unavailable: {_bvid(row.get('bvid')) or _text(row.get('aid'))}")
                    continue
                items.append(result.model_copy(update={"creator_id": identity.canonical_id, "creator_name": identity.name, "author": identity.name}))
        total = int(_number(page_info.get("count"), 0))
        next_cursor = pack_cursor(page + 1) if rows and not fallback_only and (not total or page * page_size < total) else None
        return BilibiliCreatorResult(
            identity=identity,
            items=items,
            posts_fetched=len(rows),
            next_cursor=next_cursor,
            post_ids=tuple(dict.fromkeys(post_ids)),
            pages_fetched=1,
            warnings=tuple(warnings),
        )
