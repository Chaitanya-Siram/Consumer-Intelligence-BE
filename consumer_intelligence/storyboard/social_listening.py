"""Social Listening storyboard — Social Listening Tier 1.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-social-listening.md
Screen:   Consumer-Intelligence-FE/src/screens/SocialListeningScreen.jsx

Single-brand lens (no competitor set, unlike Social Research): tracks how a
brand's own recurring conversation "pillars" get used, where, and in what
tone. The source deck names four specific expressions for one client
("Let's Cook", "No Notes", "Pop Off", "The Original") — rather than hardcode
a client's phrase list, this module treats "pillars" as the top computed
`taxonomy` theme groups (the same LLM-canonicalised system Social Research's
"themes" already use), so the lens works for any brand/dataset. Occasion,
conversation-setting and literal/figurative labels come from
social_listening_classify.prepare(). Every number here is computed from the
tagged articles; every prose field is left empty for narrative.py to fill.
"""

from .. import brand_media, cohorts, quotes, taxonomy, timeseries
from ..social_listening_classify import (
    CONVERSATION_SETTING_KEYS,
    CONVERSATION_SETTING_TITLES,
    OCCASION_TYPE_KEYS,
    OCCASION_TYPE_TITLES,
    label_of,
    prepare,  # re-exported for builder._HAS_PREPARE
)

LENS_KEY = "social_listening"
__all__ = ["LENS_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "overview", "label": "Overview"},
    {"id": "overall_expressions", "label": "Overall Expressions"},
    {"id": "conversation_settings", "label": "Primary Conversation Settings"},
    {"id": "occasions_usage", "label": "Usage Across Celebration & Social Occasions"},
    {"id": "expression_deep_dive", "label": "Expression Deep Dive"},
    {"id": "appendix", "label": "Appendix"},
]

PALETTE = ["#3b82f6", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#0891b2", "#ef4444"]
TOP_PILLARS = 4
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


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


def _pillar_rows(articles: list[dict], pillar: str) -> list[dict]:
    return [a for a in articles if a.get(taxonomy.GROUP_FIELD) == pillar]


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


def _leader_summary(rows: list[dict], noun: str) -> str:
    """One computed line naming the top row of a cohorts.pct_rows() list."""
    return f"{rows[0]['name']} leads at {rows[0]['pct']}%" if rows else f"No dominant {noun}"


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)

    theme_counts = taxonomy.group_counts(articles)
    pillar_names = [g for g in sorted(theme_counts, key=lambda k: (-theme_counts[k], k)) if g != taxonomy.OTHERS][:TOP_PILLARS]
    pillar_rows_map = {p: _pillar_rows(articles, p) for p in pillar_names}
    denom = total or 1

    pillars = [{"key": p, "title": p, "pct": round(len(pillar_rows_map[p]) * 100 / denom), "count": len(pillar_rows_map[p])} for p in pillar_names]
    trend_signals, trend_days = _trend_signals(articles, pillar_names)

    # ── Overall Expressions ───────────────────────────────────────────────
    overall_breakdown = []
    for p in pillar_names:
        rows = pillar_rows_map[p]
        platform_counts: dict[str, int] = {}
        for a in rows:
            plat = cohorts.platform(a)
            platform_counts[plat] = platform_counts.get(plat, 0) + 1
        platform_rows = cohorts.pct_rows(platform_counts)[:4]
        overall_breakdown.append({
            "pillar": p,
            "mentions": len(rows),
            "platforms": [{"name": r["name"], "pct": r["pct"]} for r in platform_rows],
            "text": "",
        })

    overall_expressions = {
        "banner": {"eyebrow": "Overall Expressions", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Mentions"},
            {"value": pillars[0]["title"] if pillars else "—", "label": "Leading Pillar"},
        ]},
        "note": "",
        "pillars": pillars,
        "trend": {"signals": trend_signals, "days": trend_days},
        "trend_summary": _trend_summary(trend_signals, trend_days),
        "pillar_breakdown": overall_breakdown,
        "quotes": quotes.pick([a for rows in pillar_rows_map.values() for a in rows], limit=quotes.TOP_POSTS),
    }

    # ── Primary Conversation Settings for Each Pillar ───────────────────────
    settings_breakdown = []
    for p in pillar_names:
        rows = pillar_rows_map[p]
        setting_counts: dict[str, int] = {k: 0 for k in CONVERSATION_SETTING_KEYS}
        for a in rows:
            s = label_of(a, prepared).get("conversation_setting")
            if s in setting_counts:
                setting_counts[s] += 1
        setting_rows = [r for r in cohorts.pct_rows({k: v for k, v in setting_counts.items() if v}) if r["pct"] > 0]
        settings_breakdown.append({
            "pillar": p,
            "mentions": len(rows),
            "settings": [{"name": CONVERSATION_SETTING_TITLES[r["name"]], "pct": r["pct"]} for r in setting_rows],
            "text": "",
        })

    conversation_settings = {
        "banner": {"eyebrow": "Primary Conversation Settings", "headline": "", "sub": "", "stats": [
            {"value": str(len(pillar_names)), "label": "Pillars Analyzed"},
        ]},
        "note": "",
        "pillar_breakdown": settings_breakdown,
    }

    # ── Usage Across Celebration and Social Occasions ───────────────────────
    occasions_breakdown = []
    for p in pillar_names:
        rows = pillar_rows_map[p]
        relevant = [a for a in rows if label_of(a, prepared).get("occasion_relevant")]
        rel_pct = round(len(relevant) * 100 / (len(rows) or 1))
        type_counts: dict[str, int] = {k: 0 for k in OCCASION_TYPE_KEYS}
        for a in relevant:
            t = label_of(a, prepared).get("occasion_type")
            if t in type_counts:
                type_counts[t] += 1
        type_rows = [r for r in cohorts.pct_rows({k: v for k, v in type_counts.items() if v}) if r["pct"] > 0]
        occasions_breakdown.append({
            "pillar": p,
            "mentions": len(rows),
            "relevant_pct": rel_pct,
            "occasion_types": [{"name": OCCASION_TYPE_TITLES[r["name"]], "pct": r["pct"]} for r in type_rows],
            "text": "",
        })

    occasions_usage = {
        "banner": {"eyebrow": "Usage Across Celebration & Social Occasions", "headline": "", "sub": "", "stats": [
            {"value": f"{max((o['relevant_pct'] for o in occasions_breakdown), default=0)}%", "label": "Peak Occasion Relevance"},
        ]},
        "note": "",
        "pillar_breakdown": occasions_breakdown,
        "quotes": quotes.pick([a for p in pillar_names for a in pillar_rows_map[p] if label_of(a, prepared).get("occasion_relevant")], limit=quotes.TOP_POSTS),
    }

    # ── Expression Deep Dive (focus: the top pillar by volume) ──────────────
    top_pillar = pillar_names[0] if pillar_names else None
    top_rows = pillar_rows_map.get(top_pillar, []) if top_pillar else []
    lit_counts: dict[str, int] = {"literal": 0, "figurative": 0}
    for a in top_rows:
        v = label_of(a, prepared).get("literal_vs_figurative")
        if v in lit_counts:
            lit_counts[v] += 1
    lit_rows = cohorts.pct_rows(lit_counts) if any(lit_counts.values()) else []
    figurative_rows = [a for a in top_rows if label_of(a, prepared).get("literal_vs_figurative") == "figurative"]
    fig_setting_counts: dict[str, int] = {}
    for a in figurative_rows:
        s = label_of(a, prepared).get("conversation_setting")
        if s and s != "none":
            fig_setting_counts[s] = fig_setting_counts.get(s, 0) + 1
    fig_setting_rows = cohorts.pct_rows(fig_setting_counts)[:5] if fig_setting_counts else []

    literal_vs_figurative = [{"name": r["name"].title(), "pct": r["pct"]} for r in lit_rows]
    figurative_settings = [{"name": CONVERSATION_SETTING_TITLES.get(r["name"], r["name"]), "pct": r["pct"]} for r in fig_setting_rows]
    deep_dive_sentiment = cohorts.sentiment_split(top_rows)
    literal_pct = next((r["pct"] for r in literal_vs_figurative if r["name"] == "Literal"), None)
    literal_vs_figurative_summary = (
        f"{literal_pct}% literal usage" + (f", {100 - literal_pct}% figurative" if literal_pct is not None and literal_pct < 100 else "")
        if literal_pct is not None else "No literal/figurative signal"
    )

    expression_deep_dive = {
        "banner": {"eyebrow": "Expression Deep Dive", "headline": "", "sub": "", "stats": [
            {"value": top_pillar or "—", "label": "Focus Pillar"},
            {"value": str(len(top_rows)), "label": "Mentions"},
        ]},
        "note": "",
        "pillar": top_pillar,
        "literal_vs_figurative": literal_vs_figurative,
        "literal_vs_figurative_summary": literal_vs_figurative_summary,
        "figurative_settings": figurative_settings,
        "figurative_settings_summary": _leader_summary(figurative_settings, "conversation setting"),
        "sentiment": deep_dive_sentiment,
        "sentiment_summary": _sentiment_summary(deep_dive_sentiment),
        "quotes": quotes.pick(top_rows, limit=quotes.TOP_POSTS),
    }

    # ── Overview ─────────────────────────────────────────────────────────
    sentiment_all = cohorts.sentiment_split(articles)
    sentiment_by_tone = {r["tone"]: r for r in sentiment_all}
    overview = {
        "banner": {"eyebrow": "Overview", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Mentions"},
            {"value": f"{sentiment_by_tone.get('pos', {}).get('pct', 0) - sentiment_by_tone.get('neg', {}).get('pct', 0):+d}" if sentiment_all else "—", "label": "Net Sentiment"},
            {"value": pillars[0]["title"] if pillars else "—", "label": "Leading Pillar"},
            {"value": top_pillar or "—", "label": "Deep-Dive Focus"},
        ]},
        "note": "",
    }

    # ── Appendix ─────────────────────────────────────────────────────────
    def _sources(rows: list[dict], limit: int = 4) -> list[dict]:
        return [{"title": quotes.truncate(q["text"], 90), "source": q["source"], "url": q["url"]} for q in quotes.pick(rows, limit=limit)]

    appendix = {
        "banner": {"eyebrow": "Appendix", "headline": "Source Citations", "sub": "", "stats": []},
        "groups": [
            {"title": "Overall Expressions", "sources": _sources([a for rows in pillar_rows_map.values() for a in rows])},
            {"title": "Conversation Settings", "sources": _sources([a for a in articles if label_of(a, prepared).get("conversation_setting") != "none"])},
            {"title": "Occasions", "sources": _sources([a for a in articles if label_of(a, prepared).get("occasion_relevant")])},
            {"title": "Expression Deep Dive", "sources": _sources(top_rows)},
        ],
    }

    named = {brand} if brand else set()
    named.update(cohorts.platform(a) for a in articles)
    labels_meta = prepared.get("labels") or {}
    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "window": window,
            "total_mentions": total,
            "logos": brand_media.brand_logos(sorted(named), articles),
            "method": {
                "pillars": "taxonomy.canonicalize theme groups over all posts, ranked by volume — this dataset's top recurring conversation pillars",
                "conversation_settings": "LLM-classified context bucket per post, broken down within each pillar's own posts",
                "occasions": "share of each pillar's posts tied to a specific occasion, broken down by occasion type",
                "deep_dive": "the highest-volume pillar, split by literal vs. figurative usage and re-broken-down by conversation setting within figurative-only posts",
            },
            "classification": {"labels": labels_meta.get("method")},
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Social Listening · Consumer Intelligence", f"Computed from {total:,} tagged posts"],
        "overview": overview,
        "overall_expressions": overall_expressions,
        "conversation_settings": conversation_settings,
        "occasions_usage": occasions_usage,
        "expression_deep_dive": expression_deep_dive,
        "appendix": appendix,
        "evidence": {
            "pillars": {p: [_brief(a) for a in pillar_rows_map[p]][:SAMPLE] for p in pillar_names},
        },
    }
