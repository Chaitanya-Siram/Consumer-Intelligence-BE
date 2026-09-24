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
from urllib.parse import urlsplit

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
# Cloudflare challenge that a plain HTTP client or a stock headless browser
# never clears, so profiles are fetched with scrapling's stealth fetcher,
# which solves it.
#
# The URL is a *guess* (lowercase, hyphenated full name), so a hit is only
# trusted when the profile's own name matches the byline — otherwise a short
# handle such as "DFB" lands on some unrelated journalist's page — and Muck
# Rack's generic silhouette is never used as a "photo". Best-effort: any miss
# returns nothing, and the caller keeps its initials-avatar fallback.

MUCKRACK_PROFILE = "https://muckrack.com"
_MAX_PROFILE_LOOKUPS = 12  # each stealth fetch takes ~10s; bound one build's cost
_PROFILE_TIMEOUT_MS = 60000
_PLACEHOLDER_PHOTO_MARKERS = ("icon-user-circle", "/static/images/")
_NAME_TOKEN_RX = re.compile(r"[a-z0-9]+")

# A hit is permanent (a real journalist's photo doesn't change identity),
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


def _name_tokens(name: str) -> list[str]:
    return _NAME_TOKEN_RX.findall(str(name or "").lower())


def looks_like_person_name(name: str) -> bool:
    """A byline worth a directory lookup: at least two words. A lone token —
    a forum handle ("kristyg58"), a brand ("Meguiars"), "Anonymous" — is not a
    reporter's name, and its guessed slug only ever finds someone else."""
    return len(_name_tokens(name)) >= 2


def is_placeholder_photo(url: str | None) -> bool:
    return not url or any(marker in url for marker in _PLACEHOLDER_PHOTO_MARKERS)


def profile_matches(name: str, profile_name: str) -> bool:
    """The profile is this byline's: every word of the byline appears in the
    profile's own name ("Kara Swisher" is "Kara Swisher"; a middle name is
    fine; "DFB" is not "Daniel Farber-Ball")."""
    wanted = _name_tokens(name)
    return len(wanted) >= 2 and set(wanted) <= set(_name_tokens(profile_name))


async def resolve_muckrack_photos(rows_by_name: dict[str, list[dict]]) -> None:
    """Set `photo_url` on every row whose byline has a real Muck Rack headshot,
    mutating the rows in place. The one implementation behind every lens that
    lists journalists (PR Research authors, Congruence's reporter influence).

    Deduplicated by name and served from the shared cache (a name only ever has
    to clear Cloudflare once, across every session); only reporter-like bylines
    are looked up, and only a profile whose own name matches is used — see
    `looks_like_person_name`, `profile_matches`. Never raises."""
    if not rows_by_name:
        return
    cache = load_author_photo_cache()
    to_fetch = []
    for name, rows in rows_by_name.items():
        if not looks_like_person_name(name):
            continue
        cached_url = cache.get(name)
        if cached_url and not is_placeholder_photo(cached_url):
            for row in rows:
                row["photo_url"] = cached_url
        else:
            to_fetch.append(name)
    if not to_fetch:
        return
    found = await muckrack_author_photos(to_fetch)
    for name, url in found.items():
        for row in rows_by_name[name]:
            row["photo_url"] = url
    if found:
        cache.update(found)
        save_author_photo_cache(cache)


def _muckrack_slugs(name: str) -> list[str]:
    """Profile URL slugs to try, most common first: Muck Rack canonicalises
    some names hyphenated ("kara-swisher" redirects) and others joined
    ("waltmossberg" — the hyphenated form 404s)."""
    words = re.sub(r"[^a-z0-9\s-]", "", str(name or "").lower()).split()
    if not words:
        return []
    return list(dict.fromkeys(["-".join(words), "".join(words)]))


def _first_matching_profile_photo(session, name: str) -> str | None:
    for slug in _muckrack_slugs(name):
        try:
            page = session.fetch(f"{MUCKRACK_PROFILE}/{slug}")
            if page.status >= 400:
                continue
            profile_name = " ".join((page.css("h1.profile-name::text").get() or "").split())
            photo = page.css('meta[property="og:image"]::attr(content)').get()
        except Exception as exc:
            logger.debug("muckrack lookup failed for %r (%s): %s", name, slug, exc)
            continue
        if profile_matches(name, profile_name) and not is_placeholder_photo(photo):
            return photo
    return None


def _lookup_muckrack_batch(names: list[str]) -> dict[str, str]:
    """Blocking: one shared stealth browser for the whole batch."""
    from scrapling.fetchers import StealthySession

    found: dict[str, str] = {}
    with StealthySession(headless=True, solve_cloudflare=True, network_idle=True, timeout=_PROFILE_TIMEOUT_MS) as session:
        for name in names:
            photo = _first_matching_profile_photo(session, name)
            if photo:
                found[name] = photo
    return found


async def muckrack_author_photos(names: list[str]) -> dict[str, str]:
    """`{byline: headshot_url}` for the bylines that resolved to a matching,
    real Muck Rack profile photo. Never raises."""
    todo = [n for n in dict.fromkeys(names) if looks_like_person_name(n)][:_MAX_PROFILE_LOOKUPS]
    if not todo:
        return {}
    try:
        return await asyncio.to_thread(_lookup_muckrack_batch, todo)
    except Exception as exc:
        logger.warning("Muck Rack author photo batch unavailable: %s", exc)
        return {}


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


_DDG_RESULTS = 5  # candidates fetched so a blocked-host hit doesn't waste the whole search
# `ddgs` is a metasearch client: with the default backend="auto" it fans every
# query out to all engines it knows (for images: Bing and DuckDuckGo) and logs
# an "Error in engine duckduckgo: TimeoutException" for each one that never
# answers. From Render's egress IPs DuckDuckGo's HTML endpoint times out on
# every single call (0 of 107 attempts in one afternoon) while Bing Images
# answers 200 every time, so the search is pinned to the engines that work
# and given a short timeout — a dead engine otherwise costs the full 5s wait
# per banner query, dozens of times per build. Override per deployment with
# DDGS_IMAGE_BACKEND / DDGS_TEXT_BACKEND (comma-separated engine names) and
# DDGS_TIMEOUT if another region's IPs see a different picture.
DDG_IMAGE_BACKEND = os.getenv("DDGS_IMAGE_BACKEND", "bing")
DDG_TEXT_BACKEND = os.getenv("DDGS_TEXT_BACKEND", "yahoo,startpage")  # ddgs has no Bing text engine
DDG_TIMEOUT = float(os.getenv("DDGS_TIMEOUT", "4"))
# Hosts whose images a plain server-side fetch can load but a real browser
# cannot: lookaside.fbsbx.com is Facebook's *own crawler's* proxy for a page's
# preview image — it answers a bare GET (curl, httpx) but rejects the request
# a real <img> tag makes, so a URL from here always renders as broken.
_UNRENDERABLE_IMAGE_HOSTS = ("fbsbx.com",)


def _is_browser_renderable(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return bool(host) and not any(host == h or host.endswith("." + h) for h in _UNRENDERABLE_IMAGE_HOSTS)


_LIVENESS_TIMEOUT = 6.0
# GET, never HEAD: some CDNs (imgix, S3, retailer product-image hosts) 404,
# 403 or method-not-allow a HEAD request while serving the same URL fine on
# GET, so a HEAD-only check would reject perfectly good photos. `stream`
# fetches only the response headers here — the body is never read.
_RANGE_HEADER = {"Range": "bytes=0-0"}


async def _url_is_live(url: str) -> bool:
    """True when `url` serves an actual image right now — not just a
    well-formed link on a renderable host. A DuckDuckGo hit is scraped from
    an arbitrary web page; the CDN asset behind it can be renamed, hotlink-
    protected, or pulled entirely by the time the dashboard renders, and a
    dead one then shows as a permanently blank card with no way for the FE to
    recover a real photo on its own. Checking liveness once here, at build
    time, is what makes a resolved image URL trustworthy for good — the FE's
    `useVerifiedImage` probe is only a last-resort safety net for photos that
    go stale after the payload was cached, not the primary defence."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_LIVENESS_TIMEOUT, follow_redirects=True) as client, client.stream(
            "GET", url, headers=_RANGE_HEADER
        ) as resp:
            content_type = resp.headers.get("content-type", "")
            return resp.status_code < 400 and content_type.startswith("image/")
    except Exception as exc:
        logger.debug("image liveness check failed for %r: %s", url, exc)
        return False


async def duckduckgo_image(query: str) -> dict | None:
    """One real image for `query` via DuckDuckGo's keyless image search — often
    the actual product or brand photo itself (a retailer listing, a press
    photo), not generic stock photography. No API key, no account; `ddgs` is
    a synchronous scraping client, so the call runs off-thread. Tried first,
    ahead of Pexels, for every banner/card photo — see `stock_photo` below.

    Every renderable-host candidate is verified live (see `_url_is_live`)
    before being returned, concurrently and in DuckDuckGo's own ranked order,
    so the first dead or hotlink-blocked hit never gets shipped to the FE —
    the next real candidate is used instead.
    """
    if not query:
        return None
    try:
        from ddgs import DDGS

        def _search() -> list[dict]:
            return DDGS(timeout=DDG_TIMEOUT).images(query, max_results=_DDG_RESULTS, safesearch="moderate", backend=DDG_IMAGE_BACKEND)

        results = await asyncio.to_thread(_search)
        candidates = [r for r in (results or []) if r.get("image") and _is_browser_renderable(r["image"])]
        if not candidates:
            return None
        alive = await asyncio.gather(*(_url_is_live(c["image"]) for c in candidates))
        for result, is_alive in zip(candidates, alive):
            if is_alive:
                return {"type": "image", "url": result["image"], "alt": result.get("title") or query, "source": "duckduckgo"}
        return None
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
    # AI assistants (the Congruence lens's "Cited by" chips): the naive slug for
    # "Claude" or "Gemini" is an unrelated company's site.
    "chatgpt": "chatgpt.com",
    "claude": "claude.ai",
    "gemini": "gemini.google.com",
    "perplexity": "perplexity.ai",
    "copilot": "copilot.microsoft.com",
    "grok": "grok.com",
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
