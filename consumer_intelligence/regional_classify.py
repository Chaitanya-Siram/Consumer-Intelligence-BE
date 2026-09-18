"""Shared per-article preparation for the Regional Intelligence lenses
(regional_sentiment, regional_engagement, regional_brand_perception).

Contract: Consumer-Intelligence-FE/docs/ci-lens-contracts-regional.md §2, §5.

Market per article, in this order (source recorded per post):
  countries  the tagger's `countries` list, first entry, normalised
  cctld      country-code TLD or known regional domain of the source URL
  llm        an explicit place cue in the text (city, state, country,
             currency, national retailer), confidence >= LLM_GATE
  default    the tagger's `region` field, which in this platform is the
             session's configured market (the same value on every post), so
             it is evidence of the project's market, not of the post's
Articles with no market at all are excluded from every region but still
count in meta.total_mentions.

Product type per article (for the Engagement lens): the tagger has no product
entity beyond a sparse generic `product mentions`, so the same batched LLM
pass names the product type the post is about; labels merge into <= 8 types
shared across all markets so H1 and H2 lists use consistent names.

Discussion themes reuse taxonomy.canonicalize (theme -> <= 8 groups).
All three lenses share this prepare() via PREPARE_KEY = "regional".
"""

import asyncio
import logging
import re

from . import aggregate, taxonomy
from .brand_media import region_iso
from .narrative_client import get_narrative_client
from .whitespace_classify import merge_labels

logger = logging.getLogger(__name__)

PREPARE_KEY = "regional"
MIN_MARKET_POSTS = 30
FALLBACK_MIN_MARKET_POSTS = 10   # used only when no market reaches MIN_MARKET_POSTS
MAX_MARKETS = 8
LLM_GATE = 0.7
BATCH = 60
CONCURRENCY = 3

_ALIASES = {
    "us": "United States", "usa": "United States", "u.s.": "United States", "u.s.a.": "United States",
    "united states": "United States", "united states of america": "United States", "america": "United States",
    "uk": "United Kingdom", "u.k.": "United Kingdom", "britain": "United Kingdom", "great britain": "United Kingdom", "england": "United Kingdom",
    "uae": "United Arab Emirates", "korea": "South Korea", "republic of korea": "South Korea",
}
_CCTLD = {
    "co.uk": "United Kingdom", "uk": "United Kingdom", "de": "Germany", "fr": "France", "com.au": "Australia", "au": "Australia",
    "ca": "Canada", "co.in": "India", "in": "India", "co.jp": "Japan", "jp": "Japan", "kr": "South Korea", "co.kr": "South Korea",
    "com.br": "Brazil", "br": "Brazil", "mx": "Mexico", "es": "Spain", "it": "Italy", "nl": "Netherlands", "se": "Sweden",
    "sg": "Singapore", "co.nz": "New Zealand", "nz": "New Zealand", "za": "South Africa", "co.za": "South Africa", "ie": "Ireland", "ae": "United Arab Emirates",
}
_REGIONAL_DOMAINS = {"naver.com": "South Korea", "daum.net": "South Korea", "canadiantire.ca": "Canada", "flipkart.com": "India"}


def article_id(a: dict) -> str:
    return str(a.get("id") or a.get("article_ref") or "")


def canonical_country(value) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip().strip(".").strip()
    if not text or text.lower() in {"none", "null", "n/a", "[]", ""}:
        return None
    low = text.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    iso = region_iso(text)
    if iso and len(text) == 2:
        return _ALIASES.get(low, text.upper())
    return text.title() if text.islower() else text


def _countries(a: dict) -> list[str]:
    v = a.get("countries")
    items: list = []
    if isinstance(v, list):
        items = v
    elif v:
        s = str(v).strip()
        if s.startswith("["):
            items = [t.strip(" '\"") for t in s.strip("[]").split(",")]
        else:
            items = [s]
    out = []
    for it in items:
        c = canonical_country(it)
        if c:
            out.append(c)
    return out


def _host(a: dict) -> str:
    host = str(a.get("domain") or a.get("domain_name") or "").lower()
    if not host:
        m = re.match(r"https?://(?:www\.)?([^/]+)", str(a.get("url") or ""))
        host = m.group(1).lower() if m else ""
    return host


def cctld_market(a: dict) -> str | None:
    host = _host(a)
    if not host:
        return None
    for dom, market in _REGIONAL_DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return market
    for suf in sorted(_CCTLD, key=len, reverse=True):
        if host.endswith("." + suf):
            return _CCTLD[suf]
    return None


def default_market(a: dict) -> str | None:
    return canonical_country(a.get("region"))


def flag_for(iso: str | None) -> str | None:
    if not iso or len(iso) != 2 or not iso.isalpha():
        return None
    return "".join(chr(0x1F1E6 + ord(ch) - ord("a")) for ch in iso.lower())


def _brief(a: dict, chars: int = 240) -> dict:
    return {"id": article_id(a), "title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "source": _host(a)[:60], "product_hint": (str(a.get("product mentions") or "")[:60] if str(a.get("product mentions") or "").lower() not in ("", "none") else "")}


async def _label_batch(briefs: list[dict], brand: str, known: list[str], sem: asyncio.Semaphore) -> dict[str, dict]:
    payload = {
        "brand": brand or None,
        "tracked_brands": known,
        "posts": briefs,
        "schema": {"posts": [{
            "id": "id from input",
            "market": "country name ONLY when the text carries an explicit place cue (city, state, country, currency, national retailer, national event); empty otherwise",
            "market_confidence": "0-1; 0 when market is empty",
            "product_type": "2-4 word product type the post is mainly about (e.g. 'interior cleaner', 'car wax', 'detailing kit', 'tyre shine'); empty when no specific product type",
        }]},
    }
    system = (
        "You label posts about a brand and its category. Name the market country only from explicit cues in the text; "
        "never guess from language or brand alone, and give low confidence when unsure. Name the product type the post is "
        "mainly about, generic to the category, not a brand name. Return ONLY JSON."
    )
    ids = {b["id"] for b in briefs}
    async with sem:
        raw = await asyncio.wait_for(
            get_narrative_client().complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": f"Label these posts.\n\nINPUT:\n{payload}"}],
                temperature=0.0,
            ),
            timeout=180,
        )
    out: dict[str, dict] = {}
    for it in (raw.get("posts") if isinstance(raw, dict) else None) or []:
        if not isinstance(it, dict) or str(it.get("id")) not in ids:
            continue
        try:
            conf = float(it.get("market_confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        market = canonical_country(it.get("market")) if conf >= LLM_GATE else None
        ptype = re.sub(r"\s+", " ", str(it.get("product_type") or "")).strip(" .").lower()[:40]
        out[str(it["id"])] = {"llm_market": market, "llm_confidence": round(conf, 2), "product_type": ptype}
    return out


async def label_articles(articles: list[dict], *, brand: str, known: list[str]) -> dict:
    briefs = [_brief(a) for a in articles if article_id(a)]
    if not briefs:
        return {"by_id": {}, "method": "empty"}
    batches = [briefs[i : i + BATCH] for i in range(0, len(briefs), BATCH)]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_label_batch(b, brand, known, sem) for b in batches), return_exceptions=True)
    by_id: dict[str, dict] = {}
    failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            logger.warning("Regional label batch failed: %s", r)
            continue
        by_id.update(r)
    for b in briefs:
        by_id.setdefault(b["id"], {"llm_market": None, "llm_confidence": 0.0, "product_type": b["product_hint"].lower()[:40]})
    method = "fallback" if failed == len(batches) else ("llm" if not failed else f"llm ({failed} of {len(batches)} batches fell back)")
    return {"by_id": by_id, "method": method}


def resolve_market(a: dict, llm: dict | None) -> tuple[str | None, str]:
    cs = _countries(a)
    if cs:
        return cs[0], "countries"
    tld = cctld_market(a)
    if tld:
        return tld, "cctld"
    if llm and llm.get("llm_market"):
        return llm["llm_market"], "llm"
    d = default_market(a)
    if d:
        return d, "default"
    return None, "none"


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "market"


async def prepare(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    known = known_brands or ([brand] if brand else [])
    labels_task = label_articles(articles, brand=brand, known=known)
    tax_task = taxonomy.canonicalize(articles, brand=brand)
    labels, tax = await asyncio.gather(labels_task, tax_task)
    by_id = labels["by_id"]

    # Market per article + market table
    assignment: dict[str, dict] = {}
    counts: dict[str, int] = {}
    sources: dict[str, dict[str, int]] = {}
    for a in articles:
        aid = article_id(a)
        market, source = resolve_market(a, by_id.get(aid))
        assignment[aid] = {"market": market, "source": source}
        if market:
            counts[market] = counts.get(market, 0) + 1
            per_source = sources.setdefault(market, {})
            per_source[source] = per_source.get(source, 0) + 1
    threshold = MIN_MARKET_POSTS
    kept = [m for m in counts if counts[m] >= threshold]
    if not kept:
        threshold = FALLBACK_MIN_MARKET_POSTS
        kept = [m for m in counts if counts[m] >= threshold]
    kept.sort(key=lambda m: (-counts[m], m))
    markets = []
    for m in kept[:MAX_MARKETS]:
        iso = region_iso(m)
        markets.append({"key": slug(m), "name": m, "iso": iso, "flag": flag_for(iso), "count": counts[m], "sources": sources[m]})

    # Product types shared across markets
    pt_counts: dict[str, int] = {}
    for lab in by_id.values():
        if lab.get("product_type"):
            pt_counts[lab["product_type"]] = pt_counts.get(lab["product_type"], 0) + 1
    product_types = await merge_labels(pt_counts, min_n=4, max_n=8, kind="product types in this category", brand=brand)

    return {
        "labels": labels,
        "assignment": assignment,
        "markets": markets,
        "market_threshold": threshold,
        "unassigned": sum(1 for v in assignment.values() if not v["market"]),
        "product_types": product_types,
        "taxonomy": tax,
    }


def market_of(a: dict, prepared: dict) -> str | None:
    return ((prepared.get("assignment") or {}).get(article_id(a)) or {}).get("market")


def product_type_of(a: dict, prepared: dict) -> str | None:
    lab = (prepared.get("labels") or {}).get("by_id", {}).get(article_id(a)) or {}
    raw = lab.get("product_type") or ""
    return ((prepared.get("product_types") or {}).get("map") or {}).get(raw) if raw else None
