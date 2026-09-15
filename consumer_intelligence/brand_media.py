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


async def resolve_hero_media(query: str, *, prefer_video: bool = False) -> dict | None:
    """Pexels video (if preferred and available) else photo, else None."""
    if prefer_video:
        video = await pexels_video(query)
        if video:
            return video
    return await pexels_photo(query)


def _slug_domain(name: str) -> str | None:
    """Fallback domain from a brand name: "Meguiar's" -> meguiars.com."""
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
