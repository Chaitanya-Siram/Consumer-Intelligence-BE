"""Validated logos for the `meta.logos` registry every storyboard screen reads.

`brand_media.brand_logos` only builds Brandfetch CDN URL strings and never checks
them. That is wrong in two ways this module fixes:

  * Brandfetch answers HTTP 200 with a tiny placeholder image for a name it has no
    brand for ("Armor All News", "Industry News"), so the FE's <img onError>
    fallback never fires and a meaningless tile is shown.
  * The naive domain guess can pick a forum ("Tesla" -> teslamotorsclub.com).

Per name, candidates are tried in order and the first one that returns a real
image wins: Brandfetch, then Google's favicon service, then an Iconify
simple-icons glyph. A result is rejected when it is too small or when the same
bytes came back for a different name (placeholders are identical across names).
Names that are not entities at all (editorial sections such as "Industry News")
get no logo, so the FE draws initials instead.

Never raises; a name that cannot be resolved is simply left out.
"""

import asyncio
import hashlib
import logging
import os
import re

from . import brand_media

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REFERER = os.getenv("FRONTEND_URL", "http://localhost:3000") + "/"
_TIMEOUT = 8.0
_CONCURRENCY = 6
_MIN_RASTER_BYTES = 600  # Brandfetch's "no such brand" tile is ~300 bytes; real marks can be < 1 KB

_JUNK_WORDS = {
    "news", "industry", "competitors", "competitor", "general", "other", "others",
    "misc", "unknown", "various", "market", "category", "overall", "total", "review",
    "reviews", "forums", "forum", "blogs", "blog", "online",
}

# Platforms and publications whose logo domain is not a plain slug of their name.
_PLATFORM_DOMAINS = {
    "twitter": "x.com",
    "x": "x.com",
    "facebook": "facebook.com",
    "instagram": "instagram.com",
    "youtube": "youtube.com",
    "reddit": "reddit.com",
    "tiktok": "tiktok.com",
    "tumblr": "tumblr.com",
    "amazon": "amazon.com",
    "amazon uk": "amazon.com",  # regional storefronts share the parent brand's mark
    "amazon australia": "amazon.com",
}

# name -> validated url or None. Shared across lenses so 20 storyboards resolve
# each name once per process.
_CACHE: dict[str, str | None] = {}
# image-bytes hash -> the distinct names it came back for.
_HASH_OWNERS: dict[str, set[str]] = {}
# name -> hash of the image last accepted for it, to purge both sides of a clash.
_NAME_HASH: dict[str, str] = {}
# Brandfetch's stand-in tiles: one for a domain it has never seen, one for a
# registered domain with no brand. Learned by probing, never assumed.
_PLACEHOLDER_PROBES = ("zqxjkv-not-a-brand.com", "industrynews.com")
_PLACEHOLDERS: set[str] = set()
_SHORT_NAME = 3  # names this short map to random domains; trust Brandfetch only


def is_entity(name: str) -> bool:
    """False for section labels and noise that only look like brand names."""
    n = str(name or "").strip()
    if len(n) < 2 or n.isdigit():
        return False
    return not (set(re.findall(r"[a-z]+", n.lower())) & _JUNK_WORDS)


def candidate_domains(name: str, articles: list[dict]) -> list[str]:
    """Domains to try for `name`, most trustworthy first."""
    key = str(name).strip().lower()
    if key in _PLATFORM_DOMAINS:
        return [_PLATFORM_DOMAINS[key]]
    out: list[str] = []
    if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", key):  # the name is already a domain ("Cars.com")
        out.append(key)
    for d in (
        brand_media._slug_domain(name),  # noqa: SLF001 - shared curated table
        brand_media.guess_brand_domain(name, articles),
    ):
        if d and d not in out:
            out.append(d)
    return out


async def _fetch_image(client, url: str) -> bytes | None:
    try:
        resp = await client.get(url, headers={"User-Agent": _UA, "Referer": _REFERER}, timeout=_TIMEOUT, follow_redirects=True)
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            return None
        return resp.content
    except Exception as exc:
        logger.debug("logo fetch failed for %s: %s", url, exc)
        return None


def _root(domain: str) -> str:
    """"amazon" for amazon.com / amazon.co.uk / amazon.com.au: one brand, one logo."""
    labels = [p for p in domain.lower().split(".") if p not in ("com", "co", "org", "net", "uk", "au", "gov")]
    return labels[-1] if labels else domain.lower()


def _accept(name: str, data: bytes | None, domain: str, *, vector: bool = False) -> bool:
    """Real image, big enough, and not the same bytes a differently-named brand also got."""
    if not data or (not vector and len(data) < _MIN_RASTER_BYTES):
        return False
    digest = hashlib.md5(data).hexdigest()  # noqa: S324 - dedupe key only
    if digest in _PLACEHOLDERS:
        return False
    owners = _HASH_OWNERS.setdefault(digest, set())
    owners.add(_root(domain))
    _NAME_HASH[name] = digest
    return len(owners) == 1


async def _resolve_one(client, name: str, articles: list[dict]) -> str | None:
    if not is_entity(name) and name.strip().lower() not in _PLATFORM_DOMAINS:
        return None
    trust_fallbacks = len(name.strip()) > _SHORT_NAME
    for domain in candidate_domains(name, articles):
        url = brand_media.brandfetch_logo_url(domain)
        if url and _accept(name, await _fetch_image(client, url), domain):
            return url
        if not trust_fallbacks:
            continue
        favicon = f"https://www.google.com/s2/favicons?domain={domain}&sz=128"
        if _accept(name, await _fetch_image(client, favicon), domain):
            return favicon
    slug = re.sub(r"[^a-z0-9]", "", str(name).lower())
    glyph = f"https://api.iconify.design/simple-icons/{slug}.svg"
    if trust_fallbacks and slug and _accept(name, await _fetch_image(client, glyph), slug, vector=True):
        return glyph
    return None


async def _learn_placeholders(client) -> None:
    for domain in _PLACEHOLDER_PROBES:
        data = await _fetch_image(client, brand_media.brandfetch_logo_url(domain))
        if data:
            _PLACEHOLDERS.add(hashlib.md5(data).hexdigest())  # noqa: S324 - dedupe key only


async def resolve_logos(names: list[str], articles: list[dict]) -> dict[str, str]:
    """`{name: logo_url}` for every name that resolved to a real image."""
    todo = [n for n in dict.fromkeys(n for n in names if n) if n not in _CACHE]
    if todo:
        try:
            import httpx

            sem = asyncio.Semaphore(_CONCURRENCY)
            async with httpx.AsyncClient() as client:
                if not _PLACEHOLDERS:
                    await _learn_placeholders(client)

                async def one(n: str) -> None:
                    async with sem:
                        _CACHE[n] = await _resolve_one(client, n, articles)

                await asyncio.gather(*(one(n) for n in todo))
        except Exception as exc:  # belt and braces — logos are decoration
            logger.warning("logo resolution failed: %s", exc)
        # Identical bytes for two names means a placeholder: drop every owner.
        for name, digest in _NAME_HASH.items():
            if len(_HASH_OWNERS.get(digest, ())) > 1:
                _CACHE[name] = None
    return {n: _CACHE[n] for n in names if _CACHE.get(n)}


async def refine_logos(storyboard: dict, articles: list[dict], platforms: list[str]) -> None:
    """Replace `meta.logos` with validated entries and add the source platforms'
    icons, so quote attributions ("Forums · slickdeals.net") show one too."""
    meta = storyboard.setdefault("meta", {})
    names = [*(meta.get("logos") or {}), *platforms]
    meta["logos"] = await resolve_logos(names, articles)
