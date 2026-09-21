"""Brand logo, stock image and country-flag helpers for CI storyboards.

Sources, in fallback order:
1. Brandfetch CDN  — logo by domain (no API call needed; URL pattern only)
2. Brandfetch API  — full brand record (name, colors, logos) when API key present
3. Pexels API      — stock photo/video for hero and tab banners
4. Iconify flags   — ISO code so the FE can render `circle-flags:<iso>` / `cif:<iso>`

All fetchers are async, never raise, and return None on failure so a storyboard
always builds even when every media provider is unreachable.

Env vars (all optional):
  BRANDFETCH_CLIENT_ID   — `c=` query param on cdn.brandfetch.io (default: shared client)
  BRANDFETCH_API_KEY     — Bearer token for api.brandfetch.io/v2
  PEXELS_API_KEY         — Authorization header for api.pexels.com
"""

import asyncio
import logging
import os
import re

logger = logging.getLogger(__name__)

BRANDFETCH_CLIENT_ID = os.getenv("BRANDFETCH_CLIENT_ID", "1idkcE3U2haqHC6bThI")
BRANDFETCH_API_KEY = os.getenv("BRANDFETCH_API_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")

BRANDFETCH_CDN = "https://cdn.brandfetch.io"
BRANDFETCH_API = "https://api.brandfetch.io/v2/brands"
PEXELS_PHOTOS = "https://api.pexels.com/v1/search"
PEXELS_VIDEOS = "https://api.pexels.com/videos/search"

_TIMEOUT = 8.0


# ── Brandfetch ─────────────────────────────────────────────────────────────


def brandfetch_logo_url(domain: str | None) -> str | None:
    """CDN URL for a brand logo. Pure string build — no network call."""
    if not domain:
        return None
    domain = domain.strip().lower().removeprefix("www.")
    if not domain or "." not in domain:
        return None
    return f"{BRANDFETCH_CDN}/{domain}?c={BRANDFETCH_CLIENT_ID}"


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"https?://(?:www\.)?([^/:?#]+)", str(url))
    return m.group(1).lower() if m else None


def guess_brand_domain(brand: str, articles: list[dict]) -> str | None:
    """Find a domain from article URLs whose host contains the brand's first word."""
    if not brand:
        return None
    needle = brand.lower().split()[0]
    if len(needle) < 3:
        return None
    for article in articles:
        domain = domain_from_url(article.get("url"))
        if domain and needle in domain and not _is_news_host(domain):
            return domain
    return None


_NEWS_HOSTS = (
    "news", "times", "post", "reuters", "bloomberg", "cnn", "bbc", "guardian",
    "yahoo", "msn", "forbes", "wsj", "nytimes", "medium", "substack", "google",
)


def _is_news_host(domain: str) -> bool:
    return any(token in domain for token in _NEWS_HOSTS)


async def brandfetch_brand(domain: str | None) -> dict | None:
    """Full brand record from Brandfetch API. None if no key or lookup fails."""
    if not domain or not BRANDFETCH_API_KEY:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{BRANDFETCH_API}/{domain}",
                headers={"Authorization": f"Bearer {BRANDFETCH_API_KEY}"},
            )
        if resp.status_code != 200:
            return None
        data = resp.json()
        logos = data.get("logos") or []
        primary = next((l for l in logos if l.get("type") == "logo"), logos[0] if logos else None)
        formats = (primary or {}).get("formats") or []
        logo_src = next((f.get("src") for f in formats if f.get("format") in ("svg", "png")), None)
        colors = [c.get("hex") for c in (data.get("colors") or []) if c.get("hex")]
        return {
            "name": data.get("name"),
            "domain": domain,
            "logo_url": logo_src or brandfetch_logo_url(domain),
            "colors": colors[:3],
            "description": data.get("description"),
        }
    except Exception as exc:
        logger.debug("brandfetch lookup failed for %s: %s", domain, exc)
        return None


# ── Muck Rack ────────────────────────────────────────────────────────────
#
# A journalist byline has no domain of its own to hand Brandfetch, but Muck
# Rack — the standard public directory PR professionals already use to look
# up reporters — publishes one canonical, guessable profile URL per name
# with a real headshot in its `og:image` tag. The site sits behind a
# Cloudflare JS challenge, so a plain HTTP client (httpx) always gets the
# "Just a moment…" interstitial rather than the real page — this needs a
# real browser (Playwright, already a dependency for verbatim_capture.py's
# screenshot capture), same idea, no stealth guarantees beyond that.
# Best-effort only: the slug guess (lowercase, hyphenated full name) misses
# on middle names/suffixes, a journalist simply not listed, or a still-blocked
# challenge, and any of those returns None rather than raising — the
# caller's existing initials-avatar fallback is what every other lens
# already shows for a person with no photo.

MUCKRACK_PROFILE = "https://muckrack.com"
_OG_IMAGE_RX = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)
_MUCKRACK_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# A hit here is permanent (a real journalist's photo doesn't change identity),
# but a miss is often just Cloudflare's challenge not clearing in time rather
# than a genuine absence — so only successes are cached, never failures. Every
# build retries whatever names aren't cached yet; the cache only ever grows,
# converging toward full coverage for recurring bylines across sessions.
_PHOTO_CACHE_KEY = "author_photo_cache/muckrack.json"


def load_author_photo_cache() -> dict[str, str]:
    try:
        import json

        from file_helpers.s3_file import s3_file

        return json.loads(s3_file.download_file(_PHOTO_CACHE_KEY).decode("utf-8"))
    except Exception:
        return {}


def save_author_photo_cache(cache: dict[str, str]) -> None:
    try:
        import json

        from file_helpers.s3_file import s3_file

        s3_file.upload_file(_PHOTO_CACHE_KEY, json.dumps(cache).encode("utf-8"))
    except Exception as exc:
        logger.debug(f"Author photo cache write failed: {exc}")


def _muckrack_slug(name: str) -> str | None:
    slug = re.sub(r"[^a-z0-9\s-]", "", str(name or "").lower()).strip()
    slug = re.sub(r"\s+", "-", slug)
    return slug or None


async def muckrack_author_photo(name: str, *, browser=None) -> str | None:
    """Real headshot URL for a journalist's byline, via their Muck Rack
    profile page — or None on any miss (no slug, still-blocked challenge,
    404, no og:image). Pure best-effort lookup, never raises.

    `browser` is an already-launched Playwright browser to reuse across
    many lookups (see resolve_author_photos, which fans a batch of these
    out over one shared instance); omit it to launch+close a throwaway
    browser for a single one-off lookup."""
    slug = _muckrack_slug(name)
    if not slug:
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.info("Playwright not installed; skipping Muck Rack author photo lookup.")
        return None

    async def _fetch(b) -> str | None:
        page = None
        try:
            page = await b.new_page(user_agent=_MUCKRACK_UA, viewport={"width": 1280, "height": 800})
            response = await page.goto(f"{MUCKRACK_PROFILE}/{slug}", wait_until="domcontentloaded", timeout=15000)
            if response is not None and response.status >= 400:
                return None
            # Cloudflare's JS challenge (when this URL is behind one) runs a
            # few seconds of client-side computation before redirecting to
            # the real page; networkidle can fire while still parked on the
            # challenge itself, so wait for the og:image tag to actually
            # show up rather than trusting the first load event.
            try:
                await page.wait_for_selector('meta[property="og:image"]', timeout=10000)
            except Exception:
                pass
            html = await page.content()
            m = _OG_IMAGE_RX.search(html)
            return m.group(1) if m else None
        finally:
            if page is not None:
                await page.close()

    try:
        if browser is not None:
            return await _fetch(browser)
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            try:
                return await _fetch(b)
            finally:
                await b.close()
    except Exception as exc:
        logger.debug("muckrack lookup failed for %r: %s", name, exc)
        return None


# ── Pexels ────────────────────────────────────────────────────────────────


async def pexels_photo(query: str, *, orientation: str = "landscape") -> dict | None:
    """One stock photo for `query`. `{url, photographer, alt}` or None."""
    if not query or not PEXELS_API_KEY:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                PEXELS_PHOTOS,
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": query, "per_page": 1, "orientation": orientation},
            )
        if resp.status_code != 200:
            return None
        photos = resp.json().get("photos") or []
        if not photos:
            return None
        p = photos[0]
        return {
            "type": "image",
            "url": p["src"].get("large") or p["src"].get("medium"),
            "photographer": p.get("photographer"),
            "alt": p.get("alt") or query,
            "source": "pexels",
        }
    except Exception as exc:
        logger.debug("pexels photo failed for %r: %s", query, exc)
        return None


async def duckduckgo_image(query: str) -> dict | None:
    """One real image for `query` via DuckDuckGo's keyless image search — often
    the actual product or brand photo itself (a retailer listing, a press
    photo), not generic stock photography. No API key, no account; `ddgs` is
    a synchronous scraping client, so the call runs off-thread. Tried first,
    ahead of Pexels, for every banner/card photo — see `stock_photo` below.
    """
    if not query:
        return None
    try:
        from ddgs import DDGS

        def _search() -> list[dict]:
            return DDGS().images(query, max_results=1, safesearch="moderate")

        results = await asyncio.to_thread(_search)
        if not results:
            return None
        url = results[0].get("image")
        if not url:
            return None
        return {"type": "image", "url": url, "alt": results[0].get("title") or query, "source": "duckduckgo"}
    except Exception as exc:
        logger.debug("duckduckgo image search failed for %r: %s", query, exc)
        return None


# query -> resolved photo dict, or None once tried and nothing was found.
# Shared across every caller (per-dimension banners, lens-picker cards, an
# eventual rebuild of the same session) so the same query is never re-sent
# to DuckDuckGo/Pexels twice in one process's lifetime.
_STOCK_CACHE: dict[str, dict | None] = {}


async def stock_photo(query: str) -> dict | None:
    """One real photo for `query`: DuckDuckGo first, Pexels when DuckDuckGo
    has nothing usable. The one place both storyboard photo paths — the
    lens's top-level hero (`resolve_hero_media`) and every per-dimension/
    per-tab sub-banner (`resolve_slot_images`) — and the lens-picker's own
    card photos (see the `/consumer-intelligence/stock-image` endpoint) pick
    a stock image, so the fallback order and the cache only need to be right
    in one spot."""
    if not query:
        return None
    if query in _STOCK_CACHE:
        return _STOCK_CACHE[query]
    photo = await duckduckgo_image(query) or await pexels_photo(query)
    _STOCK_CACHE[query] = photo
    return photo


async def pexels_video(query: str) -> dict | None:
    """One short stock video for `query`. `{url, type: 'mp4'}` or None."""
    if not query or not PEXELS_API_KEY:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                PEXELS_VIDEOS,
                headers={"Authorization": PEXELS_API_KEY},
                params={"query": query, "per_page": 1, "orientation": "landscape", "size": "medium"},
            )
        if resp.status_code != 200:
            return None
        videos = resp.json().get("videos") or []
        if not videos:
            return None
        files = videos[0].get("video_files") or []
        mp4 = next((f for f in files if f.get("file_type") == "video/mp4" and (f.get("height") or 0) <= 1080), None)
        if not mp4:
            return None
        return {"type": "mp4", "url": mp4["link"], "source": "pexels"}
    except Exception as exc:
        logger.debug("pexels video failed for %r: %s", query, exc)
        return None


# ── Country flags (Iconify) ───────────────────────────────────────────────

_COUNTRY_ISO: dict[str, str] = {
    "united states": "us", "usa": "us", "us": "us", "america": "us",
    "united kingdom": "gb", "uk": "gb", "britain": "gb", "england": "gb",
    "india": "in", "canada": "ca", "australia": "au", "germany": "de",
    "france": "fr", "japan": "jp", "china": "cn", "brazil": "br",
    "mexico": "mx", "spain": "es", "italy": "it", "netherlands": "nl",
    "singapore": "sg", "uae": "ae", "united arab emirates": "ae",
    "saudi arabia": "sa", "south africa": "za", "south korea": "kr",
    "korea": "kr", "russia": "ru", "indonesia": "id", "philippines": "ph",
    "malaysia": "my", "thailand": "th", "vietnam": "vn", "pakistan": "pk",
    "bangladesh": "bd", "nigeria": "ng", "kenya": "ke", "egypt": "eg",
    "turkey": "tr", "poland": "pl", "sweden": "se", "norway": "no",
    "denmark": "dk", "finland": "fi", "ireland": "ie", "switzerland": "ch",
    "austria": "at", "belgium": "be", "portugal": "pt", "greece": "gr",
    "new zealand": "nz", "argentina": "ar", "chile": "cl", "colombia": "co",
    "israel": "il", "qatar": "qa", "hong kong": "hk", "taiwan": "tw",
}


def region_iso(region: str | None) -> str | None:
    """ISO 3166-1 alpha-2 for a country name, or None. FE renders as
    `<iconify-icon icon="circle-flags:{iso}">` or `cif:{iso}`."""
    if not region:
        return None
    key = str(region).strip().lower()
    if len(key) == 2 and key.isalpha():
        return key
    return _COUNTRY_ISO.get(key)


def flag_icons(iso: str | None) -> dict | None:
    """Iconify icon identifiers for FE flag rendering."""
    if not iso:
        return None
    return {"circle": f"circle-flags:{iso}", "flat": f"cif:{iso}"}


# ── Combined resolvers used by builder ────────────────────────────────────


async def resolve_brand_assets(brand: str, articles: list[dict]) -> dict:
    """Logo + colors for one brand. Always returns a dict; fields may be None."""
    domain = guess_brand_domain(brand, articles)
    record = await brandfetch_brand(domain) if domain else None
    return {
        "brand": brand,
        "domain": domain,
        "logo_url": (record or {}).get("logo_url") or brandfetch_logo_url(domain),
        "colors": (record or {}).get("colors") or [],
    }


async def resolve_hero_media(query: str, *, brand: str | None = None, domain: str | None = None, prefer_video: bool = False) -> dict | None:
    """The brand's own homepage hero (video, or a vision-verified image) when
    `brand`/`domain` are given — the strongest signal, since it's provably the
    brand's own material. Pexels video next (if preferred and available).
    `stock_photo` (DuckDuckGo, then Pexels) is the final fallback."""
    if domain:
        from . import brand_hero

        site_hero = await brand_hero.resolve_brand_hero(domain, brand or domain)
        if site_hero:
            return site_hero
    if prefer_video:
        video = await pexels_video(query)
        if video:
            return video
    return await stock_photo(query)


_SLOT_CONCURRENCY = 6
_HEADLINE_QUERY_CHARS = 60


def _slot_label(slot: dict) -> str:
    """The most specific name a sub-banner placeholder carries, for use as
    Pexels search context — a dimension's `name`, a trend tab's `title`, or
    whatever label the surrounding shell gave the slot."""
    for key in ("name", "title", "label"):
        value = slot.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Section banners carry a short tab title (eyebrow) plus an LLM-written
    # headline; both go in, the headline trimmed so the search stays focused.
    eyebrow = str(slot.get("eyebrow") or slot.get("tag") or "").strip()
    headline = str(slot.get("headline") or "").strip()[:_HEADLINE_QUERY_CHARS]
    return " ".join(filter(None, [eyebrow, headline]))


async def resolve_slot_images(storyboard: dict, brand: str, category: str) -> None:
    """Every `{"image": None, ...}` placeholder anywhere in the storyboard —
    a per-dimension KPI card (health.py), a per-trend tab banner (brand_intel.py,
    trend.py), a section banner (bci.py) — gets its own Pexels photo, not just
    the lens's single top-level hero. `resolve_hero_media` above only ever
    touches `storyboard["hero"]`; every other banner slot these builders emit
    was left permanently `None` with nothing downstream to fill it.

    Brand + category + the slot's own label keeps the query the same
    word-bias-safe shape as the top-level hero query: a bare brand name like
    "Armor All" reliably pulls literal knight/military-vehicle stock photos,
    but adding the product category turns the same search back into car-care
    imagery. Each slot's photo comes from `stock_photo` — DuckDuckGo's real,
    keyless image search first (often the actual product/brand photo, not
    generic stock imagery), Pexels as the fallback.

    Mutates the storyboard in place. A slot Pexels has nothing for is simply
    left at None, so the FE's existing gradient fallback still applies to it.
    Never raises — a missing sub-banner photo is decoration, not a build failure.
    """
    slots: list[dict] = []

    def walk(node: object, key: str = "") -> None:
        if isinstance(node, dict):
            # A `banner` dict is always a slot, even where its builder never
            # declared an `image` key (a dozen lenses' banners did not).
            if node.get("image") is None and ("image" in node or key == "banner"):
                slots.append(node)
            for k, value in node.items():
                walk(value, k)
        elif isinstance(node, list):
            for value in node:
                walk(value, key)

    walk(storyboard)
    if not slots:
        return

    sem = asyncio.Semaphore(_SLOT_CONCURRENCY)

    async def fill(slot: dict) -> None:
        query = " ".join(filter(None, [brand, category, _slot_label(slot)]))
        try:
            async with sem:
                photo = await stock_photo(query)
        except Exception as exc:  # belt and braces — a sub-banner photo is decoration
            logger.debug("slot image resolution failed for %r: %s", query, exc)
            return
        if photo:
            slot["image"] = photo["url"]

    await asyncio.gather(*(fill(s) for s in slots))


# Real domains for well-known names whose naive slug is wrong — major news
# outlets ("The New York Times" -> thenewyorktimes.com, not nytimes.com) and
# government agencies ("IRS Direct File" -> irsdirectfile.com, not irs.gov)
# are common enough in PR/media-monitoring datasets to warrant a small,
# curated override table rather than leaving every multi-word or
# abbreviated name to a wrong guess. Not exhaustive — a name not listed
# here still falls through to the slug guess.
_KNOWN_DOMAINS = {
    "the new york times": "nytimes.com",
    "new york times": "nytimes.com",
    "ap": "apnews.com",
    "associated press": "apnews.com",
    "the associated press": "apnews.com",
    "wsj": "wsj.com",
    "the wall street journal": "wsj.com",
    "wall street journal": "wsj.com",
    "the washington post": "washingtonpost.com",
    "washington post": "washingtonpost.com",
    "the guardian": "theguardian.com",
    "bbc": "bbc.com",
    "npr": "npr.org",
    "the hill": "thehill.com",
    "politico": "politico.com",
    "axios": "axios.com",
    "reuters": "reuters.com",
    "bloomberg": "bloomberg.com",
    "irs": "irs.gov",
    "irs direct file": "irs.gov",
    "internal revenue service": "irs.gov",
    # Social platforms named with a slash or shorthand slug to something wrong
    # ("Twitter/X" -> naive slug "twitterx.com", which Brandfetch resolves to
    # a near-blank 40x40 placeholder instead of failing outright, so the
    # <img> onError fallback never triggers).
    "twitter/x": "x.com",
    "twitter": "x.com",
    "x": "x.com",
}


def _slug_domain(name: str) -> str | None:
    """Fallback domain from a brand name: "Meguiar's" -> meguiars.com."""
    known = _KNOWN_DOMAINS.get(str(name or "").strip().lower())
    if known:
        return known
    slug = "".join(ch for ch in str(name or "").lower() if ch.isalnum())
    return f"{slug}.com" if slug else None


def brand_logos(names: list[str], articles: list[dict]) -> dict[str, str]:
    """`{brand: logo_url}` for every named brand — the `meta.logos` registry
    the storyboard screens read. Pure string work, no network call.

    Domain comes from an article URL naming the brand when one exists, else
    the lowercase alphanumeric slug + ".com". Brands with no url are skipped.
    """
    out: dict[str, str] = {}
    for name in dict.fromkeys(n for n in names if n):
        url = brandfetch_logo_url(guess_brand_domain(name, articles)) or brandfetch_logo_url(_slug_domain(name))
        if url:
            out[name] = url
    return out
