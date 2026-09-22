import codecs
import csv
import io
import json
from typing import Any
import pandas as pd

SUPPORTED_EXTENSIONS = {"csv", "xlsx", "xls", "json"}


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _decode_text(content: bytes) -> str:
    """Bytes of an uploaded text file -> str, honouring the byte order mark.

    Excel's "Unicode Text" / Meltwater exports are UTF-16 LE with a FF FE mark;
    pandas' default (UTF-8) fails on them with "can't decode byte 0xff in
    position 0". Then UTF-8 with or without BOM, then Windows-1252 (Excel's
    plain "CSV" on a Western locale), which accepts any byte sequence.
    """
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return content.decode("utf-16")
    if content.startswith(codecs.BOM_UTF8):
        return content.decode("utf-8-sig")
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _sniff_delimiter(text: str) -> str:
    """Comma, tab, semicolon or pipe: whichever the header row uses.
    UTF-16 Excel exports are tab-separated despite the .csv extension."""
    header = text.lstrip("\ufeff").split("\n", 1)[0]
    try:
        return csv.Sniffer().sniff(header, delimiters=",\t;|").delimiter
    except csv.Error:
        counts = {d: header.count(d) for d in (",", "\t", ";", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


def _read_csv(content: bytes) -> pd.DataFrame:
    text = _decode_text(content)
    return pd.read_csv(io.StringIO(text), sep=_sniff_delimiter(text))


def parse_upload(filename: str, content: bytes) -> list[dict[str, Any]]:
    """Parse a CSV/Excel/JSON file into a list of records with normalized keys.

    All keys are lowercased; known column variants (title/content/date/audience/
    engagement/url) are renamed to their canonical form. The JSON file may be
    either a list of objects or a dict containing a list under one of the
    common keys (data, articles, items, results).
    """
    ext = _ext(filename)
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type '.{ext}'. Use one of: {sorted(SUPPORTED_EXTENSIONS)}")

    if ext == "csv":
        df = _read_csv(content)
        json_data = df_to_records(df)
        updated_data = update_columns_name(json_data)
        filtered_data = filter_articles_with_date(updated_data)
        return filtered_data

    if ext in {"xlsx", "xls"}:
        df = pd.read_excel(io.BytesIO(content))
        json_data = df_to_records(df)
        updated_data = update_columns_name(json_data)
        filtered_data = filter_articles_with_date(updated_data)
        return filtered_data

    # json
    try:
        payload = json.loads(content.decode("utf-8"))
    except UnicodeDecodeError:
        payload = json.loads(content.decode("utf-8-sig"))

    if isinstance(payload, list):
        return filter_articles_with_date(update_columns_name([r for r in payload if isinstance(r, dict)]))
    if isinstance(payload, dict):
        for key in ("data", "articles", "items", "results", "records"):
            if key in payload and isinstance(payload[key], list):
                return filter_articles_with_date(update_columns_name([r for r in payload[key] if isinstance(r, dict)]))
        return filter_articles_with_date(update_columns_name([payload]))
    raise ValueError("JSON payload must be a list of objects or a dict containing one.")


def df_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert a DataFrame to records, coercing pandas/numpy Timestamps to ISO
    strings so downstream code can treat date fields as plain strings (the
    charts rely on `date[:10]` to get the YYYY-MM-DD prefix)."""
    df = df.where(pd.notnull(df), None)
    records = df.to_dict(orient="records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, pd.Timestamp):
                record[key] = value.isoformat()
            elif not isinstance(value, str) and hasattr(value, "isoformat"):
                # datetime.date / datetime.datetime from JSON decoders, etc.
                record[key] = value.isoformat()
    return records


TITLE_FIELD_CANDIDATES = ("title", "headline", "subject")

TEXT_FIELD_CANDIDATES = ("article", "article_text", "content", "body", "text", "description", "story", "summary", "opening text")

PUB_DATE_FIELD_CANDIDATES = ("pub_date", "publication_date", "date", "created_at", "timestamp", "publisheddate", "publish date", "pubdate", "date published")

FOLLOWERS_FIELD_CANDIDATES = ("followers", "followers_count", "audience")

ENGAGEMENT_FIELD_CANDIDATES = ("engagement", "engagements", "engagement_count", "interactions", "interaction")

URL_FIELD_CANDIDATES = ("url", "link", "article_url", "post_url", "post url", "post link")

MEDIA_TYPE_FIELD_CANDIDATES = ("media_type", "type", "channel", "media type", "channel type", "media_types", "media types")

AUTHOR_FIELD_CANDIDATES = ("author", "influencer", "creator", "author name", "author_name", "writer", "authors", "authorbyline", "journalist/author", "journalist")


_STANDARD_FIELD_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title", TITLE_FIELD_CANDIDATES),
    ("content", TEXT_FIELD_CANDIDATES),
    ("date", PUB_DATE_FIELD_CANDIDATES),
    ("audience", FOLLOWERS_FIELD_CANDIDATES),
    ("engagement", ENGAGEMENT_FIELD_CANDIDATES),
    ("url", URL_FIELD_CANDIDATES),
    ("media_type", MEDIA_TYPE_FIELD_CANDIDATES),
    ("author", AUTHOR_FIELD_CANDIDATES),
)

def rename_record(record: dict[str, Any]) -> dict[str, Any]:
    """Lowercase every key in `record`, rename known variants (title/content/
    date/audience/engagement/url) to their canonical names. On case collisions
    the first occurrence wins so original ordering is preserved."""
    lower_keys = {k.lower(): k for k in record.keys() if isinstance(k, str)}

    # Build a map of lowercased original key -> canonical name (for matched candidates).
    canonical_map: dict[str, str] = {}
    for standard_name, candidates in _STANDARD_FIELD_MAP:
        for cand in candidates:
            if cand in lower_keys:
                canonical_map[cand] = standard_name
                break

    out: dict[str, Any] = {}
    for k, v in record.items():
        if not isinstance(k, str):
            out[k] = v
            continue
        lk = k.lower()
        new_key = canonical_map.get(lk, lk)
        if new_key in out:
            continue  # collision (e.g. both 'Title' and 'title') — first wins
        out[new_key] = v
    return out


def update_columns_name(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize every record's keys: lowercase + rename known variants to
    canonical names. Returns a new list; non-dict entries are skipped."""
    return [rename_record(r) for r in records if isinstance(r, dict)]


def filter_articles_with_date(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop records that don't have a usable `date` value. Run AFTER
    update_columns_name so the date variants have already been renamed to the
    canonical `date` key."""
    filtered_articles = []
    for item in records:
        if not isinstance(item, dict):
            continue
        date_val = item.get("date")
        if isinstance(date_val, str) and date_val.strip():
            filtered_articles.append(item)
        if item.get("title") is None and item.get("content") is None:
            continue  # skip records that have no title or content after filtering
        elif len(item.get("title", "") or "") == 0 and len(item.get("content", "") or "") == 0:
            continue
    return filtered_articles
