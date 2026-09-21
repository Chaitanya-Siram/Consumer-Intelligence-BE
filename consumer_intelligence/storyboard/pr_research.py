"""PR Research storyboard — PR Research Tier 1.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-pr-research.md
Screen:   Consumer-Intelligence-FE/src/screens/PrResearchScreen.jsx

Editorial/news-article lens (authors, publications, audience segments) built
around a *before/after* comparison — the source deck compares two tax
seasons. Rather than hardcode two calendar years, the tagged article set is
split into an early and late half by `timeseries.halves()` (median day of
the capture window), so the same code works for any dataset's own natural
before/after split. Within each half, recurring themes are the generic
`taxonomy` theme groups scoped to that half's positive-only/negative-only
rows (the same pattern Social Research uses for its driver cards). Audience
segment is a small, topic-agnostic classification from
pr_research_classify.prepare(). Every number here is computed from the
tagged articles; every prose field is left empty for narrative.py to fill.
"""

import asyncio
import logging

from .. import aggregate, brand_media, cohorts, quotes, taxonomy, timeseries
from ..pr_research_classify import SEGMENT_KEYS, SEGMENT_TITLES, label_of, prepare  # noqa: F401  (prepare re-exported for builder._HAS_PREPARE)

logger = logging.getLogger(__name__)

LENS_KEY = "pr_research"
__all__ = ["LENS_KEY", "build_storyboard", "prepare", "resolve_author_photos"]

_PHOTO_CONCURRENCY = 5

TABS = [
    {"id": "overview", "label": "Overview"},
    {"id": "editorial_analysis", "label": "Editorial Analysis"},
    {"id": "authors", "label": "Authors"},
    {"id": "publications", "label": "Publications"},
    {"id": "audience_profile", "label": "Audience Profile"},
    {"id": "appendix", "label": "Appendix"},
]

TOP_DRIVERS = 3
TOP_ENTITIES = 5
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def _sentiment_summary(sentiment: list[dict]) -> str:
    if not sentiment:
        return "No sentiment-scored posts"
    m = {r["tone"]: r for r in sentiment}
    return f"{m.get('pos', {}).get('pct', 0)}% positive · {m.get('neg', {}).get('pct', 0)}% negative · {m.get('neu', {}).get('pct', 0)}% neutral"


def _theme_drivers(rows: list[dict], limit: int = TOP_DRIVERS) -> list[dict]:
    counts = taxonomy.group_counts(rows)
    ranked = [g for g in sorted(counts, key=lambda k: (-counts[k], k)) if g != taxonomy.OTHERS][:limit]
    denom = len(rows) or 1
    return [{"key": g, "title": g, "pct": round(counts[g] * 100 / denom), "count": counts[g], "text": ""} for g in ranked]


def _rank_entities(groups: dict[str, list[dict]], limit: int) -> tuple[list[dict], list[dict]]:
    # aggregate.engagement(), not aggregate.reach(): the tagging pipeline
    # unconditionally overwrites every article's `reach` with a domain-level
    # traffic estimate (file_helpers/similare_web_reach.py get_reach()), so
    # summing it per author/publication multiplies a monthly domain-traffic
    # number by article count rather than reflecting real audience impact.
    rows = [
        {"name": name, "articles": len(rows_), "engagement": round(sum(aggregate.engagement(a) for a in rows_)), "sentiment": cohorts.sentiment_split(rows_)}
        for name, rows_ in groups.items()
    ]
    by_reach = sorted(rows, key=lambda e: -e["engagement"])[:limit]
    by_volume = sorted(rows, key=lambda e: -e["articles"])[:limit]
    return by_reach, by_volume


def _period_bundle(rows: list[dict], prepared: dict) -> dict:
    pos_rows = [a for a in rows if a.get("sentiment") == "Positive"]
    neg_rows = [a for a in rows if a.get("sentiment") == "Negative"]
    sentiment = cohorts.sentiment_split(rows)

    by_author: dict[str, list[dict]] = {}
    for a in rows:
        name = str(a.get("author") or "").strip()
        if name:
            by_author.setdefault(name, []).append(a)
    authors_by_reach, authors_by_volume = _rank_entities(by_author, TOP_ENTITIES)
    for row in authors_by_reach + authors_by_volume:
        pubs = sorted({cohorts.platform(a) for a in by_author[row["name"]]})
        row["affiliation"] = ", ".join(pubs[:2])

    by_pub: dict[str, list[dict]] = {}
    for a in rows:
        name = cohorts.platform(a)
        by_pub.setdefault(name, []).append(a)
    publications_by_reach, publications_by_volume = _rank_entities(by_pub, TOP_ENTITIES)

    seg_counts: dict[str, int] = {k: 0 for k in SEGMENT_KEYS}
    for a in rows:
        seg = label_of(a, prepared).get("audience_segment")
        if seg in seg_counts:
            seg_counts[seg] += 1
    seg_rows = cohorts.pct_rows({k: v for k, v in seg_counts.items() if v})
    audience = [{"key": r["name"], "title": SEGMENT_TITLES.get(r["name"], r["name"]), "pct": r["pct"], "count": r["count"], "text": ""} for r in seg_rows]

    return {
        "window": timeseries.window_label(rows),
        "total": len(rows),
        "sentiment": sentiment,
        "sentiment_summary": _sentiment_summary(sentiment),
        "positive_drivers": _theme_drivers(pos_rows),
        "negative_drivers": _theme_drivers(neg_rows),
        "authors_by_reach": authors_by_reach,
        "authors_by_volume": authors_by_volume,
        "publications_by_reach": publications_by_reach,
        "publications_by_volume": publications_by_volume,
        "audience": audience,
        "audience_top": audience[0]["title"] if audience else "—",
        "quotes_positive": quotes.pick(pos_rows, limit=quotes.TOP_POSTS),
        "quotes_negative": quotes.pick(neg_rows, limit=quotes.TOP_POSTS),
    }


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)

    early_rows, late_rows = timeseries.halves(articles)
    early = _period_bundle(early_rows, prepared) if early_rows else None
    late = _period_bundle(late_rows, prepared) if late_rows else None

    sentiment_all = cohorts.sentiment_split(articles)
    sentiment_by_tone = {r["tone"]: r for r in sentiment_all}

    overview = {
        "banner": {"eyebrow": "Overview", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Articles"},
            {"value": f"{sentiment_by_tone.get('pos', {}).get('pct', 0) - sentiment_by_tone.get('neg', {}).get('pct', 0):+d}" if sentiment_all else "—", "label": "Net Sentiment"},
            {"value": late["audience_top"] if late else (early["audience_top"] if early else "—"), "label": "Leading Audience"},
            {"value": "2", "label": "Periods Compared"},
        ]},
        "note": "",
    }

    editorial_analysis = {
        "banner": {"eyebrow": "Editorial Analysis", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Articles"},
            {"value": early["window"] if early else "—", "label": "Earlier Period"},
            {"value": late["window"] if late else "—", "label": "Later Period"},
        ]},
        "note": "",
        "narrative_shift": "",
        "early": early,
        "late": late,
        # Same dict objects as early/late's own quotes_positive/quotes_negative,
        # flattened under the field name builder.py's verbatim-evidence
        # resolver looks for (`_VERBATIM_SECTIONS`) — a real embed/screenshot
        # mutates each dict in place, visible via either reference.
        "quotes": (early["quotes_positive"] + early["quotes_negative"] if early else []) + (late["quotes_positive"] + late["quotes_negative"] if late else []),
    }

    tracked_period = late or early
    authors = {
        "banner": {"eyebrow": "Authors", "headline": "", "sub": "", "stats": [
            {"value": str(len(tracked_period["authors_by_reach"])) if tracked_period else "0", "label": "Top Authors Tracked"},
        ]},
        "note": "",
        "early": early,
        "late": late,
    }

    publications = {
        "banner": {"eyebrow": "Publications", "headline": "", "sub": "", "stats": [
            {"value": str(len({cohorts.platform(a) for a in articles})), "label": "Distinct Publications"},
        ]},
        "note": "",
        "early": early,
        "late": late,
    }

    audience_profile = {
        "banner": {"eyebrow": "Audience Profile", "headline": "", "sub": "", "stats": [
            {"value": late["audience_top"] if late else (early["audience_top"] if early else "—"), "label": "Leading Segment"},
        ]},
        "note": "",
        "early": early,
        "late": late,
    }

    appendix = {
        "banner": {"eyebrow": "Appendix", "headline": "Source Citations", "sub": "", "stats": []},
        "groups": [
            {"title": f"{early['window'] if early else 'Earlier'} · Positive", "sources": [{"title": quotes.truncate(q["text"], 90), "source": q["source"], "url": q["url"]} for q in (early["quotes_positive"] if early else [])]},
            {"title": f"{early['window'] if early else 'Earlier'} · Negative", "sources": [{"title": quotes.truncate(q["text"], 90), "source": q["source"], "url": q["url"]} for q in (early["quotes_negative"] if early else [])]},
            {"title": f"{late['window'] if late else 'Later'} · Positive", "sources": [{"title": quotes.truncate(q["text"], 90), "source": q["source"], "url": q["url"]} for q in (late["quotes_positive"] if late else [])]},
            {"title": f"{late['window'] if late else 'Later'} · Negative", "sources": [{"title": quotes.truncate(q["text"], 90), "source": q["source"], "url": q["url"]} for q in (late["quotes_negative"] if late else [])]},
        ],
    }

    labels_meta = prepared.get("labels") or {}
    named = {brand} if brand else set()
    named.update(cohorts.platform(a) for a in articles)
    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "window": window,
            "total_mentions": total,
            "logos": brand_media.brand_logos(sorted(named), articles),
            "method": {
                "periods": "capture window split into an early and late half by median article date (timeseries.halves)",
                "drivers": "top theme groups within each period's positive-only and negative-only article subsets",
                "authors_publications": "grouped by author/publication, reach summed from each article's own reach, ranked by reach and by article count",
                "audience": "LLM-classified reader-segment per article, on a small fixed topic-agnostic vocabulary",
            },
            "classification": {"labels": labels_meta.get("method")},
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["PR Research · Consumer Intelligence", f"Computed from {total:,} tagged articles"],
        "overview": overview,
        "editorial_analysis": editorial_analysis,
        "authors": authors,
        "publications": publications,
        "audience_profile": audience_profile,
        "appendix": appendix,
        "evidence": {
            "early_positive_themes": {t["title"]: [_brief(a) for a in early_rows if a.get("sentiment") == "Positive" and a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in (early["positive_drivers"] if early else [])},
            "early_negative_themes": {t["title"]: [_brief(a) for a in early_rows if a.get("sentiment") == "Negative" and a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in (early["negative_drivers"] if early else [])},
            "late_positive_themes": {t["title"]: [_brief(a) for a in late_rows if a.get("sentiment") == "Positive" and a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in (late["positive_drivers"] if late else [])},
            "late_negative_themes": {t["title"]: [_brief(a) for a in late_rows if a.get("sentiment") == "Negative" and a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in (late["negative_drivers"] if late else [])},
        },
    }


async def resolve_author_photos(storyboard: dict) -> None:
    """Real Muck Rack headshots for every author row (both rankings, both
    periods), mutated in place. Deduplicated by name — an author who leads
    both the reach and volume rankings, or appears in both periods, is
    looked up once, over one shared browser instance rather than one launch
    per author. A global cache (see brand_media.load/save_author_photo_cache)
    means a name only ever needs to clear Cloudflare's challenge once across
    every session, ever — this call only fetches names not already cached.
    Never raises: a name that's neither cached nor freshly resolved just
    keeps the FE's existing initials-avatar fallback."""
    authors = storyboard.get("authors") or {}
    rows_by_name: dict[str, list[dict]] = {}
    for period in (authors.get("early"), authors.get("late")):
        if not period:
            continue
        for row in [*period.get("authors_by_reach", []), *period.get("authors_by_volume", [])]:
            rows_by_name.setdefault(row["name"], []).append(row)

    if not rows_by_name:
        return

    cache = brand_media.load_author_photo_cache()
    to_fetch = []
    for name, rows in rows_by_name.items():
        cached_url = cache.get(name)
        if cached_url:
            for row in rows:
                row["photo_url"] = cached_url
        else:
            to_fetch.append(name)

    if not to_fetch:
        return

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.info("Playwright not installed; skipping Muck Rack author photo lookups.")
        return

    sem = asyncio.Semaphore(_PHOTO_CONCURRENCY)
    newly_found: dict[str, str] = {}

    async def one(browser, name: str) -> None:
        async with sem:
            url = await brand_media.muckrack_author_photo(name, browser=browser)
        if url:
            newly_found[name] = url
            for row in rows_by_name[name]:
                row["photo_url"] = url

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                await asyncio.gather(*(one(browser, name) for name in to_fetch), return_exceptions=True)
            finally:
                await browser.close()
    except Exception as exc:
        logger.warning("Author photo batch unavailable: %s", exc)

    if newly_found:
        cache.update(newly_found)
        brand_media.save_author_photo_cache(cache)
