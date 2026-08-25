from datetime import datetime, timedelta, timezone
import re
from typing import Optional
from configs import logger, envs
from file_helpers.cleaing_data import _to_iso_date


def filter_recent_articles(articles: list[dict], recency_hours: int) -> list[dict]:
    """Drop articles published more than `hours` ago. Articles whose date can't be
    parsed are kept (the `when:` query already scoped the window).

    Uses `_to_iso_date` (the tagging pipeline's date normalizer) to turn the
    article's raw date — ISO 8601 or RFC 1123 — into a comparable UTC datetime.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=envs.DEFAULT_RSS_RECENCY_HOURS)
    kept = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        # _to_iso_date returns a tz-aware ISO string when it can parse the date,
        # or the original (unparseable) string otherwise — which fromisoformat rejects.
        iso = _to_iso_date(art.get("date"))
        dt: Optional[datetime] = None
        try:
            dt = datetime.fromisoformat(iso) if iso else None
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
        kept.append(art)
    dropped = len(articles) - len(kept)
    if dropped:
        logger.info(f"Filtered out {dropped} RSS article(s) older than {envs.DEFAULT_RSS_RECENCY_HOURS}h.")
    return kept


def parse_boolean_query(query: str) -> list[str]:
    """ Split a boolean query into individual terms/phrases, deduplicating while preserving order."""
    query = query.replace("(", " ").replace(")", " ")

    segments = re.split(r'\b(?:AND|OR|NOT)\b', query)

    # Strip whitespace and any wrapping quotes from each segment.
    terms = []
    for seg in segments:
        term = seg.strip().strip('"').strip()
        if term:
            terms.append(term)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            result.append(t)

    return result


def find_matched_keywords(query: str, title: str | None, content: str | None) -> list[str]:
    """
    Return the query's terms that actually appear in the article.
    """
    terms = parse_boolean_query(query or "")
    if not terms:
        return []
    haystack = f"{title or ''} {content or ''}"
    matched: list[str] = []
    for term in terms:
        pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
        try:
            found = re.search(pattern, haystack, re.IGNORECASE) is not None
        except re.error:
            found = term.lower() in haystack.lower()
        if found:
            matched.append(term)
    return matched