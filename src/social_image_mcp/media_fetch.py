"""Browser-faithful media requests: per-platform headers and redirect following.

Image hosts chain redirects (page -> image host -> CDN) and each hop has its
own hotlink rule. A browser sends the page Referer on the first hop and then
adjusts it on later hops according to the ``Referrer-Policy`` header carried
by redirect responses. ``httpx``'s automatic redirect handling re-sends the
caller's headers on every hop instead, so a CDN that asked for no Referer
answers 403 and the image is lost.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx

from .models import ImageCandidate

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 20
_REFERRER_POLICIES = frozenset({
    "no-referrer", "no-referrer-when-downgrade", "origin", "origin-when-cross-origin",
    "strict-origin", "strict-origin-when-cross-origin", "unsafe-url",
})


def parse_referrer_policy(value: str | None) -> str | None:
    """The last recognized policy name in a Referrer-Policy value, or None."""
    policy = None
    for token in (value or "").split(","):
        name = token.strip().lower()
        if name in _REFERRER_POLICIES:
            policy = name
    return policy


def _split(url: str) -> tuple[str, str, str, str] | None:
    """(scheme, host[:port], path, query) of an http(s) URL, without credentials or fragment."""
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if scheme not in {"http", "https"} or not host:
        return None
    if port is not None and port != (443 if scheme == "https" else 80):
        host = f"{host}:{port}"
    return scheme, host, parts.path or "/", parts.query


def referrer_for(policy: str, referrer: str, target: str) -> str:
    """The Referer a browser sends to ``target`` under ``policy``.

    An unknown policy leaves the caller's header alone.
    """
    if policy not in _REFERRER_POLICIES:
        return referrer
    source = _split(referrer)
    destination = _split(target)
    if source is None or destination is None:
        return ""
    scheme, host, path, query = source
    full = f"{scheme}://{host}{path}" + (f"?{query}" if query else "")
    origin = f"{scheme}://{host}"
    if policy == "no-referrer":
        return ""
    if policy == "unsafe-url":
        return full
    downgrade = scheme == "https" and destination[0] == "http"
    if policy == "no-referrer-when-downgrade":
        return "" if downgrade else full
    if policy == "origin":
        return origin
    if policy == "strict-origin":
        return "" if downgrade else origin
    same_origin = scheme == destination[0] and host == destination[1]
    if policy == "origin-when-cross-origin":
        return full if same_origin else origin
    return "" if downgrade else (full if same_origin else origin)


def _same_origin(left: str, right: str) -> bool:
    first, second = _split(left), _split(right)
    return first is not None and second is not None and first[:2] == second[:2]


def _is_https_upgrade(left: str, right: str) -> bool:
    first, second = _split(left), _split(right)
    return (
        first is not None and second is not None
        and first[0] == "http" and second[0] == "https" and first[1] == second[1]
    )


def _next_hop_headers(
    headers: dict[str, str], response: httpx.Response, current: str, following: str, policy: str | None,
) -> tuple[dict[str, str], str | None]:
    """Headers for the request after a redirect, and the policy in force."""
    policy = parse_referrer_policy(response.headers.get("referrer-policy")) or policy
    outgoing = httpx.Headers(headers)
    if policy is not None and "referer" in outgoing:
        adjusted = referrer_for(policy, outgoing["referer"], following)
        if adjusted:
            outgoing["Referer"] = adjusted
        else:
            del outgoing["Referer"]
    # httpx drops credentials when a redirect leaves the origin, except for a
    # plain http -> https upgrade on the same host; mirror that.
    if not _same_origin(current, following) and not _is_https_upgrade(current, following):
        outgoing.pop("Authorization", None)
    return dict(outgoing), policy


@asynccontextmanager
async def stream_media(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: Any = None,
    max_redirects: int = MAX_REDIRECTS,
) -> AsyncIterator[httpx.Response]:
    """Stream one GET the way a browser would, following redirects hop by hop.

    The yielded response is the final hop, still open for reading. Its
    ``extensions["redirect_chain"]`` lists the URLs redirected through, so a
    caller can tell a refusal from the requested URL itself (empty chain) from
    one that came from a redirect target.
    """
    hop_headers = dict(headers or {})
    policy: str | None = None
    chain: list[str] = []
    current = url
    for _ in range(max_redirects + 1):
        request = client.build_request("GET", current, headers=hop_headers, timeout=timeout)
        response = await client.send(request, stream=True, follow_redirects=False)
        location = response.headers.get("location") if response.status_code in REDIRECT_STATUSES else None
        if not location:
            response.extensions["redirect_chain"] = list(chain)
            try:
                yield response
            finally:
                await response.aclose()
            return
        try:
            await response.aread()
        finally:
            await response.aclose()
        chain.append(current)
        following = str(httpx.URL(current).join(location))
        hop_headers, policy = _next_hop_headers(hop_headers, response, current, following, policy)
        current = following
    raise httpx.TooManyRedirects(f"more than {max_redirects} redirects for {url}")


async def get_media(
    client: httpx.AsyncClient, url: str, *, headers: dict[str, str] | None = None, timeout: Any = None,
) -> httpx.Response:
    """``client.get`` with browser-faithful redirects; the body is fully read."""
    async with stream_media(client, url, headers=headers, timeout=timeout) as response:
        await response.aread()
        return response


def media_request_headers(item: ImageCandidate) -> dict[str, str]:
    """Headers for the first request of a media item."""
    headers = {"User-Agent": "Mozilla/5.0 (social-image-mcp)"}
    platform = item.platform.value
    if platform == "x":
        headers["Referer"] = "https://x.com/"
    elif platform == "weibo":
        headers["Referer"] = item.permalink or "https://m.weibo.cn/"
    elif platform == "douyin":
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
        headers["Referer"] = item.permalink or "https://www.douyin.com/"
        headers["Origin"] = "https://www.douyin.com"
    elif platform == "bilibili":
        headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
        headers["Referer"] = item.permalink or "https://www.bilibili.com/"
        headers["Origin"] = "https://www.bilibili.com"
    elif platform == "other" and item.permalink:
        # A page may declare a referrer policy for its images (commonly
        # referrerpolicy="no-referrer", to get around hotlink protection).
        # Browsers send exactly what that policy says, so do the same.
        payload = item.source_payload if isinstance(item.source_payload, dict) else {}
        policy = parse_referrer_policy(payload.get("referrer_policy"))
        referer = referrer_for(policy, item.permalink, item.image_url) if policy else item.permalink
        if referer:
            headers["Referer"] = referer
    return headers
