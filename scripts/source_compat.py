"""Small, reproducible compatibility repairs for the locked upstream sources.

Only known snippets are replaced; unrelated local modifications are preserved.
The source projects and their original licenses remain in third_party.
"""
from __future__ import annotations

from pathlib import Path


CREATOR_METHODS = '''    def _short_url_redirects(self, url: str) -> tuple[list[str], str, str]:
        urls: list[str] = []
        headers = get_headers()
        try:
            with httpx.Client(follow_redirects=False, timeout=self.timeout) as probe:
                response = probe.get(url, headers=headers)
                location = str(response.headers.get("location") or "")
                if location:
                    urls.append(location)
        except Exception:
            pass
        try:
            response = self.client.get(url, headers=headers)
            urls.append(str(response.url))
            return urls, response.text[:100000], str(response.url)
        except Exception:
            return urls, "", urls[-1] if urls else ""

    def resolve_creator_share_url(self, url: str) -> str:
        direct = CREATOR_SHARE_URL_PATTERN.search(url)
        if direct:
            return f"https://www.douyin.com/user/{direct.group(1)}"
        if not SHORT_URL_PATTERN.match(url):
            raise DouyinAPIError(f"Invalid Douyin profile URL: {url}")
        urls, body, final_url = self._short_url_redirects(url)
        for candidate in [*urls, final_url]:
            match = CREATOR_SHARE_URL_PATTERN.search(candidate or "")
            if match:
                return f"https://www.douyin.com/user/{match.group(1)}"
        for pattern in (r"(?:share/user|douyin\\.com/user)[/\\\\]([A-Za-z0-9_.-]+)", r'"sec_uid"\\s*:\\s*"(MS4w[A-Za-z0-9_-]+)"'):
            match = re.search(pattern, body or "", re.I)
            if match:
                return f"https://www.douyin.com/user/{match.group(1)}"
        raise DouyinAPIError(f"Cannot resolve profile share; use a full https://www.douyin.com/user/... URL: {url}")

'''


def _replace(path: Path, old: str, new: str, marker: str) -> None:
    content = path.read_text(encoding="utf-8")
    if marker in content:
        return
    if old not in content:
        raise RuntimeError(f"Compatibility repair does not match {path}; existing file was preserved")
    updated = content.replace(old, new, 1)
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8")


def apply_compatibility(name: str, target: Path) -> None:
    if name == "XHS-Downloader":
        path = target / "source/module/static.py"
        old = 'PROJECT = f"XHS-Downloader V{VERSION_MAJOR}.{VERSION_MINOR} {\n    \'Beta\' if VERSION_BETA else \'Stable\'\n}"'
        new = 'PROJECT = f"XHS-Downloader V{VERSION_MAJOR}.{VERSION_MINOR} {\'Beta\' if VERSION_BETA else \'Stable\'}"'
        _replace(path, old, new, new)
    elif name == "dy-cli":
        path = target / "src/dy_cli/engines/api_client.py"
        content = path.read_text(encoding="utf-8")
        if "def resolve_creator_share_url(" in content:
            return
        old = 'SHORT_URL_PATTERN = re.compile(r"https?://v\\.douyin\\.com/\\w+/?")'
        new = 'SHORT_URL_PATTERN = re.compile(r"https?://v\\.douyin\\.com/[A-Za-z0-9_-]+/?(?:[?#].*)?$", re.I)\nCREATOR_SHARE_URL_PATTERN = re.compile(r"https?://(?:www\\.)?(?:douyin\\.com|iesdouyin\\.com)/(?:share/user|user)/([^/?#]+)", re.I)'
        anchor = '    def resolve_share_url(self, url: str) -> str:\n'
        if old not in content or anchor not in content:
            raise RuntimeError(f"Profile-share repair does not match {path}; existing file was preserved")
        content = content.replace(old, new, 1).replace(anchor, CREATOR_METHODS + anchor, 1)
        compile(content, str(path), "exec")
        path.write_text(content, encoding="utf-8")
