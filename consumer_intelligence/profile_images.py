"""Profile pictures for the people behind social posts, taken from the post links
and author names already in the uploaded data.

Per post, `identify` works out (platform, handle):
  * Twitter/X   - the handle in `twitter.com/<handle>/status/<id>`
  * Tumblr      - the blog subdomain
  * YouTube     - `youtube.com/@handle`, else the author
  * Instagram / Facebook / Reddit - the author field (those URLs carry no handle)

Sources, cheapest and most honest first:
  * Twitter and YouTube through unavatar.io (free tier)
  * Tumblr through its own public avatar API
  * Instagram, Facebook and Reddit only when `UNAVATAR_API_KEY` (a paid plan) is set;
    without it they are skipped and the FE draws initials plus the platform icon.

A fetched image is stored once in S3 under `profile_images/` and served by the
backend (`/consumer-intelligence/profile-image`), so viewers never hit the avatar
service's rate limit. A handle with no real picture (deleted account, the
service's placeholder SVG, Tumblr's default tile) yields nothing rather than a
stand-in. Never raises.
"""

import asyncio
import hashlib
import logging
import os
import re
import time

from file_helpers.s3_file import s3_file

logger = logging.getLogger(__name__)

PROFILE_PREFIX = "profile_images/"
_UNAVATAR = "https://unavatar.io"
_TUMBLR_AVATAR = "https://api.tumblr.com/v2/blog/{blog}.tumblr.com/avatar/128"
_FREE_PROVIDERS = {"twitter", "youtube"}
_TIMEOUT = 20.0
_CONCURRENCY = 3  # unavatar's free tier is rate limited
_MIN_BYTES = 800
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)  # Tumblr drops the default python client identity

_TWITTER_RX = re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})/status", re.I)
_TUMBLR_RX = re.compile(r"https?://([a-z0-9-]+)\.tumblr\.com", re.I)
_YOUTUBE_HANDLE_RX = re.compile(r"youtube\.com/@([\w.-]+)", re.I)
_HANDLE_RX = re.compile(r"^[A-Za-z0-9._-]{2,40}$")

# Lists in a storyboard whose rows name a person, and the field holding the name.
_PEOPLE_LISTS = {
    "quotes": "author",
    "quotes_positive": "author",
    "quotes_negative": "author",
    "top_influencers": "author",
    "authors_by_reach": "name",
    "authors_by_volume": "name",
}

# (provider, handle) -> S3 key, or None when no real picture exists. Per process.
_RESOLVED: dict[tuple[str, str], str | None] = {}


def identify(article: dict) -> tuple[str, str] | None:
    """(provider, handle) for the poster of a social post, or None."""
    url = str(article.get("url") or "")
    author = str(article.get("author") or "").strip().lstrip("@")
    valid_author = author if _HANDLE_RX.match(author) else ""
    m = _TWITTER_RX.search(url)
    if m:
        return "twitter", m.group(1)
    m = _TUMBLR_RX.search(url)
    if m and m.group(1) not in ("www", "api", "assets"):
        return "tumblr", m.group(1)
    host = url.lower()
    if "youtube.com" in host or "youtu.be" in host:
        m = _YOUTUBE_HANDLE_RX.search(url)
        handle = m.group(1) if m else valid_author
        return ("youtube", handle) if handle else None
    for needle, provider in (("instagram.com", "instagram"), ("reddit.com", "reddit"), ("facebook.com", "facebook")):
        if needle in host:
            return (provider, valid_author) if valid_author else None
    return None


def _key(provider: str, handle: str) -> str:
    return f"{PROFILE_PREFIX}{provider}/{hashlib.sha1(handle.lower().encode()).hexdigest()}"  # noqa: S324 - key only


class _RateLimited(Exception):
    """The avatar service refused for now; the handle is NOT known to lack a picture."""


# unavatar's free tier is rate limited; once it says 429 nothing more is asked of it
# until its reset time, and the refused handles are retried on the next build.
_unavatar_blocked_until = 0.0


def _get(url: str, headers: dict[str, str]):
    import requests  # httpx is dropped by Tumblr's API; requests is not

    return requests.get(url, headers=headers, timeout=_TIMEOUT, allow_redirects=True)


async def _download(provider: str, handle: str) -> bytes | None:
    """The picture's bytes, or None when the source has no real one.
    Raises _RateLimited when the service asked us to slow down."""
    global _unavatar_blocked_until
    headers: dict[str, str] = {"User-Agent": _UA}
    via_unavatar = provider != "tumblr"
    if provider == "tumblr":
        url = _TUMBLR_AVATAR.format(blog=handle)
    else:
        url = f"{_UNAVATAR}/{provider}/{handle}"
        if provider not in _FREE_PROVIDERS:
            api_key = os.getenv("UNAVATAR_API_KEY")
            if not api_key:
                return None
            headers["x-api-key"] = api_key
    if via_unavatar and time.time() < _unavatar_blocked_until:
        raise _RateLimited
    try:
        resp = await asyncio.to_thread(_get, url, headers)
    except Exception as exc:
        logger.info("Profile image skipped for %s/%s: %s", provider, handle, exc)
        return None
    if resp.status_code == 429:
        reset_ms = float(resp.headers.get("x-rate-limit-reset") or 0)
        _unavatar_blocked_until = reset_ms / 1000 if reset_ms else time.time() + 300
        logger.info("unavatar rate limited; pausing avatar lookups until %s", _unavatar_blocked_until)
        raise _RateLimited
    kind = resp.headers.get("content-type", "")
    is_real = (
        resp.status_code == 200
        and kind.startswith("image/")
        and "svg" not in kind  # unavatar's stand-in for an unknown handle
        and "default_avatar" not in str(resp.url)  # Tumblr's stand-in
        and len(resp.content) >= _MIN_BYTES
    )
    return resp.content if is_real else None


async def _resolve(provider: str, handle: str) -> str | None:
    ident = (provider, handle.lower())
    if ident in _RESOLVED:
        return _RESOLVED[ident]
    key = _key(provider, handle)
    try:
        if await asyncio.to_thread(s3_file.download_file, key):
            _RESOLVED[ident] = key
            return key
    except Exception:  # not cached yet
        pass
    try:
        data = await _download(provider, handle)
    except _RateLimited:
        return None  # not recorded: retried on the next build
    if data:
        try:
            await asyncio.to_thread(s3_file.upload_file, key, data)
        except Exception as exc:
            logger.warning("Could not cache profile image %s: %s", key, exc)
            data = None
    _RESOLVED[ident] = key if data else None
    return _RESOLVED[ident]


def _index(articles: list[dict]) -> dict[str, tuple[str, str]]:
    """lower-cased author name or handle -> (provider, handle)."""
    out: dict[str, tuple[str, str]] = {}
    for article in articles:
        who = identify(article)
        if not who:
            continue
        for name in (str(article.get("author") or "").strip().lstrip("@"), who[1]):
            if name:
                out.setdefault(name.lower(), who)
    return out


def _people_rows(node) -> list[tuple[dict, str]]:
    """Every (row, name field) under a storyboard whose list is in `_PEOPLE_LISTS`."""
    found: list[tuple[dict, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            field = _PEOPLE_LISTS.get(key)
            if field and isinstance(value, list):
                found += [(row, field) for row in value if isinstance(row, dict)]
            else:
                found += _people_rows(value)
    elif isinstance(node, list):
        for value in node:
            found += _people_rows(value)
    return found


async def attach(storyboard: dict, articles: list[dict]) -> None:
    """Set `avatar_key` on every person row of `storyboard` whose poster's
    picture could be fetched. Never raises."""
    try:
        index = _index(articles)
        rows = [(row, index.get(str(row.get(field) or "").strip().lstrip("@").lower())) for row, field in _people_rows(storyboard)]
        rows = [(row, who) for row, who in rows if who]
        if not rows:
            return
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def one(row: dict, who: tuple[str, str]) -> None:
            async with sem:
                key = await _resolve(*who)
            if key:
                row["avatar_key"] = key

        await asyncio.gather(*(one(row, who) for row, who in rows))
    except Exception as exc:  # belt and braces — avatars are decoration
        logger.warning("profile image pass failed: %s", exc)
