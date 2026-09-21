"""Social Audit storyboard — Social Audit Tier 1.

Contract: Consumer-Intelligence-FE/docs/ci-lens-contract-social-audit.md
Screen:   Consumer-Intelligence-FE/src/screens/SocialAuditScreen.jsx

Single-brand lens (no competitor set). The source deck audits three fixed
research pillars ("Kids, Parenting & Devices", "...& AI", "...& Screentime")
— social_audit_classify.prepare() routes every post to at most one. Within
each pillar, recurring themes are the generic `taxonomy` theme groups scoped
to that pillar's own rows (the same "computed, not hardcoded" pattern
Social Research uses per-competitor). Every number here is computed from
the tagged articles; every prose field is left empty for narrative.py to
fill.
"""

from .. import aggregate, brand_media, cohorts, quotes, taxonomy, timeseries
from ..social_audit_classify import PILLAR_KEYS, PILLAR_TITLES, label_of, prepare  # noqa: F401  (prepare re-exported for builder._HAS_PREPARE)

LENS_KEY = "social_audit"
__all__ = ["LENS_KEY", "build_storyboard", "prepare"]

TABS = [
    {"id": "overview", "label": "Overview"},
    {"id": "conversation_landscape", "label": "Conversation Landscape"},
    {"id": "devices", "label": "Kids, Parenting & Devices"},
    {"id": "ai", "label": "Kids, Parenting & AI"},
    {"id": "screentime", "label": "Kids, Parenting & Screentime"},
    {"id": "additional_insights", "label": "Additional Insights"},
    {"id": "appendix", "label": "Appendix"},
]

THEME_PALETTE = ["#3b82f6", "#10b981", "#f59e0b", "#ec4899", "#8b5cf6", "#0891b2", "#ef4444"]
PILLAR_COLORS = {"devices": "#3b82f6", "ai": "#8b5cf6", "screentime": "#f59e0b"}
TOP_THEMES = 6
TOP_ENGAGING = 4
TOP_INFLUENCERS = 4
SAMPLE = 8


def _brief(a: dict, chars: int = 220) -> dict:
    return {"title": str(a.get("title") or "")[:140], "summary": str(a.get("summary") or a.get("content") or "")[:chars], "sentiment": a.get("sentiment"), "theme": a.get("theme")}


def _pillar_rows(articles: list[dict], prepared: dict, pillar: str) -> list[dict]:
    return [a for a in articles if label_of(a, prepared).get("pillar") == pillar]


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
        signals.append({"name": group, "color": THEME_PALETTE[i % len(THEME_PALETTE)], "share": [p["y"] for p in points]})
    return signals, days or []


def _pillar_trend_signals(articles: list[dict], prepared: dict) -> tuple[list[dict], list[str]]:
    """Same shape as _trend_signals, but one series per research pillar."""
    if not articles:
        return [], []
    all_keys, _, grain = timeseries.buckets(articles)
    days: list[str] | None = None
    signals = []
    for pillar in PILLAR_KEYS:
        rows = _pillar_rows(articles, prepared, pillar)
        points, grain = timeseries.count_series(rows, grain, keys=all_keys)
        if days is None:
            days = [p["x"] for p in points]
        signals.append({"name": PILLAR_TITLES[pillar], "color": PILLAR_COLORS[pillar], "share": [p["y"] for p in points]})
    return signals, days or []


def _trend_summary(signals: list[dict], days: list[str]) -> str:
    if not signals or not days:
        return ""
    peak_val, peak_day, peak_name = -1, None, None
    for s in signals:
        for day, val in zip(days, s["share"]):
            if val > peak_val:
                peak_val, peak_day, peak_name = val, day, s["name"]
    return f"{peak_name} peaked in {peak_day} at {peak_val}" if peak_day else ""


def _sentiment_summary(sentiment: list[dict]) -> str:
    if not sentiment:
        return "No sentiment-scored posts"
    m = {r["tone"]: r for r in sentiment}
    return f"{m.get('pos', {}).get('pct', 0)}% positive · {m.get('neg', {}).get('pct', 0)}% negative · {m.get('neu', {}).get('pct', 0)}% neutral"


def _leader_summary(rows: list[dict], noun: str) -> str:
    return f"{rows[0]['name']} leads at {rows[0]['pct']}%" if rows else f"No dominant {noun}"


def _pillar_tab(articles: list[dict], prepared: dict, pillar: str) -> dict:
    rows = _pillar_rows(articles, prepared, pillar)
    theme_counts = taxonomy.group_counts(rows)
    theme_ranked = [g for g in sorted(theme_counts, key=lambda k: (-theme_counts[k], k)) if g != taxonomy.OTHERS][:TOP_THEMES]
    theme_rows = cohorts.pct_rows({g: theme_counts[g] for g in theme_ranked}, order=theme_ranked)
    themes = [{"key": r["name"], "title": r["name"], "pct": r["pct"], "count": r["count"], "text": ""} for r in theme_rows]
    trend_signals, trend_days = _trend_signals(rows, theme_ranked)
    sentiment = cohorts.sentiment_split(rows)

    return {
        "banner": {"eyebrow": PILLAR_TITLES[pillar], "headline": "", "sub": "", "stats": [
            {"value": str(len(rows)), "label": "Mentions"},
            {"value": themes[0]["title"] if themes else "—", "label": "Leading Theme"},
        ]},
        "note": "",
        "themes": themes,
        "themes_summary": _leader_summary(theme_rows, "theme"),
        "trend": {"signals": trend_signals, "days": trend_days},
        "trend_summary": _trend_summary(trend_signals, trend_days),
        "sentiment": sentiment,
        "sentiment_summary": _sentiment_summary(sentiment),
        "sentiment_positive_text": "",
        "sentiment_negative_text": "",
        "quotes": quotes.pick(rows, limit=4),
    }


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str], prepared: dict | None = None) -> dict:
    prepared = prepared or {}
    total = len(articles)
    window = timeseries.window_label(articles)

    pillar_rows_map = {p: _pillar_rows(articles, prepared, p) for p in PILLAR_KEYS}
    pillar_counts = {p: len(rows) for p, rows in pillar_rows_map.items()}
    pillar_rows = cohorts.pct_rows({PILLAR_TITLES[p]: pillar_counts[p] for p in PILLAR_KEYS})
    leading_pillar = pillar_rows[0]["name"] if pillar_rows else "—"

    pillar_trend_signals, pillar_trend_days = _pillar_trend_signals(articles, prepared)

    # ── Overview ─────────────────────────────────────────────────────────
    sentiment_all = cohorts.sentiment_split(articles)
    sentiment_by_tone = {r["tone"]: r for r in sentiment_all}
    overview = {
        "banner": {"eyebrow": "Overview", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Mentions"},
            {"value": f"{sentiment_by_tone.get('pos', {}).get('pct', 0) - sentiment_by_tone.get('neg', {}).get('pct', 0):+d}" if sentiment_all else "—", "label": "Net Sentiment"},
            {"value": leading_pillar, "label": "Leading Pillar"},
            {"value": str(len(PILLAR_KEYS)), "label": "Pillars Audited"},
        ]},
        "note": "",
    }

    # ── Conversation Landscape ───────────────────────────────────────────
    conversation_landscape = {
        "banner": {"eyebrow": "Conversation Landscape", "headline": "", "sub": "", "stats": [
            {"value": str(total), "label": "Total Mentions"},
            {"value": leading_pillar, "label": "Leading Pillar"},
        ]},
        "note": "",
        "pillar_split": [{"key": p, "name": PILLAR_TITLES[p], "pct": next((r["pct"] for r in pillar_rows if r["name"] == PILLAR_TITLES[p]), 0), "count": pillar_counts[p], "text": ""} for p in PILLAR_KEYS],
        "trend": {"signals": pillar_trend_signals, "days": pillar_trend_days},
        "trend_summary": _trend_summary(pillar_trend_signals, pillar_trend_days),
        "quotes": quotes.pick(articles, limit=4),
    }

    # ── Pillar tabs ──────────────────────────────────────────────────────
    devices = _pillar_tab(articles, prepared, "devices")
    ai = _pillar_tab(articles, prepared, "ai")
    screentime = _pillar_tab(articles, prepared, "screentime")

    # ── Additional Insights ──────────────────────────────────────────────
    # One quote per top-engagement article, in engagement order — quotes.one()
    # is scoped to a single article's own rows so the engagement number shown
    # always matches the quote it's attached to.
    top_engaging = []
    for a in sorted(articles, key=aggregate.engagement, reverse=True):
        q = quotes.one([a])
        if q:
            top_engaging.append({**q, "engagement": round(aggregate.engagement(a))})
        if len(top_engaging) >= TOP_ENGAGING:
            break

    # Ranked by engagement, not aggregate.reach(): the tagging pipeline
    # unconditionally overwrites every article's `reach` with a domain-level
    # traffic estimate (file_helpers/similare_web_reach.py get_reach()), so
    # it reflects the article's host site, not the individual author.
    by_author: dict[str, list[dict]] = {}
    for a in articles:
        name = str(a.get("author") or "").strip()
        if name:
            by_author.setdefault(name, []).append(a)
    influencer_ranked = sorted(by_author, key=lambda n: -max(aggregate.engagement(a) for a in by_author[n]))[:TOP_INFLUENCERS]
    top_influencers = []
    for name in influencer_ranked:
        rows = by_author[name]
        best = max(rows, key=aggregate.engagement)
        q = quotes.one(rows)
        top_influencers.append({
            "author": name,
            "platform": cohorts.platform(best),
            "engagement": round(aggregate.engagement(best)),
            "mentions": len(rows),
            "quote": q,
        })

    # Whitespace candidates: the smallest (lowest-volume) real theme within
    # each pillar — an emerging/underserved signal, computed per-pillar
    # rather than a fixed, brand-specific opportunity list.
    whitespaces = []
    for pillar, tab in (("devices", devices), ("ai", ai), ("screentime", screentime)):
        if tab["themes"]:
            smallest = min(tab["themes"], key=lambda t: t["count"])
            whitespaces.append({"pillar": PILLAR_TITLES[pillar], "theme": smallest["title"], "pct": smallest["pct"], "title": "", "text": ""})

    additional_insights = {
        "banner": {"eyebrow": "Additional Insights", "headline": "", "sub": "", "stats": [
            {"value": str(len(top_influencers)), "label": "Voices Tracked"},
        ]},
        "note": "",
        "top_engaging": top_engaging,
        # top_engaging's dicts plus every influencer's quote dict, under the
        # field name builder.py's verbatim-evidence resolver looks for
        # (`_VERBATIM_SECTIONS`) — a real embed/screenshot mutates each dict
        # in place, visible wherever that same dict object is referenced.
        "quotes": top_engaging + [i["quote"] for i in top_influencers if i.get("quote")],
        "top_influencers": top_influencers,
        "whitespaces": whitespaces,
    }

    # ── Appendix ─────────────────────────────────────────────────────────
    def _sources(rows: list[dict], limit: int = 4) -> list[dict]:
        return [{"title": quotes.truncate(q["text"], 90), "source": q["source"], "url": q["url"]} for q in quotes.pick(rows, limit=limit)]

    appendix = {
        "banner": {"eyebrow": "Appendix", "headline": "Source Citations", "sub": "", "stats": []},
        "groups": [
            {"title": PILLAR_TITLES["devices"], "sources": _sources(pillar_rows_map["devices"])},
            {"title": PILLAR_TITLES["ai"], "sources": _sources(pillar_rows_map["ai"])},
            {"title": PILLAR_TITLES["screentime"], "sources": _sources(pillar_rows_map["screentime"])},
            {"title": "Additional Insights", "sources": [{"title": quotes.truncate(e["text"], 90), "source": e["source"], "url": e["url"]} for e in top_engaging]},
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
                "pillars": "LLM-routed to one of three fixed research pillars (Devices, AI, Screentime) per post",
                "themes": "taxonomy.canonicalize theme groups scoped to each pillar's own posts, ranked by volume",
                "trend": "per-pillar (or per-theme) post counts bucketed over the capture window",
                "top_engaging": "highest-engagement posts across the whole dataset",
                "top_influencers": "authors ranked by their single highest-engagement post",
                "whitespaces": "the lowest-volume real theme within each pillar — an emerging, underserved signal",
            },
            "classification": {"labels": labels_meta.get("method")},
        },
        "tabs": [dict(t) for t in TABS],
        "footer": ["Social Audit · Consumer Intelligence", f"Computed from {total:,} tagged posts"],
        "overview": overview,
        "conversation_landscape": conversation_landscape,
        "devices": devices,
        "ai": ai,
        "screentime": screentime,
        "additional_insights": additional_insights,
        "appendix": appendix,
        "evidence": {
            "devices_themes": {t["title"]: [_brief(a) for a in pillar_rows_map["devices"] if a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in devices["themes"]},
            "ai_themes": {t["title"]: [_brief(a) for a in pillar_rows_map["ai"] if a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in ai["themes"]},
            "screentime_themes": {t["title"]: [_brief(a) for a in pillar_rows_map["screentime"] if a.get(taxonomy.GROUP_FIELD) == t["key"]][:SAMPLE] for t in screentime["themes"]},
        },
    }
