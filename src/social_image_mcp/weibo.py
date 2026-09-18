from __future__ import annotations

"""Bounded, metadata-only access to Weibo's mobile public JSON endpoints."""

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .bridge_utils import normalize_native_record
from .creator_protocol import creator_target, pack_cursor, unpack_cursor
from .models import CreatorFetchRequest, CreatorIdentity, ImageCandidate, Platform


class WeiboError(RuntimeError):
    pass


@dataclass(frozen=True)
class WeiboCreatorResult:
    identity: CreatorIdentity
    items: list[ImageCandidate]
    posts_fetched: int
    next_cursor: str | None = None
    post_ids: tuple[str, ...] = ()
    rejected_posts: int = 0
    pages_fetched: int = 0
    warnings: tuple[str, ...] = ()


def _text(value: Any) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(str(value or ""))).strip()


def _cards(value: Any) -> list[dict[str, Any]]:
    """Flatten card groups while retaining only actual Weibo post cards."""
    result: list[dict[str, Any]] = []
    if isinstance(value, list):
        for child in value:
            result.extend(_cards(child))
    elif isinstance(value, dict):
        if isinstance(value.get("mblog"), dict):
            result.append(value["mblog"])
        for key in ("cards", "card_group"):
            if isinstance(value.get(key), list):
                result.extend(_cards(value[key]))
    return result


class WeiboApi:
    base_url = "https://m.weibo.cn"

    def __init__(self, client: httpx.AsyncClient | None, cookie: str | None = None, timeout_seconds: float = 12) -> None:
        self.client = client
        self.timeout_seconds = max(2.0, float(timeout_seconds))
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            "Referer": "https://m.weibo.cn/",
            "Accept": "application/json, text/plain, */*",
        }
        if cookie:
            self.headers["Cookie"] = cookie

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.client is None:
            raise WeiboError("Weibo API client is not initialized")
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
            raise WeiboError(f"weibo public api request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise WeiboError("weibo public api returned a non-object response")
        if payload.get("ok") not in (1, "1", True):
            raise WeiboError(f"weibo public api rejected request: {_text(payload.get('msg') or 'unknown error')}")
        return payload

    @staticmethod
    def _candidates(row: dict[str, Any], identity: CreatorIdentity | None = None) -> list[ImageCandidate]:
        source = "weibo-public-api"
        values = normalize_native_record("weibo", {"mblog": row}, source, media_type="images")
        result: list[ImageCandidate] = []
        for index, value in enumerate(values, 1):
            if not isinstance(value, dict) or not value.get("image_url"):
                continue
            value["platform"] = Platform.WEIBO
            value["media_type"] = "image"
            value["media_index"] = int(value.get("media_index") or index)
            value["post_id"] = str(value.get("post_id") or row.get("bid") or row.get("idstr") or row.get("id") or "")
            value["id"] = f"{value['post_id']}:{value['media_index']}"
            value["title"] = _text(value.get("title") or row.get("text"))
            value["description"] = _text(value.get("description") or row.get("text"))
            value["source_payload"] = {"source": source, "record": row}
            if identity:
                value.update({"creator_id": identity.canonical_id, "creator_name": identity.name, "author": identity.name})
            try:
                result.append(ImageCandidate.model_validate(value))
            except ValueError:
                continue
        return result

    async def resolve_identity(self, request: CreatorFetchRequest) -> CreatorIdentity:
        requested = request.profile_url or request.creator_id or request.creator_name or ""
        if request.creator_name:
            payload = await self._get(
                "/api/container/getIndex",
                {"containerid": f"100103type=3&q={request.creator_name.strip()}", "page_type": "searchall", "page": 1},
            )
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            users: list[dict[str, Any]] = []
            def collect(value: Any) -> None:
                if isinstance(value, dict):
                    user = value.get("user")
                    if isinstance(user, dict) and (user.get("id") or user.get("idstr")):
                        users.append(user)
                    if value.get("screen_name") and (value.get("id") or value.get("idstr")):
                        users.append(value)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(data.get("cards"))
            unique = {}
            for user in users:
                name = _text(user.get("screen_name") or user.get("name"))
                uid = str(user.get("id") or user.get("idstr") or "")
                if name == request.creator_name.strip() and uid:
                    unique[uid] = user
            if len(unique) != 1:
                raise WeiboError(f"creator_identity_unresolved: exact nickname matched {len(unique)} users")
            uid = next(iter(unique))
            profile = unique[uid]
            return CreatorIdentity(
                platform=Platform.WEIBO, requested_id=requested, canonical_id=uid,
                name=_text(profile.get("screen_name") or profile.get("name")),
                profile_url=f"https://weibo.com/u/{uid}", source="weibo-public-api", matched_by="exact_account_name",
            )
        uid = creator_target("weibo", requested)
        payload = await self._get(
            "/api/container/getIndex",
            {"type": "uid", "value": uid, "containerid": f"100505{uid}"},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        profile = data.get("userInfo") if isinstance(data.get("userInfo"), dict) else {}
        confirmed = str(profile.get("id") or profile.get("idstr") or uid)
        if confirmed != uid:
            raise WeiboError("creator_identity_mismatch: Weibo profile UID differs from the requested UID")
        return CreatorIdentity(
            platform=Platform.WEIBO,
            requested_id=requested,
            canonical_id=uid,
            name=_text(profile.get("screen_name")),
            profile_url=f"https://weibo.com/u/{uid}",
            source="weibo-public-api",
            matched_by="profile_url" if requested.startswith(("http://", "https://")) else "exact_uid",
        )

    async def search(self, intent: Any, limit: int, safe_mode: bool = True) -> list[ImageCandidate]:
        if intent.identifier and intent.identifier_platform in (None, "weibo"):
            payload = await self._get("/statuses/show", {"id": intent.identifier})
            row = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            return self._candidates(row)[:limit]
        payload = await self._get(
            "/api/container/getIndex",
            {"containerid": f"100103type=1&q={intent.raw.strip()}", "page_type": "searchall", "page": 1},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        result: list[ImageCandidate] = []
        for row in _cards(data.get("cards")):
            result.extend(self._candidates(row))
            if len(result) >= limit:
                break
        return result[:limit]

    async def fetch_creator(self, request: CreatorFetchRequest) -> WeiboCreatorResult:
        identity = await self.resolve_identity(request)
        native, _ = unpack_cursor(request.cursor)
        page = max(1, int(native or "1"))
        payload = await self._get(
            "/api/container/getIndex",
            {"type": "uid", "value": identity.canonical_id, "containerid": f"107603{identity.canonical_id}", "page": page},
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = _cards(data.get("cards"))[: request.max_posts]
        items: list[ImageCandidate] = []
        post_ids: list[str] = []
        rejected = 0
        for row in rows:
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            author_id = str(user.get("id") or user.get("idstr") or "")
            if author_id and author_id != identity.canonical_id:
                rejected += 1
                continue
            post_id = str(row.get("bid") or row.get("idstr") or row.get("id") or "")
            if post_id:
                post_ids.append(post_id)
            if request.media_type != "videos":
                items.extend(self._candidates(row, identity))
        info = data.get("cardlistInfo") if isinstance(data.get("cardlistInfo"), dict) else {}
        has_more = bool(info.get("since_id")) or len(rows) >= request.max_posts
        next_cursor = pack_cursor(page + 1) if rows and has_more else None
        warnings = ("Weibo public API exposes image posts only; video requests require the MediaCrawler fallback",) if request.media_type in ("videos", "all") else ()
        return WeiboCreatorResult(
            identity=identity,
            items=items,
            posts_fetched=len(rows),
            next_cursor=next_cursor,
            post_ids=tuple(dict.fromkeys(post_ids)),
            rejected_posts=rejected,
            pages_fetched=1,
            warnings=warnings,
        )
