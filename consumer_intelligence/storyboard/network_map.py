"""Network Map storyboard — news-article adaptation.

Port of ConsumerIntelligence_PR/backend/app/charts/storyboard/network.py.
Adaptation: news articles rarely carry `community`; fall back to `section`,
then `theme`, so communities are "the rooms this coverage lives in".
Authors are outlet/byline names rather than social handles.
"""

from .. import aggregate, brand_media
from . import brands as brand_metrics

LENS_KEY = "network_map"

COMMUNITY_COLORS = ["#702082", "#E4007C", "#C8102E", "#1C7C9C", "#00897B", "#E8A33D"]
LAYOUT = [
    {"angle": 25, "dist": 0.14},
    {"angle": 305, "dist": 0.19},
    {"angle": 210, "dist": 0.21},
    {"angle": 150, "dist": 0.25},
    {"angle": 350, "dist": 0.27},
    {"angle": 245, "dist": 0.29},
]
MAX_COMMUNITIES = len(LAYOUT)
MIN_COMMUNITY_VOLUME = 5
TOP_CO_MENTIONS = 3


def _community_label(article: dict) -> str | None:
    for field in ("community", "section", "theme"):
        value = article.get(field)
        if value and not aggregate.is_junk(value):
            return str(value).strip()
    return None


def _author(article: dict) -> str:
    return str(article.get("author") or article.get("source_name") or article.get("domain") or "").strip()


def _fmt(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{round(value):,}"


def _positive_rate(articles: list[dict]) -> tuple[float, float]:
    rated = [a for a in articles if a.get("sentiment")]
    if not rated:
        return 0.0, 0.0
    pos = sum(1 for a in rated if a["sentiment"] == "Positive")
    neg = sum(1 for a in rated if a["sentiment"] == "Negative")
    return round(pos * 100 / len(rated), 1), round(neg * 100 / len(rated), 1)


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


def _topics(articles: list[dict], limit: int = 4) -> list[dict]:
    counts = aggregate.count_by(articles, "subtheme", skip_junk=True) or aggregate.count_by(
        articles, "theme", skip_junk=True
    )
    top = aggregate.top_n(counts, limit)
    if not top:
        return []
    peak = top[0][1] or 1
    total = sum(counts.values()) or 1
    return [
        {"name": n, "value": v, "pct": round(v * 100 / total, 1), "bar_pct": round(v * 100 / peak, 1)}
        for n, v in top
    ]


def _voice_rows(articles: list[dict], limit: int = 4) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for a in articles:
        name = _author(a)
        if name and not aggregate.is_junk(name):
            grouped.setdefault(name, []).append(a)
    rows = sorted(grouped.items(), key=lambda kv: -sum(aggregate.engagement(a) for a in kv[1]))
    out = []
    for name, subset in rows[:limit]:
        eng = sum(aggregate.engagement(a) for a in subset)
        out.append(
            {
                "handle": name,
                "posts": len(subset),
                "metric_value": _fmt(eng),
                "metric_label": "reach",
            }
        )
    return out


def _members(articles: list[dict], limit: int = 4) -> list[str]:
    counts: dict[str, int] = {}
    for a in articles:
        name = _author(a)
        if name and not aggregate.is_junk(name):
            counts[name] = counts.get(name, 0) + 1
    return [n for n, _ in aggregate.top_n(counts, limit)]


def _co_mentions(articles: list[dict], brand: str, known: list[str]) -> list[tuple[str, int]]:
    tally: dict[str, int] = {}
    for a in articles:
        for name in aggregate.brands_in(a, known):
            if name != brand:
                tally[name] = tally.get(name, 0) + 1
    return aggregate.top_n(tally, TOP_CO_MENTIONS)


def _communities(articles: list[dict], brand: str, known: list[str]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for a in articles:
        name = _community_label(a)
        if name:
            grouped.setdefault(name, []).append(a)

    ranked = [
        (n, rows)
        for n, rows in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if len(rows) >= MIN_COMMUNITY_VOLUME
    ][:MAX_COMMUNITIES]
    if not ranked:
        return []

    total_mentions = sum(len(r) for _, r in ranked) or 1
    total_engagement = sum(aggregate.engagement(a) for a in articles) or 1
    total_reach = sum(aggregate.reach(a) for a in articles) or 1

    built = []
    for index, (name, rows) in enumerate(ranked):
        eng = sum(aggregate.engagement(a) for a in rows)
        reach = sum(aggregate.reach(a) for a in rows)
        pos, neg = _positive_rate(rows)
        rivals = _co_mentions(rows, brand, known)
        built.append(
            {
                "id": index,
                "name": name,
                "color": COMMUNITY_COLORS[index % len(COMMUNITY_COLORS)],
                "frac": round(len(rows) / total_mentions, 4),
                "angle": LAYOUT[index]["angle"],
                "dist": LAYOUT[index]["dist"],
                "mentions": len(rows),
                "share": round(len(rows) * 100 / total_mentions, 1),
                "engagement_share": round(eng * 100 / total_engagement, 1),
                "reach_share": round(reach * 100 / total_reach, 1),
                "positive_rate": pos,
                "negative_rate": neg,
                "rivals": [{"brand": b, "count": c} for b, c in rivals],
                "subtitle": "",
                "insights": [],
                "members": _members(rows),
                "sentiment_split": _split(rows),
                "topics": _topics(rows),
                "voices": _voice_rows(rows),
                "kpis": [
                    {"val": f"{len(rows):,}", "lbl": "Mentions", "sub": f"{round(len(rows) * 100 / total_mentions, 1)}% of conversation"},
                    {"val": f"{round(eng * 100 / total_engagement, 1)}%", "lbl": "Engagement Share", "sub": _fmt(eng) + " reach"},
                    {"val": f"{pos}%", "lbl": "Positive Rate", "sub": f"{neg}% negative"},
                    {"val": f"{round(reach * 100 / total_reach, 1)}%", "lbl": "Reach Share", "sub": _fmt(reach) + " potential reach"},
                    {"val": rivals[0][0] if rivals else "—", "lbl": "Top Co-mention", "sub": f"{rivals[0][1]} shared articles" if rivals else "no other brand in this cluster"},
                ],
            }
        )
    return built


def _network_stats(articles: list[dict], communities: list[dict]) -> dict:
    authors: dict[str, set[str]] = {}
    for a in articles:
        name = _author(a)
        community = _community_label(a)
        if not name or aggregate.is_junk(name):
            continue
        authors.setdefault(name, set())
        if community:
            authors[name].add(community)
    bridges = sum(1 for seen in authors.values() if len(seen) > 1)
    named = {c["name"] for c in communities}
    return {
        "accounts": len(authors),
        "connections": sum(c["mentions"] for c in communities) * 2,
        "bridges": bridges,
        "bridge_note": (
            f"outlets publishing into more than one of the {len(named)} communities"
            if named
            else "no communities detected"
        ),
    }


def _spotlight(articles: list[dict], communities: list[dict]) -> dict | None:
    if not communities:
        return None
    warmest = max(communities, key=lambda c: (c["positive_rate"], c["mentions"]))
    rows = [a for a in articles if _community_label(a) == warmest["name"] and _author(a)]
    if not rows:
        return None
    by_author: dict[str, list[dict]] = {}
    for a in rows:
        by_author.setdefault(_author(a), []).append(a)
    handle, posts = max(
        by_author.items(),
        key=lambda kv: (len(kv[1]), sum(aggregate.engagement(a) for a in kv[1])),
    )
    peak_reach = max((aggregate.reach(a) for a in posts), default=0.0)
    eng = sum(aggregate.engagement(a) for a in posts)
    positive = sum(1 for a in posts if a.get("sentiment") == "Positive")
    quotes = []
    for a in sorted(posts, key=aggregate.engagement, reverse=True)[:3]:
        text = str(a.get("title") or a.get("content") or "").strip()
        if text:
            quotes.append(
                {
                    "text": text[:260] + ("…" if len(text) > 260 else ""),
                    "sentiment": a.get("sentiment") or "Neutral",
                    "source": aggregate.source_platform(a),
                    "url": a.get("url") or "",
                }
            )
    initials = "".join(w[0] for w in handle.replace("_", " ").split()[:2]).upper()[:2] or handle[:2].upper()
    return {
        "handle": handle,
        "initials": initials,
        "community": warmest["name"],
        "community_id": warmest["id"],
        "color": warmest["color"],
        "role": "",
        "bio": "",
        "implication": "",
        "kpis": [
            {"val": str(len(posts)), "lbl": "Articles in Window", "sub": "in this community"},
            {"val": _fmt(peak_reach), "lbl": "Peak Reach", "sub": "per article"},
            {"val": _fmt(eng), "lbl": "Total Reach", "sub": "across all articles"},
            {"val": f"{round(positive * 100 / len(posts))}%", "lbl": "Positive", "sub": f"{positive} of {len(posts)} articles"},
        ],
        "quotes": quotes,
    }


def _slides() -> list[dict]:
    def shell(sid, label, number, eyebrow):
        return {"id": sid, "label": label, "number": number, "eyebrow": eyebrow, "title": "", "body": ""}

    return [
        shell("s1", "The Network", "01", "COMMUNITY MAP"),
        shell("s2", "Where the Brand Sits", "02", "STRUCTURE"),
        shell("s3", "Community Deep Dive", "03", "DEEP DIVE"),
    ]


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    days = sorted(d for d in aggregate.bucket_by_day(articles) if d != aggregate.UNKNOWN_DAY)
    window = f"{days[0]} – {days[-1]}" if days else "No dated coverage"
    communities = _communities(articles, brand, known_brands)
    sov = brand_metrics.share_of_voice(articles, known_brands)
    stats = _network_stats(articles, communities)
    eng_total = sum(aggregate.engagement(a) for a in articles)
    reach_total = sum(aggregate.reach(a) for a in articles)

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": [b for b in known_brands if b and b != brand],
            "total_conversations": len(articles),
            "window_label": window,
            "dataset_mode": brand_metrics.dataset_mode(sov),
            "community_count": len(communities),
            "community_source": "section_or_theme",
            "logos": brand_media.brand_logos([brand, *known_brands], articles),
        },
        "hero": {"eyebrow": window, "title": "", "subtitle": "", "chips": [], "media": None},
        "communities": communities,
        "graph": {"seed": 7777, "nodes": 1600, "edges": 1100},
        "slides": _slides(),
        "metrics": [
            {"label": "Communities", "value": str(len(communities))},
            {
                "label": "Largest cluster",
                "value": f"{communities[0]['share']}%" if communities else "—",
                "sub": communities[0]["name"] if communities else "",
            },
            {
                "label": "Most engaged",
                "value": max(communities, key=lambda c: c["engagement_share"])["name"] if communities else "—",
            },
        ],
        "spotlight": _spotlight(articles, communities),
        "network_stats": stats,
        "conversation_metrics": [
            {"value": f"{len(articles):,}", "label": "Articles analysed"},
            {"value": _fmt(eng_total), "label": "Total reach"},
            {"value": _fmt(reach_total), "label": "Potential reach"},
        ],
        "callout": "",
        "footer": [],
    }
