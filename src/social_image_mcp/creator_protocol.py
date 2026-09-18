from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote, urlparse

from .bridge_utils import normalize_native_record
from .models import CreatorIdentity, Platform


def creator_target(platform: str, value: str) -> str:
    """Parse explicit account inputs without guessing from nicknames or post URLs."""
    value = value.strip()
    prefix = re.match(r"^(douyin|weibo|bilibili|bili|b站|x|twitter)-user[:：]", value, re.I)
    if prefix:
        prefix_platform = {"bili": "bilibili", "b站": "bilibili"}.get(prefix.group(1).lower(), prefix.group(1).lower())
        if prefix_platform != platform:
            raise ValueError("creator prefix and platform do not match")
        value = value[prefix.end():]
    if value.startswith(("https://", "http://")):
        url = urlparse(value)
        allowed = {"douyin": {"douyin.com", "www.douyin.com", "v.douyin.com"},
                   "weibo": {"weibo.com", "www.weibo.com", "m.weibo.cn"},
                   "bilibili": {"space.bilibili.com", "www.bilibili.com", "bilibili.com"},
                   "x": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
                   "instagram": {"instagram.com", "www.instagram.com"},
                   "xhs": {"xiaohongshu.com", "www.xiaohongshu.com", "xhslink.com"}}
        if url.hostname not in allowed.get(platform, set()) or url.username or url.port:
            raise ValueError("use the full creator profile URL on the selected platform")
        if platform == "douyin":
            pattern = r"/user/([A-Za-z0-9_.-]+)/?"
        elif platform == "bilibili":
            pattern = r"/(?:u/)?(\d+)/?"
        elif platform == "x":
            pattern = r"/([A-Za-z0-9_.-]+)/?"
        elif platform == "instagram":
            pattern = r"/([A-Za-z0-9_.-]+)/?"
        elif platform == "xhs":
            pattern = r"/user/profile/([A-Za-z0-9_-]+)/?"
        else:
            pattern = r"/(?:u/|profile/)?(\d+)/?"
        match = re.fullmatch(pattern, unquote(url.path))
        if not match:
            raise ValueError("this is not a supported creator profile URL; provide the account ID or full profile URL")
        value = match.group(1)
    pattern = r"[A-Za-z0-9_.-]+" if platform in ("douyin", "x", "instagram", "xhs") else r"\d+"
    if not re.fullmatch(pattern, value) or len(value) > 200:
        raise ValueError("invalid creator ID; Douyin accepts a handle/UID/sec_uid, Weibo and Bilibili require a numeric UID")
    return value


def unpack_cursor(cursor: str | None) -> tuple[str, int]:
    if not cursor:
        return "0", 0
    try:
        data = json.loads(cursor)
        native = str(data["native"])
        offset = int(data["offset"])
        if not native.isdigit() or offset < 0 or offset > 100:
            raise ValueError()
        return native, offset
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid creator cursor; use next_cursor returned by the tool") from None


def pack_cursor(native: str | int, offset: int = 0) -> str:
    return json.dumps({"native": str(native), "offset": offset}, separators=(",", ":"))


def timestamp(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        if str(value).replace(".", "", 1).isdigit():
            result = float(value)
            return result / 1000 if result > 1e12 else result
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            date = parsedate_to_datetime(str(value))
        return date.replace(tzinfo=timezone.utc).timestamp() if date.tzinfo is None else date.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def native_author_id(platform: str, record: dict[str, Any]) -> str:
    data = record.get("mblog", record) if platform == "weibo" else record
    author = data.get("user" if platform == "weibo" else "author") or {}
    if not isinstance(author, dict):
        return ""
    return str((author.get("idstr") or author.get("id")) if platform == "weibo" else author.get("sec_uid") or "")


def creator_items(platform: str, record: dict[str, Any], identity: CreatorIdentity, covers: bool, media_type: str = "images") -> list[dict[str, Any]]:
    if native_author_id(platform, record) != identity.canonical_id:
        return []
    # Never recursively extract avatars, reposted media or link preview pictures.
    data = record.get("mblog", record) if platform == "weibo" else record
    if platform == "weibo" and not data.get("pics"):
        return []
    items = normalize_native_record(platform, record, identity.source, include_video_covers=covers, media_type=media_type)
    for item in items:
        item["id"] = f"{item['post_id']}:{item['media_index']}"
        item["creator_id"] = identity.canonical_id
        item["creator_name"] = identity.name
        if not item.get("permalink") and platform == "douyin":
            item["permalink"] = f"https://www.douyin.com/note/{item['post_id']}"
    return items


def douyin_identity(client: Any, requested: str, nickname: bool = False) -> CreatorIdentity:
    if nickname:
        target = requested.strip()
        if not target or len(target) > 200 or target.startswith(("http://", "https://")):
            raise ValueError("invalid Douyin nickname")
    else:
        target = creator_target("douyin", requested)
    matched_by = "sec_uid"
    sec_uid = target
    if not target.startswith("MS4w"):
        payload = client.search_users(target, count=20)
        rows = list(payload.get("user_list") or [])
        for row in payload.get("data") or []:
            if isinstance(row, dict):
                rows.extend(row.get("user_list") or [])
        matches = {}
        for row in rows:
            user = row.get("user_info") or row
            keys = ("nickname", "nick_name", "display_name") if nickname else ("unique_id", "short_id", "uid")
            if any(str(user.get(key) or "") == target for key in keys) and user.get("sec_uid"):
                matches[str(user["sec_uid"])] = user
        if len(matches) != 1:
            target_kind = "nickname" if nickname else "account ID"
            raise ValueError(f"creator_identity_unresolved: exact {target_kind} matched {len(matches)} users; provide the full Douyin profile URL")
        sec_uid = next(iter(matches))
        matched_by = "exact_account_name" if nickname else "exact_account_id"
    profile = client.get_user_profile(sec_uid)
    if str(profile.get("sec_uid") or "") != sec_uid:
        raise ValueError("creator_identity_mismatch: profile did not confirm the requested sec_uid")
    if matched_by == "exact_account_id" and not any(str(profile.get(k) or "") == target for k in ("unique_id", "short_id", "uid")):
        raise ValueError("creator_identity_mismatch: profile account ID differs from the requested ID")
    if matched_by == "exact_account_name" and not any(str(profile.get(k) or "") == target for k in ("nickname", "nick_name", "display_name")):
        raise ValueError("creator_identity_mismatch: profile nickname differs from the requested name")
    return CreatorIdentity(platform=Platform.DOUYIN, requested_id=requested, canonical_id=sec_uid,
                           name=str(profile.get("nickname") or ""), profile_url=f"https://www.douyin.com/user/{sec_uid}",
                           source="dy-cli", matched_by=matched_by)


class CreatorCollector:
    """Bound native pagination, retaining a partial-page offset for resume."""

    def __init__(self, identity: CreatorIdentity, request: Any) -> None:
        self.identity = identity
        self.request = request
        self.native, self.offset = unpack_cursor(request.cursor)
        self.next_cursor = request.cursor or pack_cursor("0")
        self.items: list[dict[str, Any]] = []
        self.post_ids: list[str] = []
        self.seen: set[str] = set()
        self.posts_fetched = 0
        self.rejected_posts = 0
        self.pages_fetched = 0
        self.warnings: list[str] = []

    def consume(self, rows: list[dict], next_native: Any, has_more: bool) -> bool:
        self.pages_fetched += 1
        if self.offset > len(rows):
            raise ValueError("creator page changed while resuming; restart with resume=false")
        for index in range(self.offset, len(rows)):
            if self.posts_fetched >= self.request.max_posts:
                self.next_cursor = pack_cursor(self.native, index)
                return False
            row = rows[index]
            data = row.get("mblog", row) if self.identity.platform == Platform.WEIBO else row
            post_id = str(data.get("aweme_id") or data.get("idstr") or data.get("id") or "")
            self.posts_fetched += 1
            if not post_id or native_author_id(self.identity.platform.value, row) != self.identity.canonical_id:
                self.rejected_posts += 1
                continue
            if post_id in self.seen:
                continue
            self.seen.add(post_id)
            self.post_ids.append(post_id)
            self.items.extend(creator_items(self.identity.platform.value, row, self.identity, self.request.include_video_covers, self.request.media_type))
        if not has_more:
            self.next_cursor = None
            return False
        if next_native in (None, "", "0", 0) or str(next_native) == self.native:
            raise ValueError("creator_cursor_stalled: platform did not return an advancing cursor")
        self.native, self.offset = str(next_native), 0
        self.next_cursor = pack_cursor(self.native)
        return self.posts_fetched < self.request.max_posts and self.pages_fetched < 10

    def result(self) -> dict[str, Any]:
        return {"identity": self.identity.model_dump(mode="json"), "items": self.items,
                "post_ids": self.post_ids, "posts_fetched": self.posts_fetched,
                "rejected_posts": self.rejected_posts, "pages_fetched": self.pages_fetched,
                "next_cursor": self.next_cursor, "warnings": self.warnings}
