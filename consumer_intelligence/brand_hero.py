"""The brand's own website as the primary hero banner source.

Every storyboard's cover banner used to be Pexels stock photography only — never
anything that actually shows the brand. This module tries the brand's own
homepage first: a YouTube embed or a direct video file if the page has one
(brand sites use these constantly for their own hero section), else the page's
Open Graph image, downloaded and checked to be a real photo. Callers (see
`brand_media.resolve_hero_media`) fall back to Pexels stock media only when the
brand's site has nothing usable.

"Nothing usable" includes an image that doesn't actually show the brand: a
vision-model call verifies it before it's trusted, since a homepage's og:image
is sometimes a generic seasonal banner with no product or logo in it at all. A
video scraped from the brand's own page markup skips this check — it is
definitionally the brand's own material, not a stock-photo guess.

Every result (site scrape and vision verdict) is cached per domain / image URL
for the life of the process, so a session with many lenses costs one homepage
fetch and at most one vision call per brand, not one per lens.

Never raises; a brand whose site can't be reached, or has nothing usable,
yields None and the caller moves on to Pexels.
"""

import logging
import re
from html import unescape
from urllib.parse import urljoin

from .narrative_client import get_vision_client
from .verbatim_capture import _PREVIEW_UA, parse_preview

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
_RENDER_TIMEOUT_MS = 15000
_MAX_HTML_BYTES = 400_000
_MAX_IMAGE_BYTES = 4_000_000
_MIN_IMAGE_BYTES = 3000  # a real hero photo, not a tracking pixel or tiny icon

_YOUTUBE_ID_RX = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/)([\w-]{6,})", re.I)
_VIDEO_FILE_RX = re.compile(r'<(?:video|source)[^>]+src=["\']([^"\']+\.(?:mp4|webm))["\']', re.I)

# A visually-classed hero <img> found in the rendered page — many storefront
# themes (Shopify especially) inject their real banner client-side and never
# set og:image at all, so scanning actual <img> tags after JS runs catches
# what a plain HTTP fetch and og:image-only parsing both miss.
_HERO_IMG_RX = re.compile(r'<img\b[^>]*?(?:class|alt)=["\'][^"\']*?(?:hero|banner|slide|feature)[^"\']*?["\'][^>]*?>', re.I)
_IMG_SRC_RX = re.compile(r'\bsrc=["\']([^"\']+)["\']')
_IMG_SRCSET_RX = re.compile(r'\bsrcset=["\']([^"\']+)["\']')

# A homepage's own footer/header social links — the brand's *stated* accounts,
# never a guessed handle — used as a second-tier source when the page itself
# has no usable hero.
_YT_LINK_RX = re.compile(r'href=["\']https?://(?:www\.)?youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w-]+)/?["\']', re.I)
_TWITTER_LINK_RX = re.compile(r'href=["\']https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{2,15})/?["\']', re.I)
_YT_CHANNEL_ID_RX = re.compile(r'"channelId":"(UC[\w-]{10,})"')
_YT_ENTRY_RX = re.compile(r"<entry>.*?<yt:videoId>([\w-]{6,})</yt:videoId>", re.S)
# Boilerplate paths that match the link pattern but aren't a real account (share widgets, nav links).
_NOT_A_HANDLE = {"intent", "share", "home", "i", "search", "hashtag", "login", "signup"}

# domain -> the site's own hero candidate (unverified), or None once tried.
_SITE_CACHE: dict[str, dict | None] = {}
# image url -> vision verdict, so the same og:image isn't re-checked every build.
_VERDICT_CACHE: dict[str, bool | None] = {}


async def _get(client, url: str, *, binary: bool = False):
    try:
        resp = await client.get(url, headers={"User-Agent": _PREVIEW_UA, "Accept-Language": "en"}, timeout=_TIMEOUT, follow_redirects=True)
        if resp.status_code >= 400:
            return None
        if binary:
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
            return resp.content, content_type
        return resp.text
    except Exception as exc:
        logger.debug("brand_hero fetch failed for %s: %s", url, exc)
        return None


def extract_social_links(html: str) -> dict[str, str]:
    """{"youtube": "@handle"|"channel/UC..."|"c/Name", "twitter": "handle"} for
    whichever accounts the page itself links to. Pure — no network call."""
    links: dict[str, str] = {}
    match = _YT_LINK_RX.search(html)
    if match:
        links["youtube"] = match.group(1)
    match = _TWITTER_LINK_RX.search(html)
    if match and match.group(1).lower() not in _NOT_A_HANDLE:
        links["twitter"] = match.group(1)
    return links


async def _resolve_youtube_channel_id(client, path: str) -> str | None:
    if path.lower().startswith("channel/"):
        return path.split("/", 1)[1]
    page = await _get(client, f"https://www.youtube.com/{path}")
    if not page:
        return None
    match = _YT_CHANNEL_ID_RX.search(page)
    return match.group(1) if match else None


async def _youtube_channel_hero(client, path: str, domain: str) -> dict | None:
    """The channel's most recent upload, via YouTube's public per-channel RSS
    feed — free, no API key, no quota."""
    channel_id = await _resolve_youtube_channel_id(client, path)
    if not channel_id:
        return None
    feed = await _get(client, f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    if not feed:
        return None
    match = _YT_ENTRY_RX.search(feed)
    if not match:
        return None
    return {"type": "youtube", "url": f"https://www.youtube.com/embed/{match.group(1)}", "source": "brand_youtube", "domain": domain}


async def _twitter_avatar_hero(client, handle: str, domain: str) -> dict | None:
    """The account's own profile picture via unavatar — not a wide banner, but
    real and unmistakably the brand's, so it skips the vision check like video
    does. Last resort before Pexels."""
    fetched = await _get(client, f"https://unavatar.io/twitter/{handle}", binary=True)
    if not fetched:
        return None
    data, content_type = fetched
    if not content_type.startswith("image/") or "svg" in content_type or len(data) < _MIN_IMAGE_BYTES:
        return None  # unavatar's stand-in for an unknown handle is a small SVG
    return {"type": "image", "url": f"https://unavatar.io/twitter/{handle}", "source": "brand_twitter", "domain": domain}


async def _render_html(url: str) -> str | None:
    """The page's fully rendered HTML, after its own JS has run — a plain HTTP
    fetch never sees client-injected content, and modern storefront themes
    (Shopify especially) inject their real hero banner and even their own
    og:image this way. Best-effort: no Playwright, a timeout, or a dead page
    all fall through to og:image/og:video parsing of whatever a plain fetch got."""
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page(user_agent=_PREVIEW_UA)
                response = await page.goto(url, wait_until="networkidle", timeout=_RENDER_TIMEOUT_MS)
                if response is not None and response.status >= 400:
                    return None
                return await page.content()
            finally:
                await browser.close()
    except Exception as exc:
        logger.debug("brand_hero render failed for %s: %s", url, exc)
        return None


def _extract_hero_image_url(html: str, base: str) -> str | None:
    """The first hero/banner/slide-classed <img>'s src, resolved to an
    absolute URL. Pure — no network call. None when the page has none."""
    for tag in _HERO_IMG_RX.findall(html):
        match = _IMG_SRC_RX.search(tag)
        src = match.group(1) if match else None
        if not src:
            match = _IMG_SRCSET_RX.search(tag)
            src = match.group(1).split(",")[0].strip().split(" ")[0] if match else None
        if src and not src.startswith("data:"):
            return urljoin(base, unescape(src))  # raw markup HTML-escapes "&" as "&amp;" inside attributes
    return None


async def _social_hero(client, html: str, domain: str) -> dict | None:
    """A video or picture from the brand's own linked social accounts, tried
    only once the homepage itself had nothing usable."""
    links = extract_social_links(html)
    if "youtube" in links:
        hero = await _youtube_channel_hero(client, links["youtube"], domain)
        if hero:
            return hero
    if "twitter" in links:
        hero = await _twitter_avatar_hero(client, links["twitter"], domain)
        if hero:
            return hero
    return None


async def _scrape_site(client, domain: str) -> dict | None:
    home = f"https://{domain}/"
    # Render first (catches client-injected banners and og tags); a plain
    # fetch is the fallback when Playwright itself can't reach the page.
    html = await _render_html(home) or await _get(client, home)
    if not html:
        return None
    html = html[:_MAX_HTML_BYTES]

    match = _YOUTUBE_ID_RX.search(html)
    if match:
        return {"type": "youtube", "url": f"https://www.youtube.com/embed/{match.group(1)}", "source": "brand_website", "domain": domain}

    match = _VIDEO_FILE_RX.search(html)
    if match:
        return {"type": "video", "url": urljoin(home, match.group(1)), "source": "brand_website", "domain": domain}

    preview = parse_preview(html, home)
    video_url = (preview or {}).get("video")
    if video_url:
        yt = _YOUTUBE_ID_RX.search(video_url)
        if yt:
            return {"type": "youtube", "url": f"https://www.youtube.com/embed/{yt.group(1)}", "source": "brand_website", "domain": domain}
        return {"type": "video", "url": urljoin(home, video_url), "source": "brand_website", "domain": domain}

    # og:image, then a heuristic scan of the rendered page's own hero/banner
    # <img> for sites (Shopify themes especially) that never set og:image at all.
    for image_url in filter(None, [(preview or {}).get("image"), _extract_hero_image_url(html, home)]):
        fetched = await _get(client, image_url, binary=True)
        if fetched:
            data, content_type = fetched
            if content_type.startswith("image/") and _MIN_IMAGE_BYTES <= len(data) <= _MAX_IMAGE_BYTES:
                return {"type": "image", "url": image_url, "source": "brand_website", "domain": domain, "_bytes": data, "_media_type": content_type}

    # The homepage itself had nothing; the brand's own linked social accounts
    # are the last thing tried before the caller falls back to Pexels.
    return await _social_hero(client, html, domain)


async def brand_site_hero(domain: str | None) -> dict | None:
    """The brand's own homepage hero (video or image candidate, not yet
    vision-verified). Cached per domain."""
    if not domain:
        return None
    if domain in _SITE_CACHE:
        return _SITE_CACHE[domain]
    result = None
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            result = await _scrape_site(client, domain)
    except Exception as exc:  # belt and braces — a hero banner is decoration
        logger.warning("brand_site_hero failed for %s: %s", domain, exc)
    _SITE_CACHE[domain] = result
    return result


async def _verify(candidate: dict, brand: str) -> bool | None:
    image_bytes = candidate.get("_bytes")
    if not image_bytes:
        return None  # a re-served cached candidate carries no bytes; unknown, not false
    if candidate["url"] in _VERDICT_CACHE:
        return _VERDICT_CACHE[candidate["url"]]
    verdict = await get_vision_client().image_shows_brand(image_bytes, candidate.get("_media_type", "image/jpeg"), brand)
    _VERDICT_CACHE[candidate["url"]] = verdict
    return verdict


def _public(candidate: dict) -> dict:
    return {k: v for k, v in candidate.items() if not k.startswith("_")}


async def resolve_brand_hero(domain: str | None, brand: str) -> dict | None:
    """The brand's own homepage hero, or None when its site has nothing usable
    or a candidate image was vision-confirmed to not actually show the brand.
    Callers fall back to Pexels stock media on None."""
    candidate = await brand_site_hero(domain)
    if not candidate:
        return None
    if candidate["type"] != "image" or candidate.get("source") != "brand_website":
        return _public(candidate)  # scraped straight from the brand's own markup or verified account
    verdict = await _verify(candidate, brand)
    if verdict is False:
        return None
    return _public(candidate)
