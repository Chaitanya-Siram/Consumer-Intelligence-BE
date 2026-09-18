"""Dominant Narratives storyboard — Landscape Analysis Tier 2.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-dominant-narratives.md
Screen:   Consumer-Intelligence-FE/src/screens/DominantNarrativesScreen.jsx

Numbers are computed here from the tagged articles plus the per-article
labels produced by narratives_classify.prepare() (usage group, question
intent, merged outlook theme). Prose is left empty for narrative.py, which
receives real sample posts per section under `evidence`.

Secondary research (contract §2): omitted. No bullet ever carries ext: true.
"""

import re
from collections import Counter

from .. import aggregate, brand_media, cohorts, quotes, timeseries
from ..narratives_classify import (
    QUESTION_KEYS,
    USAGE_KEYS,
    USAGE_TITLES,
    outlook_of,
    prepare,  # re-exported for builder._HAS_PREPARE
    question_of,
    usage_of,
)

LENS_KEY = "dominant_narratives"
__all__ = ["LENS_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "t1", "label": "Usage & Engagement"},
    {"id": "t2", "label": "Landscape Observations"},
    {"id": "t3", "label": "Current Landscape"},
    {"id": "t4", "label": "Audience Outlook"},
]
QUESTIONS = {
    "q1": "Why does the audience need this category?",
    "q2": "What is their general perception and emotion around it?",
    "q3": "What motivates them to own or try more than one brand?",
    "q4": "How do they decide which product to use from what they own?",
    "reco": "Recommendations on how to engage this audience",
}
MAX_PLATFORMS = 5
MAX_ISSUERS = 4
SAMPLE = 8
_AWARD = re.compile(r"\b(best|ranked|ranking|award|top pick|editor'?s choice|winner|#1|number one)\b", re.I)


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def _fold(counts: Counter, limit: int, *, others: str | None) -> list[dict]:
    """Top `limit` rows desc; tail folded into `others` (or into the smallest kept
    row when others is None), integer pcts summing to 100."""
    if not counts:
        return []
    ranked = [k for k, _ in counts.most_common()]
    keep = ranked[:limit]
    tail = sum(counts[k] for k in ranked[limit:])
    folded = {k: counts[k] for k in keep}
    if tail:
        if others:
            folded[others] = folded.get(others, 0) + tail
        else:
            folded[keep[-1]] += tail
    order = sorted(folded, key=lambda k: (k == others, -folded[k], k))
    return cohorts.pct_rows(folded, order=order)


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)
    brand_rows = cohorts.brand_articles(articles, brand, known)

    # ── tab 1: usage groups ───────────────────────────────────────────────
    by_usage: dict[str, list[dict]] = {k: [] for k in USAGE_KEYS}
    for a in articles:
        k = usage_of(a, prepared)
        if k in by_usage:
            by_usage[k].append(a)
    top_use = aggregate.top_n(aggregate.count_by(by_usage["patterns"], "theme", skip_junk=True), 1)
    usage = {
        "banner": {
            "eyebrow": "Category Landscape",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": str(sum(1 for k in USAGE_KEYS if by_usage[k])), "label": "Narrative groups"},
                {"value": f"{total:,}", "label": "Posts analysed"},
                {"value": top_use[0][0] if top_use else "—", "label": "Top use case"},
                {"value": f"{round(len(brand_rows) * 100 / total)}%" if total else "—", "label": f"Name {brand}" if brand else "Name the brand"},
            ],
        },
        "note": "",
        "summary": "",
        "keywords": [],
        "groups": [{"title": USAGE_TITLES[k], "key": k, "count": len(by_usage[k]), "points": []} for k in USAGE_KEYS],
    }

    # ── tab 2: five questions ─────────────────────────────────────────────
    by_q: dict[str, list[dict]] = {k: [] for k in QUESTION_KEYS}
    for a in articles:
        q = question_of(a, prepared)
        if q in by_q:
            by_q[q].append(a)
    columns = [{"q": QUESTIONS[k], "key": k, "count": len(by_q[k]), "points": []} for k in QUESTION_KEYS]
    columns.append({"q": QUESTIONS["reco"], "key": "reco", "reco": True, "count": 0, "points": []})
    observations = {
        "banner": {
            "eyebrow": "Landscape Observations",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": "5", "label": "Questions"},
                {"value": "4", "label": "Insight columns"},
                {"value": "1", "label": "Recommendation set"},
            ],
        },
        "note": "",
        "columns": columns,
    }

    # ── tab 3: current landscape ──────────────────────────────────────────
    platform_counts = Counter(cohorts.platform(a) for a in articles)
    platforms = [{"name": r["name"], "pct": r["pct"]} for r in _fold(platform_counts, MAX_PLATFORMS, others=None)]
    issuer_counts: Counter = Counter()
    for a in articles:
        for b in aggregate.brands_in(a, known):
            issuer_counts[b] += 1
    issuer_rows = _fold(issuer_counts, MAX_ISSUERS, others="Others")
    issuers = []
    for r in issuer_rows:
        row = {"name": r["name"], "pct": r["pct"]}
        if brand and r["name"].lower() == brand.lower():
            row["is_brand"] = True
        issuers.append(row)
    tracked = [n for n in issuer_counts]
    ranked_issuers = [k for k, _ in issuer_counts.most_common()]
    brand_rank = next((i + 1 for i, k in enumerate(ranked_issuers) if brand and k.lower() == brand.lower()), None)
    top_issuer = ranked_issuers[0] if ranked_issuers else None
    top_issuer_rows = [a for a in articles if top_issuer and top_issuer in aggregate.brands_in(a, known)]
    award_rows = [a for a in articles if _AWARD.search(f"{a.get('title') or ''} {a.get('summary') or ''}")]
    callouts = [{"key": "volume", "title": top_issuer or (brand or "Top brand"), "text": "", "count": len(top_issuer_rows)}] if top_issuer else []
    if award_rows:
        # Default title survives if the LLM only supplies the text; the card is
        # still dropped when no text is written (contract: omit if nothing found).
        callouts.append({"key": "award", "title": "Recognised in rankings and reviews", "text": "", "count": len(award_rows)})
    positive_brand = [a for a in brand_rows if a.get("sentiment") == "Positive"]
    landscape = {
        "banner": {
            "eyebrow": "Current Landscape",
            "headline": "",
            "sub": "",
            "stats": [
                {"value": f"{platforms[0]['pct']}%" if platforms else "—", "label": f"Share on {platforms[0]['name']}" if platforms else "Top platform"},
                {"value": f"{issuers[0]['pct']}%" if issuers else "—", "label": "Top brand share"},
                {"value": str(len(tracked)), "label": "Brands tracked"},
                {"value": f"#{brand_rank}" if brand_rank else "—", "label": "Rank among brands"},
            ],
        },
        "sec_title": "",
        "note": "",
        "platforms": platforms,
        "issuers": issuers,
        "callouts": callouts,
        "goods_title": "",
        "goods_lead": "",
        "goods": [],
        "positive_brand_posts": len(positive_brand),
    }

    # ── tab 4: audience outlook ───────────────────────────────────────────
    outlook_meta = prepared.get("outlook") or {}
    theme_titles = {t["key"]: t["title"] for t in outlook_meta.get("themes", [])}
    by_theme: dict[str, list[dict]] = {}
    for a in articles:
        k = outlook_of(a, prepared)
        if k:
            by_theme.setdefault(k, []).append(a)
    tagged_total = sum(len(v) for v in by_theme.values())
    theme_pct = cohorts.pct_rows({k: len(v) for k, v in by_theme.items()}) if tagged_total else []
    themes = [{"key": r["name"], "title": theme_titles.get(r["name"], r["name"].replace("-", " ").title()), "pct": r["pct"], "count": r["count"], "text": ""} for r in theme_pct]
    # Agg candidates the LLM may choose three headline stats from (values fixed).
    candidates = []
    if themes:
        candidates.append({"value": themes[0]["title"], "label": "Leading attitude"})
        candidates.append({"value": f"{themes[0]['pct']}%", "label": f"of outlook posts · {themes[0]['title']}"})
    split = {s["tone"]: s["pct"] for s in cohorts.sentiment_split(articles)}
    if split:
        candidates.append({"value": f"{split.get('pos', 0)}%", "label": "Positive posts"})
        candidates.append({"value": f"{split.get('neu', 0)}%", "label": "Neutral posts"})
    if platforms:
        candidates.append({"value": platforms[0]["name"], "label": "Main platform"})
    if brand:
        candidates.append({"value": f"{round(len(brand_rows) * 100 / total)}%" if total else "—", "label": f"Posts naming {brand}"})
    outlook = {
        "banner": {
            "eyebrow": "Audience Outlook",
            "headline": "",
            "sub": "",
            "stats": [{"value": str(len(themes)), "label": "Outlook themes"}, *candidates[:3]],
        },
        "sec_title": "",
        "note": "",
        "themes": themes,
        "tagged_posts": tagged_total,
        "stat_candidates": candidates,
    }

    named = {brand, *[r["name"] for r in issuers if r["name"] != "Others"]}
    named.discard("")
    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "category": "",
            "competitors": [k for k in known if k.lower() != (brand or "").lower()],
            "window": window,
            "total_mentions": total,
            "logos": brand_media.brand_logos(sorted(named), articles),
            "classification": {
                "labels": (prepared.get("labels") or {}).get("method"),
                "outlook": outlook_meta.get("method"),
                "outlook_themes": [{"key": t["key"], "title": t["title"], "raw": t["raw"][:12]} for t in outlook_meta.get("themes", [])],
                "usage_counts": {k: len(v) for k, v in by_usage.items()},
                "question_counts": {k: len(v) for k, v in by_q.items()},
            },
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Dominant Narratives · Landscape Analysis", f"Computed from {total:,} tagged posts"],
        "usage": usage,
        "observations": observations,
        "landscape": landscape,
        "outlook": outlook,
        "evidence": {
            "usage": {k: [_brief(a) for a in by_usage[k][:SAMPLE]] for k in USAGE_KEYS if by_usage[k]},
            "questions": {k: [_brief(a) for a in by_q[k][:SAMPLE]] for k in QUESTION_KEYS if by_q[k]},
            "top_issuer_posts": [_brief(a) for a in top_issuer_rows[:SAMPLE]],
            "award_posts": [_brief(a) for a in award_rows[:SAMPLE]],
            "positive_brand_posts": [_brief(a) for a in positive_brand[:12]],
            "brand_quotes": quotes.pick(brand_rows, needles=[brand] if brand else None, limit=3),
            "outlook": {t["key"]: [_brief(a) for a in by_theme.get(t["key"], [])[:SAMPLE]] for t in themes},
        },
    }
