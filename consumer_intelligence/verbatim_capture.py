"""Real evidence for "Supporting Verbatims" sections: an official embed where
the platform offers one, else a server-captured screenshot of the live post,
else the plain text quote (already the fallback every caller has today).

Order of preference, per quote:
  1. `detect_embed(url)` + `_verify_embed(embed)` — X/Twitter and YouTube URLs
     get a real, live, ToS-safe embed rendered client-side (X's own
     widgets.js, YouTube's own iframe embed), but only once a cheap oEmbed
     call confirms the post/video actually exists. Committing to the embed
     path from URL shape alone (a tweet-shaped URL) is not enough: X's
     widgets.js and YouTube's iframe both fail SILENTLY (blank/unstyled) on
     a dead or synthetic ID, with no client-side fallback — this bit a
     synthetic test dataset where every "tweet" URL has a made-up status id.
  2. `capture_screenshots(urls)` — for everything else (or an embed that
     failed verification), a real Playwright screenshot of the live page,
     cached to S3 under `verbatim_screenshots/` (same bucket/pattern as
     brand_media's other cached assets). Best-effort: a blocked, login-walled,
     or dead URL is simply omitted, and the caller's existing plain-text
     quote card is what the user sees.

Never raises: a total capture failure (no Playwright, no network, everything
blocked) leaves every quote exactly as it was — plain text, still real,
still cited.
"""

import asyncio
import hashlib
import logging
import re

from file_helpers.s3_file import s3_file

logger = logging.getLogger(__name__)

SCREENSHOT_PREFIX = "verbatim_screenshots/"
_CAPTURE_TIMEOUT_MS = 12000
_OEMBED_TIMEOUT = 6.0
_CONCURRENCY = 3
_NOT_FOUND_TITLE_HINTS = (
    "page not found",
    "page isn't available",
    "page is not available",
    "content isn't available",
    "content is not available",
    "video unavailable",
    "post unavailable",
    "this page doesn't exist",
    "sorry, this page",
    "404",
)

_TWITTER_RX = re.compile(r"(?:twitter|x)\.com/[^/]+/status(?:es)?/(\d+)", re.I)
_YOUTUBE_RX = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{6,})", re.I)

_TWITTER_OEMBED = "https://publish.twitter.com/oembed"
_YOUTUBE_OEMBED = "https://www.youtube.com/oembed"


def detect_embed(url: str) -> dict | None:
    """A real, official embed descriptor for `url`, or None. Pure — no network call.

    Shape-only: callers must still run this through `_verify_embed` before
    trusting it, since a tweet-shaped or video-shaped URL may not resolve to
    anything real (deleted post, or synthetic test data).
    """
    if not url:
        return None
    m = _TWITTER_RX.search(url)
    if m:
        return {"type": "twitter", "url": url, "id": m.group(1)}
    m = _YOUTUBE_RX.search(url)
    if m:
        return {"type": "youtube", "video_id": m.group(1)}
    return None


async def _verify_embed(embed: dict, client) -> bool:
    """True if the platform's own oEmbed endpoint confirms this post/video is
    real and public. Never raises — any network/parse failure counts as
    unverified so the caller falls back to a screenshot or plain text rather
    than a client-side embed that might render blank."""
    try:
        if embed["type"] == "twitter":
            resp = await client.get(_TWITTER_OEMBED, params={"url": embed["url"]}, timeout=_OEMBED_TIMEOUT)
        else:
            video_url = f"https://www.youtube.com/watch?v={embed['video_id']}"
            resp = await client.get(_YOUTUBE_OEMBED, params={"url": video_url, "format": "json"}, timeout=_OEMBED_TIMEOUT)
        return resp.status_code == 200
    except Exception as exc:
        logger.info(f"Embed verification failed for {embed}: {exc}")
        return False


def _key_for(url: str) -> str:
    return f"{SCREENSHOT_PREFIX}{hashlib.sha1(url.encode('utf-8')).hexdigest()}.png"


async def capture_screenshots(urls: list[str]) -> dict[str, str]:
    """`{url: s3_key}` for URLs a headless browser could actually load and
    screenshot. Best-effort — a blocked/dead/slow URL is simply left out, and
    Playwright not being installed disables this path entirely rather than
    failing the dashboard build."""
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return {}
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.info("Playwright not installed; skipping verbatim screenshot capture.")
        return {}

    out: dict[str, str] = {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(browser, url: str) -> None:
        async with sem:
            page = None
            try:
                page = await browser.new_page(viewport={"width": 640, "height": 720})
                response = await page.goto(url, wait_until="networkidle", timeout=_CAPTURE_TIMEOUT_MS)
                if response is not None and response.status >= 400:
                    logger.info(f"Verbatim screenshot skipped for {url}: HTTP {response.status}")
                    return
                title = ((await page.title()) or "").strip().lower()
                if any(hint in title for hint in _NOT_FOUND_TITLE_HINTS):
                    logger.info(f"Verbatim screenshot skipped for {url}: page title indicates missing content ({title!r})")
                    return
                png = await page.screenshot(type="png")
                key = _key_for(url)
                s3_file.upload_file(key, png)
                out[url] = key
            except Exception as exc:
                logger.info(f"Verbatim screenshot skipped for {url}: {exc}")
            finally:
                if page is not None:
                    await page.close()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                await asyncio.gather(*(one(browser, u) for u in urls))
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning(f"Verbatim screenshot capture unavailable: {exc}")
        return {}
    return out


async def resolve_evidence(quotes: list[dict]) -> list[dict]:
    """Enrich each quote in place with `embed` or `screenshot_key` when real
    evidence could be resolved for its `url`. Never raises; on total failure
    the quotes are returned exactly as given."""
    if not quotes:
        return quotes
    need_screenshot: list[str] = []
    candidates = [(q, detect_embed(q.get("url") or "")) for q in quotes]

    verified: dict[int, bool] = {}
    to_verify = [(q, embed) for q, embed in candidates if embed]
    if to_verify:
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                sem = asyncio.Semaphore(_CONCURRENCY)

                async def check(q, embed):
                    async with sem:
                        verified[id(q)] = await _verify_embed(embed, client)

                await asyncio.gather(*(check(q, embed) for q, embed in to_verify))
        except Exception as exc:  # belt and braces — verification must never block the build
            logger.warning(f"resolve_evidence embed verification pass failed: {exc}")

    for q, embed in candidates:
        url = q.get("url") or ""
        if embed and verified.get(id(q)):
            q["embed"] = embed
        elif url:
            need_screenshot.append(url)

    if need_screenshot:
        try:
            captured = await capture_screenshots(need_screenshot)
        except Exception as exc:  # belt and braces — this function must never raise
            logger.warning(f"resolve_evidence screenshot pass failed: {exc}")
            captured = {}
        for q in quotes:
            key = captured.get(q.get("url") or "")
            if key:
                q["screenshot_key"] = key
    return quotes
