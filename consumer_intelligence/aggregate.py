"""Pure aggregation helpers over tagged news articles.

Adapted from ConsumerIntelligence_PR for news-article data:
- engagement() falls back to reach (news has no social engagement)
- source_platform() uses section instead of source_type
- signal field derived from subtheme/theme (no social signal field)
"""

from collections import defaultdict
from datetime import datetime

UNKNOWN_DAY = "Unknown"

JUNK_LABELS = {
    "other", "none", "n/a", "na", "unknown", "general",
    "miscellaneous", "null", "-", "unspecified",
}

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%B %d, %Y",
    "%b %d, %Y",
    "%d %B %Y",
)

_PLAUSIBLE_YEARS = range(1995, 2100)


def is_junk(value) -> bool:
    return str(value).strip().lower() in JUNK_LABELS


def usable(articles: list[dict]) -> tuple[list[dict], int]:
    kept = [a for a in articles if not a.get("tagging_error")]
    return kept, len(articles) - len(kept)


def numeric(article: dict, *fields: str) -> float:
    for field in fields:
        try:
            value = float(article.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0.0


def engagement(article: dict) -> float:
    """For news articles, reach is the engagement proxy."""
    return numeric(article, "engagement", "reach", "views", "likes", "shares")


def reach(article: dict) -> float:
    return numeric(article, "reach", "views")


def source_platform(article: dict) -> str:
    """The article's own source platform (Forums, Instagram, Amazon UK...) when the
    upload carried one, else the section label as a proxy for news articles."""
    return str(
        article.get("source_type")
        or article.get("content source name")
        or article.get("section")
        or article.get("domain")
        or "News"
    ).strip()


def signal_label(article: dict) -> str | None:
    """Signal derived from subtheme (primary) or theme for news articles."""
    for field in ("signal", "subtheme", "theme"):
        value = article.get(field)
        if value and not is_junk(str(value)):
            return str(value).strip()
    return None


def count_by(articles: list[dict], field: str, *, skip_junk: bool = False) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for article in articles:
        value = article.get(field)
        if value is None or value == "":
            continue
        if skip_junk and is_junk(value):
            continue
        counts[str(value).strip()] += 1
    return dict(counts)


def share_by(articles: list[dict], field: str) -> dict[str, float]:
    counts = count_by(articles, field)
    total = sum(counts.values())
    if not total:
        return {}
    return {key: round(value * 100 / total, 1) for key, value in counts.items()}


def net_sentiment(articles: list[dict]) -> float:
    rated = [a for a in articles if a.get("sentiment")]
    if not rated:
        return 0.0
    positive = sum(1 for a in rated if a["sentiment"] == "Positive")
    negative = sum(1 for a in rated if a["sentiment"] == "Negative")
    return round((positive - negative) * 100 / len(rated), 1)


def brands_in(article: dict, known: list[str]) -> set[str]:
    canonical = {name.lower(): name for name in known if name}
    found: set[str] = set()
    raw: list[str] = []
    brand = article.get("brand_of_interest")
    if isinstance(brand, (list, tuple, set)):
        raw.extend(str(v) for v in brand if v)
    elif brand:
        raw.append(str(brand))
    for field in ("competitors", "other_competitors"):
        values = article.get(field) or []
        if isinstance(values, str):
            values = [values]
        raw.extend(str(v) for v in values if v)
    for name in raw:
        cleaned = name.strip()
        if cleaned:
            found.add(canonical.get(cleaned.lower(), cleaned))
    return found


def parse_day(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d") if value.year in _PLAUSIBLE_YEARS else None
    text = str(value).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.year not in _PLAUSIBLE_YEARS:
            return None
        return parsed.strftime("%Y-%m-%d")
    # The platform's tagged_articles store ISO-8601 with milliseconds and a
    # timezone offset ("2023-12-31T16:24:00.000+00:00"); strptime has no
    # single pattern for that, so fall back to fromisoformat.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.year not in _PLAUSIBLE_YEARS:
        return None
    return parsed.strftime("%Y-%m-%d")


def bucket_by_day(articles: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for article in articles:
        buckets[parse_day(article.get("date")) or UNKNOWN_DAY].append(article)
    return dict(buckets)


def top_n(counts: dict[str, int], n: int) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
