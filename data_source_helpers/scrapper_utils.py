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


def split_boolean_query(query: str) -> dict[str, list[str]]:
    """Split a boolean query into its OR / AND / NOT terms.

    For platforms whose search takes a plain keyword instead of a boolean
    expression: the OR terms are searched one at a time, then AND/NOT are applied
    to the results locally. A term with no operator before it counts as an OR term.

    Args:
        query: Boolean query string, e.g. '("Acme" OR Acme Corp) AND launch NOT hiring'.

    Returns:
        Dict with "or", "and" and "not" term lists.
    """
    cleaned = (query or "").replace("(", " ").replace(")", " ")
    # Keep the operators so each term can be attributed to the one preceding it.
    tokens = re.split(r"\b(AND|OR|NOT)\b", cleaned)

    groups: dict[str, list[str]] = {"or": [], "and": [], "not": []}
    seen: dict[str, set[str]] = {"or": set(), "and": set(), "not": set()}
    bucket = "or"
    for token in tokens:
        upper = token.strip().upper()
        if upper in ("AND", "OR", "NOT"):
            bucket = upper.lower()
            continue
        term = token.strip().strip('"').strip()
        if not term:
            continue
        key = term.lower()
        if key in seen[bucket]:
            continue
        seen[bucket].add(key)
        groups[bucket].append(term)
    return groups


def contains_term(term: str, haystack: str) -> bool:
    """Whether a term appears in the text as a whole word.

    Args:
        term: Term or phrase to look for.
        haystack: Text to search.

    Returns:
        True when the term is present.
    """
    pattern = r"(?<!\w)" + re.escape(term) + r"(?!\w)"
    try:
        return re.search(pattern, haystack, re.IGNORECASE) is not None
    except re.error:
        return term.lower() in haystack.lower()


def matches_boolean_query(groups: dict[str, list[str]], title: str | None, content: str | None) -> bool:
    """Apply the AND / NOT parts of a split query to one article.

    The OR terms are what was searched for, so they are not re-checked here.

    Args:
        groups: Output of split_boolean_query.
        title: Article title.
        content: Article body/description.

    Returns:
        True when the article satisfies every AND term and no NOT term.
    """
    haystack = f"{title or ''} {content or ''}"
    if any(contains_term(t, haystack) for t in groups.get("not", [])):
        return False
    return all(contains_term(t, haystack) for t in groups.get("and", []))


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