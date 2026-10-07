from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

from .models import ImageCandidate, Platform


class StorageAgent:
    """Coordinate durable media paths and account-level download indexes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()

    @staticmethod
    def safe_name(value: object, fallback: str = "account") -> str:
        text = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", str(value or ""))
        text = re.sub(r"\s+", " ", text).strip(" ._")
        if text.upper().split(".", 1)[0] in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
            text = f"_{text}"
        return (text[:120] or fallback).strip(" ._") or fallback

    @classmethod
    def account_name(
        cls,
        platform: Platform | str,
        *,
        identity: dict | None = None,
        candidate: ImageCandidate | dict | None = None,
        query: str | None = None,
    ) -> str:
        platform_name = platform.value if isinstance(platform, Platform) else str(platform)
        identity = identity if isinstance(identity, dict) else {}
        candidate_data = candidate.model_dump(mode="json") if isinstance(candidate, ImageCandidate) else (candidate or {})
        values = (
            identity.get("name"),
            identity.get("canonical_id"),
            identity.get("requested_id"),
            candidate_data.get("creator_name"),
            candidate_data.get("creator_id"),
        )
        for value in values:
            if value and str(value).strip():
                return cls.safe_name(value, platform_name)
        if platform_name == Platform.OTHER.value:
            url = str(candidate_data.get("permalink") or query or "")
            host = urlparse(url).hostname or "web"
            return cls.safe_name(host.lower().removeprefix("www."), "web")
        raw_query = str(query or "").strip().lstrip("@")
        return cls.safe_name(raw_query or platform_name, platform_name)

    def account_dir(
        self,
        platform: Platform | str,
        *,
        identity: dict | None = None,
        candidate: ImageCandidate | dict | None = None,
        query: str | None = None,
        override: str | Path | None = None,
    ) -> Path:
        root = Path(override).expanduser() if override else self.root
        platform_name = platform.value if isinstance(platform, Platform) else str(platform)
        account = self.account_name(platform, identity=identity, candidate=candidate, query=query)
        path = root / self.safe_name(platform_name, "platform") / account
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _key(record: dict) -> str:
        return ":".join(
            str(record.get(name) or "")
            for name in ("post_id", "media_type", "media_index", "candidate_id")
        )

    @staticmethod
    def _escape(value: object) -> str:
        text = str(value or "").replace("\r", " ").replace("\n", " ")
        return text.replace("|", "\\|")

    @staticmethod
    def _date_label(value: object, path: Path) -> str:
        """Format publication time as the compact date used in the index."""
        text = str(value or "").strip()
        if text:
            try:
                numeric = float(text)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                return datetime.fromtimestamp(numeric, timezone.utc).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError, OverflowError):
                pass
            normalized = text.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(text)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
                except (TypeError, ValueError, OverflowError):
                    pass
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
                    except ValueError:
                        continue

        # Source CLIs may omit publication time. Download filenames carry a
        # stable YYYYMMDD prefix, which gives the reader a useful fallback.
        match = re.match(r"^(\d{8})_", path.name)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                pass
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
        except OSError:
            return "日期未知"

    @staticmethod
    def _media_heading(records: list[dict]) -> tuple[str, str]:
        kinds = {str(record.get("media_type") or "image") for record in records}
        if kinds == {"video"}:
            return "已下载视频清单", "视频"
        if kinds == {"image"}:
            return "已下载图片清单", "图片"
        return "已下载媒体清单", "图片和视频"

    @staticmethod
    def _identity_label(platform: str) -> str:
        return {
            "bilibili": "BV 号",
            "douyin": "作品 ID",
            "weibo": "作品 ID",
            "x": "帖子 ID",
            "instagram": "作品 ID",
            "xhs": "作品 ID",
            "other": "页面作品 ID",
        }.get(platform, "作品 ID")

    @classmethod
    def _read_manifest(cls, account_dir: Path) -> dict[str, dict]:
        manifest = account_dir / "manifest.jsonl"
        records: dict[str, dict] = {}
        try:
            for line in manifest.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(value, dict) or not value.get("path"):
                    continue
                path = Path(str(value["path"]))
                if not path.is_file():
                    continue
                records[cls._key(value)] = value
        except (OSError, UnicodeDecodeError):
            pass
        return records

    def write_index(
        self,
        account_dir: str | Path,
        platform: Platform | str,
        *,
        identity: dict | None = None,
        records: list[dict] | None = None,
    ) -> Path:
        """Write a human-readable account index beside the media folders."""
        directory = Path(account_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        merged = self._read_manifest(directory)
        for record in records or []:
            if isinstance(record, dict) and record.get("path"):
                merged[self._key(record)] = record
        platform_name = platform.value if isinstance(platform, Platform) else str(platform)
        account = directory.name
        rows = []
        ordered_records = sorted(
            merged.values(),
            key=lambda value: self._date_label(
                value.get("published_at"), Path(str(value.get("path") or ""))
            ) + " " + Path(str(value.get("path") or "")).name,
        )
        for record in ordered_records:
            path = Path(str(record.get("path") or ""))
            filename = path.name or str(record.get("candidate_id") or "未命名文件")
            date = self._date_label(record.get("published_at"), path)
            title = self._escape(record.get("title") or "")
            # Keep one physical file on every line. A missing title is common
            # for native Douyin records, so the filename remains the fallback
            # readers can use to identify the work.
            parts = [date]
            if title:
                parts.append(title)
            parts.append(self._escape(filename))
            rows.append("- " + " ".join(parts))

        heading, media_label = self._media_heading(list(merged.values()))
        identity_label = self._identity_label(platform_name)
        body = "\n".join(
            [
                f"# {heading}",
                "",
                f"只录入确认下载完成的{media_label}（一行一个文件名）；按文件名中的{identity_label}核验。",
                "本表是「已下载」的唯一权威依据：本地文件可能已备份到别处，缺失不代表未下载。",
                "",
                *rows,
                "",
            ]
        )
        target = directory / f"{self.safe_name(account, 'account')}.md"
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(target)
        return target
