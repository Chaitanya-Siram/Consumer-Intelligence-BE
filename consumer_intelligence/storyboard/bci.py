"""Brand & Competitive Intel storyboard — news-article adaptation.

Faithful port of ConsumerIntelligence_PR/backend/app/charts/storyboard/bci.py:
three tabs — an overview of volume, sentiment and engagement; a brand deep-dive
into what drives each sentiment; and a competitor benchmark. Every visual is a
CSS bar, so this module emits values plus the percentage each bar fills.

Numbers are computed here; prose fields are left empty for `narrative.py`.

Adaptations (field access only):
- platform   → aggregate.source_platform() (source_type → section → domain)
- engagement → aggregate.engagement()      (falls back to reach)
"""

from .. import aggregate, brand_media
from . import brands as brand_metrics

LENS_KEY = "brand_competitive_intel"

TOP_PLATFORMS = 6
TOP_THEMES = 6
TOP_ITEMS = 8
TOP_POSTS = 8
TOP_COMPETITORS = 6

# Below this a "driver" is one or two articles — a coincidence, not a driver.
MIN_DRIVER_VOLUME = 3


_engagement = aggregate.engagement
_reach = aggregate.reach


def _platform_counts(articles: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for article in articles:
        name = aggregate.source_platform(article)
        if name and not aggregate.is_junk(name):
            counts[name] = counts.get(name, 0) + 1
    return counts


def _bars(counts: dict[str, int], limit: int) -> list[dict]:
    """Ranked rows carrying the percentage each bar should fill.

    `bar_pct` is relative to the largest row, not the total: these render as a
    bar list where the leader fills the track, which is how the source page reads.
    """
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
            # How wide to draw the bar, NOT a statistic: the leader is always 100.
            "bar_pct": round(value * 100 / peak, 1),
        }
        for name, value in top
    ]


def _sentiment_split(articles: list[dict]) -> list[dict]:
    rated = [a for a in articles if a.get("sentiment")]
    total = len(rated) or 1
    out = []
    for label, tone in (("Positive", "pos"), ("Neutral", "neu"), ("Negative", "neg")):
        count = sum(1 for a in rated if a["sentiment"] == label)
        out.append(
            {"label": label, "tone": tone, "value": count, "pct": round(count * 100 / total, 1)}
        )
    return out


def _by_sentiment(articles: list[dict], sentiment: str, field: str, limit: int) -> list[dict]:
    """What the conversation of one sentiment is actually about."""
    subset = [a for a in articles if a.get("sentiment") == sentiment]
    counts = {
        k: v
        for k, v in aggregate.count_by(subset, field, skip_junk=True).items()
        if v >= MIN_DRIVER_VOLUME
    }
    return _bars(counts, limit)


def _items(articles: list[dict], limit: int, exclude: set[str]) -> list[dict]:
    """Most-mentioned entities in the brand's conversation.

    Drawn from the tagged organisations and people. The brand and its benchmarked
    rivals are excluded — they have their own panel, and the subject brand would
    otherwise top this list in every dataset.
    """
    skip = {name.strip().lower() for name in exclude if name}
    counts: dict[str, int] = {}
    for article in articles:
        for field in ("organizations", "peoples", "people"):
            values = article.get(field) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                name = str(value).strip()
                if name and name.lower() not in skip and not aggregate.is_junk(name):
                    counts[name] = counts.get(name, 0) + 1
    return _bars({k: v for k, v in counts.items() if v >= MIN_DRIVER_VOLUME}, limit)


def _platform_engagement(articles: list[dict], limit: int) -> list[dict]:
    """Volume against engagement per platform.

    The storyboard's point is that these disagree — the loudest channel is rarely
    the most engaged — so both measures travel together.
    """
    volume: dict[str, int] = {}
    engaged: dict[str, float] = {}
    for article in articles:
        name = aggregate.source_platform(article)
        if not name or aggregate.is_junk(name):
            continue
        volume[name] = volume.get(name, 0) + 1
        engaged[name] = engaged.get(name, 0.0) + _engagement(article)

    ranked = aggregate.top_n(volume, limit)
    peak_engagement = max((engaged.get(n, 0.0) for n, _ in ranked), default=0.0) or 1
    peak_volume = ranked[0][1] if ranked else 1
    return [
        {
            "name": name,
            "volume": count,
            "engagement": round(engaged.get(name, 0.0)),
            "avg_engagement": round(engaged.get(name, 0.0) / count) if count else 0,
            "volume_pct": round(count * 100 / peak_volume, 1),
            "engagement_pct": round(engaged.get(name, 0.0) * 100 / peak_engagement, 1),
        }
        for name, count in ranked
    ]


def _top_posts(articles: list[dict], limit: int) -> list[dict]:
    """Highest-engagement posts, balanced across sentiments.

    Taking the raw top N gives a wall of one sentiment; the source page shows the
    conversation from both sides, so each is quota'd.
    """
    picked: list[dict] = []
    per_sentiment = max(1, limit // 3)
    for sentiment in ("Positive", "Negative", "Neutral"):
        subset = [
            a
            for a in articles
            if a.get("sentiment") == sentiment and (a.get("content") or a.get("title"))
        ]
        subset.sort(key=_engagement, reverse=True)
        picked.extend(subset[:per_sentiment])

    # The quota fills at most 3 x (limit // 3); top up from whatever is left so the
    # grid is not short two cards.
    if len(picked) < limit:
        chosen = {id(a) for a in picked}
        rest = [
            a
            for a in articles
            if id(a) not in chosen and (a.get("content") or a.get("title"))
        ]
        rest.sort(key=_engagement, reverse=True)
        picked.extend(rest[: limit - len(picked)])

    picked.sort(key=_engagement, reverse=True)
    out = []
    for article in picked[:limit]:
        text = str(article.get("content") or article.get("title") or "").strip()
        if len(text) > 240:
            text = text[:237].rstrip() + "…"
        out.append(
            {
                "text": text,
                "sentiment": article.get("sentiment") or "Neutral",
                "source": article.get("source_name") or aggregate.source_platform(article) or "",
                "author": article.get("author") or "",
                "engagement": round(_engagement(article)),
                "url": article.get("url") or "",
            }
        )
    return out


def _competitor_ranks(articles: list[dict], brand: str, known: list[str]) -> list[dict]:
    """The competitor table: one row per brand, every column the source page shows.

    Sentiment and engagement are grouped per brand here rather than derived
    separately, so a row cannot disagree with itself.
    """
    grouped: dict[str, list[dict]] = {}
    for article in articles:
        for name in aggregate.brands_in(article, known):
            grouped.setdefault(name, []).append(article)

    counts = {name: len(rows) for name, rows in grouped.items()}
    ranked = aggregate.top_n(counts, TOP_COMPETITORS)
    peak = ranked[0][1] if ranked else 1
    total = sum(counts.values()) or 1
    engagement_total = sum(_engagement(a) for rows in grouped.values() for a in rows) or 1

    rows = []
    for position, (name, value) in enumerate(ranked, start=1):
        subset = grouped[name]
        engagement = sum(_engagement(a) for a in subset)
        rated = [a for a in subset if a.get("sentiment")]
        rated_total = len(rated) or 1
        rows.append(
            {
                "rank": position,
                "brand": name,
                "value": value,
                "share": round(value * 100 / total, 1),
                # Sentiment composition — shares of this brand's own rated posts.
                "pos_pct": round(sum(1 for a in rated if a["sentiment"] == "Positive") * 100 / rated_total, 1),
                "neu_pct": round(sum(1 for a in rated if a["sentiment"] == "Neutral") * 100 / rated_total, 1),
                "neg_pct": round(sum(1 for a in rated if a["sentiment"] == "Negative") * 100 / rated_total, 1),
                # How wide to draw the bar, NOT a statistic: the leader is always 100.
                "bar_pct": round(value * 100 / peak, 1),
                # Too few mentions to rate: shown as null rather than a made-up 0.
                "net_sentiment": (
                    aggregate.net_sentiment(subset)
                    if len(subset) >= brand_metrics.MIN_MENTIONS_FOR_LEAGUE
                    else None
                ),
                "engagement": round(engagement),
                "engagement_share": round(engagement * 100 / engagement_total, 1),
                "avg_engagement": round(engagement / value) if value else 0,
                "is_brand": name == brand,
            }
        )
    return rows


def _avg_engagement(articles: list[dict], brand: str, known: list[str]) -> list[dict]:
    """Average engagement per post per brand — reach quality, not reach volume."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for article in articles:
        value = _engagement(article)
        for name in aggregate.brands_in(article, known):
            counts[name] = counts.get(name, 0) + 1
            totals[name] = totals.get(name, 0.0) + value

    rows = [
        {"brand": name, "value": round(totals[name] / count), "is_brand": name == brand}
        for name, count in counts.items()
        if count >= brand_metrics.MIN_MENTIONS_FOR_LEAGUE
    ]
    rows.sort(key=lambda r: -r["value"])
    rows = rows[:TOP_COMPETITORS]
    peak = rows[0]["value"] if rows else 1
    for row in rows:
        row["pct"] = round(row["value"] * 100 / (peak or 1), 1)
    return rows


def _banner_stats(tab_id: str, brand: str, kpis: list[dict], platforms: list[dict],
                  themes: list[dict], competitors: list[dict]) -> list[dict]:
    """The `b-stats` strip under each banner headline.

    Computed, like every other figure: the model writes the headline above them,
    never the numbers themselves.
    """
    def fmt(value):
        n = float(value)
        if abs(n) >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if abs(n) >= 1_000:
            return f"{n / 1_000:.1f}K"
        return f"{n:,.0f}" if n == int(n) else f"{n}"

    by_label = {k["label"]: k for k in kpis}
    if tab_id == "t1":
        return [
            {"value": fmt(by_label["Total Mentions"]["value"]), "label": f"{brand} mentions"},
            {"value": f"{by_label['Net Sentiment']['value']:+g}", "label": "Net sentiment"},
            {"value": fmt(by_label["Total Engagement"]["value"]), "label": "Total engagement"},
        ]
    if tab_id == "t2":
        return [
            {"value": themes[0]["name"] if themes else "—", "label": "Leading theme"},
            {"value": str(len(platforms)), "label": "Platforms"},
            {"value": platforms[0]["name"] if platforms else "—", "label": "Loudest channel"},
        ]
    return [
        {"value": str(max(0, len(competitors) - 1)), "label": "Competitors named"},
        {"value": f"{competitors[0]['share']}%" if competitors else "—", "label": "Share of voice"},
        {"value": fmt(by_label["Potential Reach"]["value"]), "label": "Potential reach"},
    ]


def _tabs(brand: str) -> list[dict]:
    def shell(tab_id, label, number, eyebrow, sections):
        return {
            "id": tab_id,
            "label": label,
            "number": number,
            "banner": {"eyebrow": eyebrow, "headline": "", "sub": "", "stats": [], "image": None},
            "sections": sections,
            "whats_next": {"eyebrow": "", "title": "", "sub": "", "actions": [], "cta": None},
        }

    return [
        shell("t1", "Overview", "01", "BRAND OVERVIEW",
              ["kpis", "breakdown", "drivers"]),
        shell("t2", "Brand Deep Dive", "02", "BRAND DEEP DIVE",
              ["items", "platform_engagement", "sentiment_drivers", "operations", "posts"]),
        shell("t3", "Competitor Intel", "03", "COMPETITOR INTEL",
              ["competitor_ranks", "avg_engagement"]),
    ]


def build_storyboard(articles: list[dict], *, brand: str, known_brands: list[str]) -> dict:
    """Compute the full payload, prose fields left empty."""
    days = sorted(d for d in aggregate.bucket_by_day(articles) if d != aggregate.UNKNOWN_DAY)
    window = f"{days[0]} – {days[-1]}" if days else "No dated coverage"

    engagement_total = sum(_engagement(a) for a in articles)
    reach_total = sum(_reach(a) for a in articles)
    sov = brand_metrics.share_of_voice(articles, known_brands)
    split = _sentiment_split(articles)
    platform_rows = _bars(_platform_counts(articles), TOP_PLATFORMS)

    tabs = _tabs(brand)
    kpis = [
        {"label": "Total Mentions", "value": len(articles), "format": "int",
         "sub": f"across {len(platform_rows)} platforms", "tag": ""},
        {"label": "Net Sentiment", "value": aggregate.net_sentiment(articles), "format": "signed",
         "sub": f"Positive {split[0]['pct']}% vs Negative {split[2]['pct']}% of rated posts", "tag": ""},
        {"label": "Total Engagement", "value": round(engagement_total), "format": "compact",
         "sub": f"{round(engagement_total / len(articles)) if articles else 0} average per post", "tag": ""},
        {"label": "Potential Reach", "value": round(reach_total), "format": "compact",
         "sub": f"over {window}", "tag": ""},
    ]
    theme_rows = _bars(aggregate.count_by(articles, "theme", skip_junk=True), TOP_THEMES)
    competitor_rows = _competitor_ranks(articles, brand, known_brands)
    for tab in tabs:
        tab["banner"]["stats"] = _banner_stats(
            tab["id"], brand, kpis, platform_rows, theme_rows, competitor_rows
        )

    return {
        "meta": {
            "lens": LENS_KEY,
            "brand": brand,
            "competitors": [b for b in known_brands if b and b != brand],
            "total_conversations": len(articles),
            "window_label": window,
            "days": days,
            "dataset_mode": brand_metrics.dataset_mode(sov),
            "logos": brand_media.brand_logos([brand, *known_brands], articles),
        },
        "kpis": kpis,
        "sentiment_split": split,
        "platforms": platform_rows,
        "themes": theme_rows,
        "drivers": [],  # LLM: the two stories moving the numbers
        "items": _items(articles, TOP_ITEMS, exclude=set(known_brands) | {brand}),
        "platform_engagement": _platform_engagement(articles, 3),
        "positive_drivers": _by_sentiment(articles, "Positive", "theme", 5),
        "negative_drivers": _by_sentiment(articles, "Negative", "theme", 5),
        "operational_issues": _by_sentiment(articles, "Negative", "subtheme", 5),
        "top_posts": _top_posts(articles, TOP_POSTS),
        "competitor_ranks": competitor_rows,
        "avg_engagement": _avg_engagement(articles, brand, known_brands),
        "tabs": tabs,
        "callout": "",
        "footer": [],
    }
