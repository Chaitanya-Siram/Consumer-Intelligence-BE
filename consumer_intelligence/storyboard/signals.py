"""Per-signal metrics for Trend Intelligence storyboard.

Ported from ConsumerIntelligence_PR/backend/app/charts/storyboard/signals.py.
Adaptation: news articles don't have a `signal` field — signal is derived from
`subtheme` (primary) or `theme` (fallback) via aggregate.signal_label().
"""

from .. import aggregate

PALETTE = [
    "#3b82f6", "#702082", "#a78bfa", "#10b981",
    "#f59e0b", "#ec4899", "#0891b2", "#ef4444",
]

MAX_SIGNALS = 6
PER = 1000
MIN_VOLUME = 5  # lowered from 10 for news articles (smaller corpora)
GROWTH_CAP = 200

JUNK_SIGNALS = aggregate.JUNK_LABELS

STAGE_LABELS = {
    "emerging": "◆ Emerging",
    "accelerating": "▲ Accelerating",
    "mainstream": "● Mainstream",
    "declining": "▼ Declining",
}


def stage_for(growth: float) -> str:
    if growth >= 30:
        return "emerging"
    if growth >= 10:
        return "accelerating"
    if growth >= 0:
        return "mainstream"
    return "declining"


def growth_for(share: list[float]) -> int:
    series = [v for v in share if v is not None]
    if len(series) < 2:
        return 0
    half = len(series) // 2
    first = series[:half]
    second = series[len(series) - half:]
    base = sum(first) / len(first)
    if base <= 0:
        return 0
    later = sum(second) / len(second)
    return max(-GROWTH_CAP, min(GROWTH_CAP, round((later - base) * 100 / base)))


def _share_series(signal_articles: list[dict], totals: dict[str, int], days: list[str]) -> list[float]:
    by_day = aggregate.bucket_by_day(signal_articles)
    series = []
    for day in days:
        total = totals.get(day, 0)
        count = len(by_day.get(day, []))
        series.append(round(count * PER / total, 1) if total else 0.0)
    return series


def _platform_share(signal_articles: list[dict], platforms: list[str]) -> list[int]:
    counts: dict[str, int] = {}
    for a in signal_articles:
        p = aggregate.source_platform(a)
        counts[p] = counts.get(p, 0) + 1
    total = len(signal_articles)
    if not total:
        return [0 for _ in platforms]
    return [round(counts.get(name, 0) * 100 / total) for name in platforms]


def _leaders(signal_articles: list[dict], known_brands: list[str], limit: int = 5) -> list[dict]:
    tally: dict[str, int] = {}
    for article in signal_articles:
        for brand in aggregate.brands_in(article, known_brands):
            tally[brand] = tally.get(brand, 0) + 1
    return [{"brand": name, "count": count} for name, count in aggregate.top_n(tally, limit)]


def _capture(signal_articles: list[dict], brand: str, known_brands: list[str]) -> float:
    if not brand or not signal_articles:
        return 0.0
    hits = sum(1 for a in signal_articles if brand in aggregate.brands_in(a, known_brands))
    return round(hits * 100 / len(signal_articles), 1)


def build_signals(
    articles: list[dict],
    *,
    days: list[str],
    platforms: list[str],
    brand: str,
    known_brands: list[str],
) -> list[dict]:
    """Build signal list from subtheme/theme fields of news articles."""
    grouped: dict[str, list[dict]] = {}
    for article in articles:
        name = aggregate.signal_label(article)
        if name and str(name).strip().lower() not in JUNK_SIGNALS:
            grouped.setdefault(str(name).strip(), []).append(article)

    by_volume = [
        (name, rows)
        for name, rows in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if len(rows) >= MIN_VOLUME
    ][:MAX_SIGNALS]
    if not by_volume:
        return []

    totals = {day: len(rows) for day, rows in aggregate.bucket_by_day(articles).items()}

    built = []
    for name, rows in by_volume:
        share = _share_series(rows, totals, days)
        growth = growth_for(share)
        stage = stage_for(growth)
        built.append(
            {
                "name": name,
                "stage": stage,
                "stage_label": STAGE_LABELS[stage],
                "share": share,
                "platforms": _platform_share(rows, platforms),
                "volume": len(rows),
                "growth": growth,
                "net_sentiment": aggregate.net_sentiment(rows),
                "brand_capture": _capture(rows, brand, known_brands),
                "leaders": _leaders(rows, known_brands),
            }
        )

    built.sort(key=lambda s: (-s["growth"], -s["volume"]))
    for index, signal in enumerate(built):
        signal["id"] = f"signal_{index}"
        signal["color"] = PALETTE[index % len(PALETTE)]
    return built
