"""Brand-level metrics for Consumer Intelligence storyboards.

Ported from ConsumerIntelligence_PR/backend/app/charts/storyboard/brands.py.
Adapted for news-article data: reach replaces engagement where engagement is absent.
"""

from .. import aggregate

TOP_BRANDS = 10
MIN_MENTIONS_FOR_LEAGUE = 3
LEAGUE_SHARE_FLOOR = 0.02
MIN_SHARE_FOR_CATEGORY = 5.0
MIN_BRANDS_FOR_CATEGORY = 3


def league_threshold(total_mentions: int) -> int:
    return max(MIN_MENTIONS_FOR_LEAGUE, round(total_mentions * LEAGUE_SHARE_FLOOR))


def mention_counts(articles: list[dict], known_brands: list[str]) -> dict[str, int]:
    tally: dict[str, int] = {}
    for article in articles:
        for brand in aggregate.brands_in(article, known_brands):
            tally[brand] = tally.get(brand, 0) + 1
    return tally


def share_of_voice(articles: list[dict], known_brands: list[str]) -> list[dict]:
    tally = mention_counts(articles, known_brands)
    total = sum(tally.values())
    if not total:
        return []
    return [
        {"brand": name, "value": round(count * 100 / total, 1)}
        for name, count in aggregate.top_n(tally, TOP_BRANDS)
    ]


def net_sentiment_league(articles: list[dict], known_brands: list[str]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for article in articles:
        for brand in aggregate.brands_in(article, known_brands):
            grouped.setdefault(brand, []).append(article)

    floor = league_threshold(sum(len(rows) for rows in grouped.values()))
    league = [
        {"brand": name, "value": aggregate.net_sentiment(rows), "mentions": len(rows)}
        for name, rows in grouped.items()
        if len(rows) >= floor
    ]
    league.sort(key=lambda row: (-row["value"], row["brand"]))
    return league[:TOP_BRANDS]


def leaders_matrix(signals: list[dict]) -> dict[str, dict[str, int]]:
    return {
        signal["name"]: {row["brand"]: row["count"] for row in signal["leaders"]}
        for signal in signals
    }


def dataset_mode(sov: list[dict]) -> str:
    strong = [row for row in sov if row["value"] >= MIN_SHARE_FOR_CATEGORY]
    return "category" if len(strong) >= MIN_BRANDS_FOR_CATEGORY else "brand"


def capture_ranking(signals: list[dict], *, mode: str, total: int) -> tuple[list[dict], str]:
    if mode == "category":
        ranked = sorted(signals, key=lambda s: -s["brand_capture"])
        return (
            [
                {"name": s["name"], "capture": s["brand_capture"], "color": s["color"]}
                for s in ranked
            ],
            "How much of each trend names the brand",
        )
    ranked = sorted(signals, key=lambda s: -s["volume"])
    return (
        [
            {
                "name": s["name"],
                "capture": round(s["volume"] * 100 / total, 1) if total else 0.0,
                "color": s["color"],
            }
            for s in ranked
        ],
        "Each signal's share of the brand's conversation",
    )
