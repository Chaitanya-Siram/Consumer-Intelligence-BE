from datetime import datetime, timezone
import re
from typing import Any
from configs import logger
from file_helpers.publication_helper import publication_name

# ==================================
#  Date Formating ==================
# ==================================
DATE_FORMATS = (
    "%d-%m-%y %H:%M",
    "%m/%d/%Y, %I:%M %p, %z UTC",
    "%m/%d/%Y, %I:%M %p, %z",
    "%m/%d/%Y, %I:%M %p UTC",
    "%m/%d/%Y, %I:%M %p",
    "%m/%d/%Y",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%a, %d %b %Y %H:%M:%S %Z",
)

def _to_iso_date(raw: Any) -> str:
    """Normalise a SerpAPI date to ISO 8601 (UTC, millisecond precision). Returns the
    original string if it can't be parsed, and "" for empty input."""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    # Already ISO-ish?
    try:
        return _iso(datetime.fromisoformat(raw))
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return _iso(datetime.strptime(raw, fmt))
        except ValueError:
            continue
    logger.debug(f"Could not parse SerpAPI date {raw!r}; leaving as-is")
    return raw


def _iso(dt: datetime) -> str:
    """A tz-aware/naive datetime → UTC ISO 8601 with milliseconds (naive = assumed UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def clean_articles(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty rows, normalize whitespace/HTML, assign A0..An ids.

    Each output record preserves the original fields plus:
      - id: "A{i}"
      - article_text: the cleaned primary body text used for tagging
      - title: cleaned title if present
    """
    cleaned: list[dict[str, Any]] = []
    next_id = 1
    for record in records:
        if not isinstance(record, dict):
            continue

        title = record.get("title")
        content = record.get("content")

        if not title and not content:
            continue

        out = {k: v for k, v in record.items()}
        out["id"] = f"A{next_id}"

        # Domain extraction
        url = record.get("url")
        
        if isinstance(url, str) and url.strip():
            out["domain"] = get_domain(url)
            out["domain_name"] = publication_name.get_publication_name_for_domain(out["domain"])

        # Combine date and time
        if "date" in out and len(out["date"]) == 8 and "time" in out:
            out["date"] = f'{out["date"]} {out["time"]}'

        # Date Formating
        out["date"] = _to_iso_date(out["date"])

        cleaned.append(out)
        next_id += 1

    return cleaned


def get_domain(url: str) -> str:
    """Extract domain from URL for better tagging."""
    cleaned_domain = re.sub(r'^https?://(www\.)?', '', url).split('/')[0].lower()
    return cleaned_domain


_CONFIDENCE_FIELDS = ("sentiment_confidence", "theme_confidence", "section_category_confidence")


def reorder_by_confidence(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reorder tagged articles by tagging confidence (highest first) and re-assign
    sequential A1..An ids.

    Confidence is the mean of the per-field confidences (sentiment / theme / section
    category) that are present. All original fields are preserved; only `id` is
    rewritten so it matches the new order. Records with no numeric confidence sort to
    the end. Ties keep their original relative order (stable sort). Inputs not mutated.
    """
    def _conf(record: dict[str, Any]) -> float:
        vals = []
        for key in _CONFIDENCE_FIELDS:
            try:
                v = record.get(key)
                if v is not None:
                    vals.append(float(v))
            except (TypeError, ValueError):
                continue
        return sum(vals) / len(vals) if vals else float("-inf")

    ordered = sorted(
        (r for r in records if isinstance(r, dict)),
        key=_conf,
        reverse=True,
    )

    # Reassign ids, tracking old→new so any relation pointers (set before this
    # reorder, e.g. by link_articles running pre-tagging) still resolve correctly.
    id_map: dict[str, str] = {}
    result: list[dict[str, Any]] = []
    for i, record in enumerate(ordered, start=1):
        new_id = f"A{i}"
        old_id = record.get("id")
        if old_id is not None:
            id_map[old_id] = new_id
        result.append({**record, "id": new_id})

    for record in result:
        for field in ("syndication_of", "similar_of"):
            ref = record.get(field)
            if ref:
                record[field] = id_map.get(ref, "")
    return result

