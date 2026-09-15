"""Brand Health Tracker storyboard — news-article adaptation.

Port of ConsumerIntelligence_PR/backend/app/charts/storyboard/health.py.
Adaptation: news articles don't carry `bhi_dimension`. This module derives
the BHI dimension from theme/subtheme/content keyword matching.
"""

from .. import aggregate, brand_media
from . import brands as brand_metrics

LENS_KEY = "brand_health_storyboard"

DIMENSIONS = [
    {"key": "awareness", "name": "Awareness", "weight": 20, "color": "#702082"},
    {"key": "consideration", "name": "Consideration", "weight": 20, "color": "#9333ea"},
    {"key": "preference", "name": "Preference", "weight": 25, "color": "#c026d3"},
    {"key": "advocacy", "name": "Advocacy", "weight": 20, "color": "#e4007c"},
    {"key": "trust", "name": "Trust", "weight": 15, "color": "#1c7c9c"},
]

# Keyword lists for deriving BHI dimension from text content.
# Priority: first match wins (lists are ordered by specificity).
_BHI_KEYWORDS: dict[str, list[str]] = {
    "Advocacy": [
        "recommend", "referral", "word of mouth", "ambassador", "advocate",
        "evangelist", "loyal", "love it", "amazing", "raving fan",
        "tell everyone", "share this", "must try", "must have",
    ],
    "Trust": [
        "trust", "reliable", "dependable", "quality", "safe", "safety",
        "honest", "credible", "transparency", "integrity", "authentic",
        "ethical", "regulation", "compliance", "recall", "scandal",
    ],
    "Preference": [
        "prefer", "favorite", "favourite", "best", "top choice", "go-to",
        "over competitors", "instead of", "rather than", "beats",
        "chosen", "pick", "selected", "switched to",
    ],
    "Consideration": [
        "consider", "thinking about", "looking at", "comparing",
        "evaluation", "alternative", "option", "might buy", "planning to",
        "interested in", "researching", "shortlist", "review",
    ],
    "Awareness": [
        "aware", "heard of", "seen", "discovered", "came across", "noticed",
        "exposure", "coverage", "media", "advertisement", "ad ", "press",
        "launch", "announced", "new brand",
    ],
}


def _derive_bhi_dimension(article: dict) -> str | None:
    """Keyword-based BHI dimension derivation for news articles.

    First tries `bhi_dimension` field (tagged data), then falls back to
    scanning theme, subtheme, title and content for dimension keywords.
    Returns the dimension name or None when no signal is found.
    """
    tagged = article.get("bhi_dimension")
    if tagged and not aggregate.is_junk(tagged):
        return str(tagged).strip()

    # Tagger fields outrank free text: a theme of "Trust" is a stronger signal
    # than the word "recommend" appearing once in a 600-word body.
    tagged_text = " ".join(str(article.get(f) or "") for f in ("theme", "subtheme", "title")).lower()
    body_text = str(article.get("content") or "").lower()

    scores: dict[str, float] = {}
    for dimension, keywords in _BHI_KEYWORDS.items():
        score = 0.0
        for kw in keywords:
            if kw in tagged_text:
                score += 3.0
            if kw in body_text:
                score += 1.0
        if score:
            scores[dimension] = score
    if not scores:
        return None
    best = max(scores.values())
    winners = [d for d, s in scores.items() if s == best]
    return winners[0] if len(winners) == 1 else None


TOP_SUB_KPIS = 6
TOP_CHANNELS = 5
TOP_COMPETITORS = 6
MIN_DIMENSION_VOLUME = 5

CONFIDENCE_TIERS = (("High", 0.8), ("Medium", 0.5), ("Low", 0.0))
BANDS = ((80, "Strong"), (65, "Healthy"), (50, "Stable"), (35, "Under pressure"), (0, "At risk"))


def _score(articles: list[dict]) -> int:
    return round((aggregate.net_sentiment(articles) + 100) / 2)


def _band(score: float) -> str:
    for floor, label in BANDS:
        if score >= floor:
            return label
    return BANDS[-1][1]


def _split(articles: list[dict]) -> list[dict]:
    rated = [a for a in articles if a.get("sentiment")]
    total = len(rated) or 1
    return [
        {
            "label": label,
            "tone": tone,
            "value": sum(1 for a in rated if a["sentiment"] == label),
            "pct": round(sum(1 for a in rated if a["sentiment"] == label) * 100 / total, 1),
        }
        for label, tone in (("Positive", "pos"), ("Neutral", "neu"), ("Negative", "neg"))
    ]


def _bars(counts: dict[str, int], limit: int) -> list[dict]:
    top = aggregate.top_n(counts, limit)
    if not top:
        return []
    peak = top[0][1] or 1
    total = sum(counts.values()) or 1
    return [
        {
            "name": name,
            "value": value,
            "share": round(value * 100 / total, 1),
            "bar_pct": round(value * 100 / peak, 1),
        }
        for name, value in top
    ]


def _sub_kpis(rows: list[dict], limit: int) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for article in rows:
        name = article.get("subtheme") or article.get("theme")
        if name and not aggregate.is_junk(name):
            grouped.setdefault(str(name).strip(), []).append(article)
    if not grouped:
        return []
    ranked = sorted(grouped.items(), key=lambda kv: -len(kv[1]))[:limit]
    peak = len(ranked[0][1]) or 1
    total = sum(len(v) for v in grouped.values()) or 1
    out = []
    for name, subset in ranked:
        rated = [a for a in subset if a.get("sentiment")]
        pos = sum(1 for a in rated if a["sentiment"] == "Positive")
        neu = sum(1 for a in rated if a["sentiment"] == "Neutral")
        neg = sum(1 for a in rated if a["sentiment"] == "Negative")
        out.append(
            {
                "name": name,
                "value": len(subset),
                "share": round(len(subset) * 100 / total, 1),
                "bar_pct": round(len(subset) * 100 / peak, 1),
                "positive": pos,
                "neutral": neu,
                "negative": neg,
                "pos_rate": round(pos * 100 / (len(rated) or 1), 1),
            }
        )
    return out


def _confidence(articles: list[dict]) -> list[dict]:
    tiers = {label: 0 for label, _ in CONFIDENCE_TIERS}
    for article in articles:
        raw = article.get("sentiment_confidence")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 1:
            value /= 100
        for label, floor in CONFIDENCE_TIERS:
            if value >= floor:
                tiers[label] += 1
                break
    total = sum(tiers.values()) or 1
    return [
        {"name": label, "value": count, "pct": round(count * 100 / total, 1)}
        for label, count in tiers.items()
    ]


def _daily(articles: list[dict], days: list[str]) -> list[dict]:
    buckets = aggregate.bucket_by_day(articles)
    out = []
    for day in days:
        rows = buckets.get(day, [])
        rated = [a for a in rows if a.get("sentiment")]
        out.append(
            {
                "day": day,
                "volume": len(rows),
                "net_sentiment": aggregate.net_sentiment(rows),
                "positive": sum(1 for a in rated if a["sentiment"] == "Positive"),
                "neutral": sum(1 for a in rated if a["sentiment"] == "Neutral"),
                "negative": sum(1 for a in rated if a["sentiment"] == "Negative"),
            }
        )
    return out


def _bhi_series(articles: list[dict], days: list[str]) -> dict:
    buckets = aggregate.bucket_by_day(articles)
    points = [
        {"label": day, "score": _score(buckets.get(day, []))}
        for day in days
        if buckets.get(day)
    ]
    span = f"{days[0]} to {days[-1]}" if len(days) >= 2 else (days[0] if days else "")
    return {"span": span, "points": points}


def _dimension(name: str, spec: dict, rows: list[dict], total: int, days: list[str]) -> dict:
    score = _score(rows) if rows else 0
    return {
        "key": spec["key"],
        "name": name,
        "weight": spec["weight"],
        "color": spec["color"],
        "score": score,
        "band": _band(score),
        "volume": len(rows),
        "share": round(len(rows) * 100 / total, 1) if total else 0.0,
        "net_sentiment": aggregate.net_sentiment(rows),
        "engagement": round(sum(aggregate.engagement(a) for a in rows)),
        "reach": round(sum(aggregate.reach(a) for a in rows)),
        "sub_kpis": _sub_kpis(rows, TOP_SUB_KPIS),
        "themes": _bars(aggregate.count_by(rows, "theme", skip_junk=True), TOP_SUB_KPIS),
        "channels": _bars(aggregate.count_by(rows, "source_type", skip_junk=True), TOP_CHANNELS),
        "sentiment_split": _split(rows),
        "confidence": _confidence(rows),
        "daily": _daily(rows, days),
        "headline": "",
        "body": "",
        "insights": [],
        "image": None,
    }


def _co_mentions(articles: list[dict], brand: str, known: list[str]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for article in articles:
        named = aggregate.brands_in(article, known)
        if brand not in named:
            continue
        for other in named:
            if other != brand:
                grouped.setdefault(other, []).append(article)
    if not grouped:
        return []
    total = sum(len(rows) for rows in grouped.values()) or 1
    brand_net = aggregate.net_sentiment(
        [a for a in articles if brand in aggregate.brands_in(a, known)]
    )
    rows = []
    for name, subset in sorted(grouped.items(), key=lambda kv: -len(kv[1])):
        has_enough = len(subset) >= brand_metrics.MIN_MENTIONS_FOR_LEAGUE
        net = aggregate.net_sentiment(subset) if has_enough else None
        rows.append(
            {
                "brand": name,
                "value": len(subset),
                "share": round(len(subset) * 100 / total, 1),
                "net_sentiment": net,
                "differential": round(net - brand_net, 1) if net is not None else None,
            }
        )
    return rows[:TOP_COMPETITORS]


def _competitive_heatmap(articles: list[dict], known: list[str], brand: str) -> dict:
    """Every brand scored on every dimension — the source's competitive heatmap.

    Same 0-100 scale as the dimension screens, so a cell is comparable to the
    headline score. A brand-dimension pair with too little coverage is None rather
    than 0: an empty cell reads as "not measured", a zero reads as "terrible".
    Dimension comes from `_derive_bhi_dimension` (news has no `bhi_dimension`).
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for article in articles:
        dimension = _derive_bhi_dimension(article)
        if not dimension or aggregate.is_junk(dimension):
            continue
        for name in aggregate.brands_in(article, known):
            grouped.setdefault((name, str(dimension).strip()), []).append(article)

    totals: dict[str, int] = {}
    for (name, _), rows in grouped.items():
        totals[name] = totals.get(name, 0) + len(rows)
    ranked = [n for n, _ in aggregate.top_n(totals, TOP_COMPETITORS)]

    names = [d["name"] for d in DIMENSIONS]
    rows = []
    for name in ranked:
        cells = []
        for dimension in names:
            subset = grouped.get((name, dimension), [])
            cells.append(
                {
                    "dimension": dimension,
                    "score": _score(subset) if len(subset) >= brand_metrics.MIN_MENTIONS_FOR_LEAGUE else None,
                    "volume": len(subset),
                }
            )
        rows.append({"brand": name, "is_brand": name == brand, "cells": cells})
    return {"dimensions": names, "rows": rows}


def _composite(dimensions: list[dict]) -> dict:
    scored = [d for d in dimensions if d["volume"] >= MIN_DIMENSION_VOLUME]
    weight_total = sum(d["weight"] for d in scored)
    if not scored or not weight_total:
        return {
            "score": 0,
            "band": "No coverage",
            "covered": 0,
            "of": len(dimensions),
            "renormalised": False,
            "contributions": [],
        }
    score = round(sum(d["score"] * d["weight"] for d in scored) / weight_total)
    return {
        "score": score,
        "band": _band(score),
        "covered": len(scored),
        "of": len(dimensions),
        "renormalised": len(scored) != len(dimensions),
        "contributions": [
            {
                "name": d["name"],
                "color": d["color"],
                "weight": d["weight"],
                "score": d["score"],
                "contribution": round(d["score"] * d["weight"] / weight_total, 1),
            }
            for d in scored
        ],
    }


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Compute full Brand Health payload. BHI dimension derived from keywords."""
    days = sorted(d for d in aggregate.bucket_by_day(articles) if d != aggregate.UNKNOWN_DAY)
    window = f"{days[0]} – {days[-1]}" if days else "No dated coverage"

    # Group articles by derived BHI dimension
    grouped: dict[str, list[dict]] = {}
    for article in articles:
        dim = _derive_bhi_dimension(article)
        if dim:
            grouped.setdefault(dim, []).append(article)

    total = sum(len(rows) for rows in grouped.values())
    dimensions = [
        _dimension(spec["name"], spec, grouped.get(spec["name"], []), total, days)
        for spec in DIMENSIONS
    ]
    composite = _composite(dimensions)
    sov = brand_metrics.share_of_voice(articles, known_brands)
    league = brand_metrics.net_sentiment_league(articles, known_brands)
    rank = next((i + 1 for i, row in enumerate(sov) if row["brand"] == brand), None)

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": [b for b in known_brands if b and b != brand],
            "total_conversations": len(articles),
            "classified": total,
            "window_label": window,
            "days": days,
            "dataset_mode": brand_metrics.dataset_mode(sov),
            "dimension_source": "keyword_derived",
            "logos": brand_media.brand_logos([brand, *known_brands], articles),
        },
        "hero": {"eyebrow": window, "title": "", "subtitle": "", "chips": [], "media": None},
        "bhi": composite,
        "dimensions": dimensions,
        "sentiment_split": _split(articles),
        "channels": _bars(aggregate.count_by(articles, "source_type", skip_junk=True), TOP_CHANNELS),
        "kpi_distribution": [
            {
                "name": d["name"],
                "value": d["volume"],
                "color": d["color"],
                "bar_pct": round(
                    d["volume"] * 100 / max(1, max(x["volume"] for x in dimensions)), 1
                ),
            }
            for d in dimensions
        ],
        "radar": [
            {
                "name": d["name"],
                "score": d["score"],
                "benchmark": (
                    round(
                        sum(o["score"] for o in dimensions if o["key"] != d["key"] and o["volume"])
                        / max(1, len([o for o in dimensions if o["key"] != d["key"] and o["volume"]]))
                    )
                ),
            }
            for d in dimensions
        ],
        "sentiment_trend": _daily(articles, days),
        "bhi_series": _bhi_series(articles, days),
        "competitive": {
            "tracked": len([b for b in known_brands if b and b != brand]),
            "rank": rank,
            "share_of_voice": sov,
            "sentiment_league": league,
            "co_mentions": _co_mentions(articles, brand, known_brands),
            "heatmap": _competitive_heatmap(articles, known_brands, brand),
        },
        "callout": "",
        "footer": [],
    }
