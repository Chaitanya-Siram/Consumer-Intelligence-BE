"""Social Research storyboard — Social Research Tier 1.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-social-research.md
Screen:   Consumer-Intelligence-FE/src/screens/SocialResearchScreen.jsx

One lens, six sub-lens tabs (plus Overview and Appendix), all built from the
same tagged-article set. Behavioral themes and their volume trend reuse the
shared `taxonomy` module (LENS_KEY is in builder._NEEDS_TAXONOMY, so every
article already carries `theme_group` by the time this runs). Occasion,
cultural-space, motivation and brand-association labels come from
social_research_classify.prepare(). Every number here is computed from the
tagged articles; every prose field is left empty for narrative.py to fill.
"""

from collections import Counter

from .. import brand_media, cohorts, quotes, taxonomy, timeseries
from ..social_research_classify import (
    CULTURAL_SPACE_KEYS,
    CULTURAL_SPACE_TITLES,
    FEEL_KEYS,
    FEEL_TITLES,
    MOTIVATION_KEYS,
    MOTIVATION_TITLES,
    OCCASION_KEYS,
    OCCASION_TITLES,
    label_of,
    prepare,  # re-exported for builder._HAS_PREPARE
)
from . import brands as brand_metrics

TOP_ENTITIES = 6

LENS_KEY = "social_research"
__all__ = ["LENS_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "overview", "label": "Overview"},
    {"id": "brand_perception", "label": "Brand Perception & Relevance"},
    {"id": "competitive_cultural", "label": "Competitive & Cultural Landscape"},
    {"id": "occasions_behaviors", "label": "Occasions & Social Behaviors"},
    {"id": "motivations_identity", "label": "Social Motivations & Identity"},
    {"id": "cultural_spaces", "label": "Cultural Spaces"},
    {"id": "appendix", "label": "Appendix"},
]

PALETTE = ["#3b82f6", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#0891b2", "#ef4444"]
TOP_THEMES = 5
TOP_DRIVERS = 3
TOP_WHITESPACE = 3
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def _label_counts(rows: list[dict], prepared: dict, field: str, keys: tuple) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {k: [] for k in keys}
    for a in rows:
        v = label_of(a, prepared).get(field)
        if v in out:
            out[v].append(a)
    return out


def _trend_signals(rows: list[dict], groups: list[str]) -> tuple[list[dict], list[str]]:
    if not rows or not groups:
        return [], []
    all_keys, _, grain = timeseries.buckets(rows)
    days: list[str] | None = None
    signals = []
    for i, group in enumerate(groups):
        group_rows = [a for a in rows if a.get(taxonomy.GROUP_FIELD) == group]
        points, grain = timeseries.count_series(group_rows, grain, keys=all_keys)
        if days is None:
            days = [p["x"] for p in points]
        signals.append({"name": group, "color": PALETTE[i % len(PALETTE)], "share": [p["y"] for p in points]})
    return signals, days or []


def _trend_summary(signals: list[dict], days: list[str]) -> str:
    """One computed (not LLM) line: which series peaked, when, at what value."""
    if not signals or not days:
        return ""
    peak_val, peak_day, peak_name = -1, None, None
    for s in signals:
        for day, val in zip(days, s["share"]):
            if val > peak_val:
                peak_val, peak_day, peak_name = val, day, s["name"]
    return f"{peak_name} peaked in {peak_day} at {peak_val}" if peak_day else ""


def _sentiment_summary(sentiment: list[dict]) -> str:
    """One computed (not LLM) line summarizing a sentiment_split() result."""
    if not sentiment:
        return "No sentiment-scored posts"
    m = {r["tone"]: r for r in sentiment}
    return f"{m.get('pos', {}).get('pct', 0)}% positive · {m.get('neg', {}).get('pct', 0)}% negative · {m.get('neu', {}).get('pct', 0)}% neutral"


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    known = known_brands or ([brand] if brand else [])
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)
    brand_rows = cohorts.brand_articles(articles, brand, known)
    competitors = [k for k in known if k.lower() != (brand or "").lower()]
    competitor_rows = cohorts.competitor_articles(articles, brand, known)

    # ── Brand Perception & Relevance ────────────────────────────────────────
    theme_counts = taxonomy.group_counts(brand_rows)
    theme_ranked = [g for g in sorted(theme_counts, key=lambda k: (-theme_counts[k], k)) if g != taxonomy.OTHERS][:TOP_THEMES]
    theme_rows = cohorts.pct_rows({g: theme_counts[g] for g in theme_ranked}, order=theme_ranked)
    # `key` is the raw theme_group display name (what taxonomy.annotate wrote onto
    # each article), not a slug — comparing on it directly is how every other
    # taxonomy-based lens (emerging_issues, audience_priorities) matches rows back
    # to a group.
    themes = [{"key": r["name"], "title": r["name"], "pct": r["pct"], "count": r["count"], "text": ""} for r in theme_rows]

    trend_signals, trend_days = _trend_signals(brand_rows, theme_ranked)

    sentiment = cohorts.sentiment_split(brand_rows)
    sentiment_by_tone = {r["tone"]: r for r in sentiment}
    pos_rows = [a for a in brand_rows if a.get("sentiment") == "Positive"]
    neg_rows = [a for a in brand_rows if a.get("sentiment") == "Negative"]
    pos_theme_counts = taxonomy.group_counts(pos_rows)
    neg_theme_counts = taxonomy.group_counts(neg_rows)
    positive_drivers = [
        {"key": g, "title": g, "count": pos_theme_counts[g]}
        for g in sorted(pos_theme_counts, key=lambda k: (-pos_theme_counts[k], k))
        if g != taxonomy.OTHERS
    ][:TOP_DRIVERS]
    negative_drivers = [
        {"key": g, "title": g, "count": neg_theme_counts[g]}
        for g in sorted(neg_theme_counts, key=lambda k: (-neg_theme_counts[k], k))
        if g != taxonomy.OTHERS
    ][:TOP_DRIVERS]

    # Named entities (specific venues/events/platforms the posts actually name,
    # e.g. "Heineken Riverdeck") rather than a fixed category — free-text LLM
    # extraction, grouped by exact string match (no merge pass: distinct real
    # names are the point, not a small taxonomy).
    entity_rows: dict[str, list[dict]] = {}
    for a in brand_rows:
        name = label_of(a, prepared).get("entity")
        if name:
            entity_rows.setdefault(name, []).append(a)
    entity_ranked = sorted(entity_rows, key=lambda k: (-len(entity_rows[k]), k))[:TOP_ENTITIES]
    denom_entities = len(brand_rows) or 1
    entities = [
        {"key": name, "title": name, "count": len(entity_rows[name]), "pct": round(len(entity_rows[name]) * 100 / denom_entities)}
        for name in entity_ranked
    ]

    feel_counts = _label_counts(brand_rows, prepared, "feel", FEEL_KEYS)
    associations = {
        "entities": entities,
        "feel": [{"key": k, "title": FEEL_TITLES[k], "count": len(v)} for k, v in feel_counts.items() if v],
    }

    feel_ranked = sorted(((k, v) for k, v in feel_counts.items() if v), key=lambda kv: (-len(kv[1]), kv[0]))[:3]
    denom = len(brand_rows) or 1
    pillars = [
        {"key": k, "title": FEEL_TITLES[k], "pct": round(len(rows) * 100 / denom), "text": "", "quote": quotes.one(rows, prefer="Positive")}
        for k, rows in feel_ranked
    ]

    brand_perception = {
        "banner": {"eyebrow": "Brand Perception & Relevance", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Mentions"},
            {"value": f"{sentiment_by_tone.get('pos', {}).get('pct', 0)}%", "label": "Positive Sentiment"},
            {"value": themes[0]["title"] if themes else "—", "label": "Leading Theme"},
        ]},
        "note": "",
        "themes": themes,
        "trend": {"signals": trend_signals, "days": trend_days},
        "trend_summary": _trend_summary(trend_signals, trend_days),
        "sentiment": sentiment,
        "sentiment_summary": _sentiment_summary(sentiment),
        "drivers": {"positive": positive_drivers, "negative": negative_drivers},
        "associations": associations,
        "pillars": pillars,
        "quotes": quotes.pick(brand_rows, limit=quotes.TOP_POSTS),
    }

    # ── Competitive & Cultural Landscape ────────────────────────────────────
    share_of_voice = brand_metrics.share_of_voice(articles, known) if competitors else []
    net_sentiment_league = brand_metrics.net_sentiment_league(articles, known) if competitors else []
    for row in share_of_voice:
        row["is_brand"] = bool(brand) and row["brand"].lower() == brand.lower()
    for row in net_sentiment_league:
        row["is_brand"] = bool(brand) and row["brand"].lower() == brand.lower()

    culture_associations = []
    for comp, rows in competitor_rows.items():
        counts = taxonomy.group_counts(rows)
        ranked = [g for g in sorted(counts, key=lambda k: (-counts[k], k)) if g != taxonomy.OTHERS]
        if not ranked:
            continue
        top = ranked[0]
        top_rows = [a for a in rows if a.get(taxonomy.GROUP_FIELD) == top]
        culture_associations.append({"brand": comp, "title": top, "pct": round(len(top_rows) * 100 / (len(rows) or 1)), "text": ""})
    culture_associations.sort(key=lambda r: -r["pct"])

    # Per-competitor breakdown (theme trend + theme distribution), matching
    # the deck's repeated per-brand slide pattern. Sentiment is included for
    # visual consistency with Heineken's own section, but under this
    # platform's aspect-based tagging most competitor-only posts score
    # Neutral — sentiment is scored relative to the tracked brand, not
    # whichever brand a post actually names — so a competitor's split
    # skewing Neutral reflects that methodology, not a data gap.
    competitor_breakdown = []
    for comp in sorted(competitor_rows, key=lambda c: -len(competitor_rows[c]))[:4]:
        rows = competitor_rows[comp]
        counts = taxonomy.group_counts(rows)
        ranked_c = [g for g in sorted(counts, key=lambda k: (-counts[k], k)) if g != taxonomy.OTHERS][:TOP_THEMES]
        theme_rows_c = cohorts.pct_rows({g: counts[g] for g in ranked_c}, order=ranked_c)
        trend_c, days_c = _trend_signals(rows, ranked_c)
        sentiment_c = cohorts.sentiment_split(rows)

        # One computed (not LLM) one-line summary per chart, so each chart is
        # readable on its own rather than relying only on the card-level text.
        themes_summary = f"{theme_rows_c[0]['name']} leads at {theme_rows_c[0]['pct']}%" if theme_rows_c else "No dominant theme"
        trend_summary = _trend_summary(trend_c, days_c)
        sentiment_summary = _sentiment_summary(sentiment_c)

        competitor_breakdown.append({
            "brand": comp,
            "mentions": len(rows),
            "themes": [{"name": r["name"], "pct": r["pct"]} for r in theme_rows_c],
            "themes_summary": themes_summary,
            "trend": {"signals": trend_c, "days": days_c},
            "trend_summary": trend_summary,
            "sentiment": sentiment_c,
            "sentiment_summary": sentiment_summary,
            "text": "",
        })

    # Whitespace candidates: theme groups where the competitor set out-indexes
    # the subject brand — a real, computed gap, not an invented one.
    all_comp_rows = [a for rows in competitor_rows.values() for a in rows]
    comp_theme_counts = taxonomy.group_counts(all_comp_rows)
    comp_denom = len(all_comp_rows) or 1
    brand_denom = len(brand_rows) or 1
    gaps = []
    for g in set(comp_theme_counts) | set(theme_counts):
        if g == taxonomy.OTHERS:
            continue
        comp_share = comp_theme_counts.get(g, 0) * 100 / comp_denom
        brand_share = theme_counts.get(g, 0) * 100 / brand_denom
        gap = comp_share - brand_share
        if gap > 0:
            gaps.append({"topic": g, "competitor_share": round(comp_share), "brand_share": round(brand_share), "gap": round(gap), "why": "", "gap_text": "", "entry": ""})
    gaps.sort(key=lambda r: -r["gap"])
    whitespace = gaps[:TOP_WHITESPACE]

    share_of_voice_summary = f"{share_of_voice[0]['brand']} leads with {share_of_voice[0]['value']}% share of voice" if share_of_voice else ""
    net_sentiment_summary = (
        f"{net_sentiment_league[0]['brand']} leads net sentiment at {net_sentiment_league[0]['value']:+.0f}" if net_sentiment_league else ""
    )

    competitive_cultural = {
        "banner": {"eyebrow": "Competitive & Cultural Landscape", "headline": "", "sub": "", "stats": [
            {"value": str(len(competitors)), "label": "Competitors Benchmarked"},
            {"value": f"{share_of_voice[0]['value']}%" if share_of_voice else "—", "label": "Leading Share of Voice"},
        ]},
        "note": "",
        "share_of_voice": share_of_voice,
        "share_of_voice_summary": share_of_voice_summary,
        "net_sentiment": net_sentiment_league,
        "net_sentiment_summary": net_sentiment_summary,
        "culture_associations": culture_associations[:4],
        "competitor_breakdown": competitor_breakdown,
        "whitespace": whitespace,
        "quotes": quotes.pick(all_comp_rows, limit=quotes.TOP_POSTS),
    }

    # ── Occasions & Social Behaviors ─────────────────────────────────────────
    occasion_counts = _label_counts(brand_rows, prepared, "occasion", OCCASION_KEYS)
    occasions = [
        {"key": k, "title": OCCASION_TITLES[k], "pct": round(len(v) * 100 / denom), "count": len(v), "text": ""}
        for k, v in occasion_counts.items() if v
    ]
    occasions.sort(key=lambda r: -r["count"])
    occasion_quotes = quotes.pick([a for v in occasion_counts.values() for a in v], limit=4)

    occasions_behaviors = {
        "banner": {"eyebrow": "Occasions & Social Behaviors", "headline": "", "sub": "", "stats": [
            {"value": f"{occasions[0]['pct']}%" if occasions else "—", "label": f"{occasions[0]['title']}" if occasions else "Leading Occasion"},
        ]},
        "note": "",
        "occasions": occasions,
        "quotes": occasion_quotes,
    }

    # ── Social Motivations & Identity ───────────────────────────────────────
    motivation_counts = _label_counts(brand_rows, prepared, "motivation", MOTIVATION_KEYS)
    motivations = [
        {"key": k, "title": MOTIVATION_TITLES[k], "pct": round(len(v) * 100 / denom), "count": len(v), "text": "", "opportunity": ""}
        for k, v in motivation_counts.items() if v
    ]
    motivations.sort(key=lambda r: -r["count"])

    motivations_identity = {
        "banner": {"eyebrow": "Social Motivations & Identity", "headline": "", "sub": "", "stats": [
            {"value": str(len(motivations)), "label": "Motivations Identified"},
        ]},
        "note": "",
        "motivations": motivations,
        "quotes": quotes.pick([a for v in motivation_counts.values() for a in v], limit=quotes.TOP_POSTS),
    }

    # ── Cultural Spaces ──────────────────────────────────────────────────────
    space_counts = _label_counts(brand_rows, prepared, "cultural_space", CULTURAL_SPACE_KEYS)
    spaces = [
        {"key": k, "title": CULTURAL_SPACE_TITLES[k], "pct": round(len(v) * 100 / denom), "count": len(v), "text": ""}
        for k, v in space_counts.items() if v
    ]
    spaces.sort(key=lambda r: -r["count"])

    cultural_spaces = {
        "banner": {"eyebrow": "Cultural Spaces", "headline": "", "sub": "", "stats": [
            {"value": spaces[0]["title"] if spaces else "—", "label": "Leading Cultural Space"},
        ]},
        "note": "",
        "spaces": spaces,
        "quotes": quotes.pick([a for v in space_counts.values() for a in v], limit=quotes.TOP_POSTS),
    }

    # ── Overview (aggregates the above; no independent computation) ─────────
    top_theme = themes[0] if themes else None
    # By share of voice, not net sentiment: aspect-based sentiment is measured
    # against the subject brand specifically, so every competitor's net
    # sentiment defaults toward neutral regardless of their own conversation's
    # tone — share of voice is the metric that actually distinguishes them.
    top_threat = next((row for row in share_of_voice if not row.get("is_brand")), None)
    overview = {
        "banner": {"eyebrow": "Overview", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Mentions"},
            {"value": f"{sentiment_by_tone.get('pos', {}).get('pct', 0) - sentiment_by_tone.get('neg', {}).get('pct', 0):+d}" if sentiment else "—", "label": "Net Sentiment"},
            {"value": top_theme["title"] if top_theme else "—", "label": "Leading Theme"},
            {"value": top_threat["brand"] if top_threat else "—", "label": "Closest Competitor"},
        ]},
        "note": "",
    }

    # ── Appendix: real citations gathered from the sections above ───────────
    def _sources(rows: list[dict], limit: int = 4) -> list[dict]:
        return [{"title": quotes.truncate(q["text"], 90), "source": q["source"], "url": q["url"]} for q in quotes.pick(rows, limit=limit)]

    appendix = {
        "banner": {"eyebrow": "Appendix", "headline": "Source Citations", "sub": "", "stats": []},
        "groups": [
            {"title": "Themes", "sources": _sources([a for a in brand_rows if a.get(taxonomy.GROUP_FIELD) in theme_ranked])},
            {"title": "Positive Drivers", "sources": _sources(pos_rows)},
            {"title": "Negative Drivers", "sources": _sources(neg_rows)},
            {"title": "Whitespaces", "sources": _sources(all_comp_rows)},
        ],
    }

    named = {brand, *competitors}
    named.discard("")
    named.update(cohorts.platform(a) for a in articles)
    labels_meta = prepared.get("labels") or {}
    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": competitors,
            "window": window,
            "total_mentions": total,
            "brand_tagged": len(brand_rows),
            "logos": brand_media.brand_logos(sorted(named), articles),
            "method": {
                "themes": "taxonomy.canonicalize theme groups over the brand's own posts, ranked by volume",
                "trend": "per-theme-group post counts bucketed over the capture window (weekly/monthly)",
                "sentiment_drivers": "top theme groups within the positive-only and negative-only post subsets",
                "share_of_voice": "brands.share_of_voice over all tagged posts",
                "whitespace": "theme groups where the competitor set's share exceeds the brand's own share, ranked by gap size",
            },
            "classification": {"labels": labels_meta.get("method")},
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Social Research · Consumer Intelligence", f"Computed from {total:,} tagged posts"],
        "overview": overview,
        "brand_perception": brand_perception,
        "competitive_cultural": competitive_cultural,
        "occasions_behaviors": occasions_behaviors,
        "motivations_identity": motivations_identity,
        "cultural_spaces": cultural_spaces,
        "appendix": appendix,
        "evidence": {
            "themes": {t["title"]: [_brief(a) for a in brand_rows if a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in themes},
            "positive_drivers": {d["title"]: [_brief(a) for a in pos_rows if a.get(taxonomy.GROUP_FIELD) == d["key"]][:SAMPLE] for d in positive_drivers},
            "negative_drivers": {d["title"]: [_brief(a) for a in neg_rows if a.get(taxonomy.GROUP_FIELD) == d["key"]][:SAMPLE] for d in negative_drivers},
            "competitive": {comp: [_brief(a) for a in rows][:SAMPLE] for comp, rows in competitor_rows.items()},
            "occasions": {k: [_brief(a) for a in v][:SAMPLE] for k, v in occasion_counts.items() if v},
            "motivations": {k: [_brief(a) for a in v][:SAMPLE] for k, v in motivation_counts.items() if v},
            "cultural_spaces": {k: [_brief(a) for a in v][:SAMPLE] for k, v in space_counts.items() if v},
        },
    }
