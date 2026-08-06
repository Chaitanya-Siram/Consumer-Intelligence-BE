import hashlib
from typing import Any, Optional

from sqlalchemy.orm import Session

from db_helpers.models.raw_article_model import RawArticleModel
from file_helpers.cleaing_data import _to_iso_date


def _article_id_from_url(url: Any) -> Optional[str]:
    """SHA-256 of the article URL with any trailing slash stripped (None if no URL)."""
    if not isinstance(url, str):
        return None
    normalized = url.strip().rstrip("/")
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _clean_date(record: dict[str, Any]) -> Optional[str]:
    """Normalize the record's date to ISO-8601 using the same logic as
    clean_articles (combine an 8-char date with a separate time, then _to_iso_date)."""
    date = record.get("date")
    if isinstance(date, str) and len(date) == 8 and record.get("time"):
        date = f"{date} {record.get('time')}"
    return _to_iso_date(date) or None


def _row_from_record(session_id: int, record: Any) -> RawArticleModel:
    if not isinstance(record, dict):
        return RawArticleModel(session_id=session_id, data=record)
    raw_url = record.get("url")
    url = raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None
    return RawArticleModel(
        session_id=session_id,
        article_id=_article_id_from_url(url),
        url=url,
        date=_clean_date(record),
        data=record,
    )


def replace_raw_articles(
    db: Session, session_id: int, records: list[dict[str, Any]]
) -> list[RawArticleModel]:
    """Replace all raw articles for a session with `records` (one row each).

    Mirrors update_session_source_file's "a fresh source resets everything"
    semantics: any previously stored raw rows for this session are cleared first.
    """
    db.query(RawArticleModel).filter(RawArticleModel.session_id == session_id).delete()
    rows = [_row_from_record(session_id, record) for record in records]
    db.add_all(rows)
    db.commit()
    return rows


def get_raw_articles(db: Session, session_id: int) -> list[dict[str, Any]]:
    """Return the raw article records for a session as a plain list[dict] — the
    shape the tagging pipeline previously got from parse_upload()."""
    rows = (
        db.query(RawArticleModel)
        .filter(RawArticleModel.session_id == session_id)
        .order_by(RawArticleModel.id)
        .all()
    )
    return [row.data for row in rows]


def count_raw_articles(db: Session, session_id: int) -> int:
    return (
        db.query(RawArticleModel)
        .filter(RawArticleModel.session_id == session_id)
        .count()
    )


def delete_raw_articles(db: Session, session_id: int) -> None:
    db.query(RawArticleModel).filter(RawArticleModel.session_id == session_id).delete()
    db.commit()
