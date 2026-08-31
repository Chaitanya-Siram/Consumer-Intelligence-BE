import hashlib
import uuid
from typing import Any, Optional

from sqlalchemy.orm import Session

from db_helpers.models.raw_article_model import RawArticleModel
from db_helpers.models.tagged_article_model import TaggedArticleModel
from file_helpers.cleaing_data import _to_iso_date, raw_date_of, to_datetime


def _article_id_from_url(url: Any) -> Optional[str]:
    """SHA-256 of the article URL with any trailing slash stripped (None if no URL)."""
    if not isinstance(url, str):
        return None
    normalized = url.strip().rstrip("/")
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _row_from_record(
    project_id: int,
    session_id: Optional[int],
    record: Any,
    file_upload_id: Optional[str] = None,
) -> RawArticleModel:
    if not isinstance(record, dict):
        return RawArticleModel(
            project_id=project_id,
            session_id=session_id,
            file_upload_id=file_upload_id,
            data=record,
        )
    raw_url = record.get("url")
    url = raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None
    return RawArticleModel(
        project_id=project_id,
        session_id=session_id,
        file_upload_id=file_upload_id,
        article_id=_article_id_from_url(url),
        url=url,
        date=to_datetime(raw_date_of(record)),
        data=record,
    )


def add_new_raw_articles(
    db: Session, project_id: int, session_id: int, records: list[dict[str, Any]]
) -> list[RawArticleModel]:
    """Append only the records this session hasn't stored yet, keyed on article_id.

    Args:
        db: Database session.
        project_id: Project the session belongs to.
        session_id: Session the articles belong to.
        records: Fetched article records.

    Returns:
        The rows that were newly inserted.
    """
    existing_ids = {
        article_id
        for (article_id,) in db.query(RawArticleModel.article_id)
        .filter(RawArticleModel.session_id == session_id)
        .all()
        if article_id is not None
    }

    rows: list[RawArticleModel] = []
    for record in records:
        row = _row_from_record(project_id, session_id, record)
        # Records without a usable url have no article_id to dedupe on; skip them.
        if row.article_id is None or row.article_id in existing_ids:
            continue
        existing_ids.add(row.article_id)
        rows.append(row)

    db.add_all(rows)
    db.commit()
    return rows


def new_file_upload_id() -> str:
    """Generate a unique id identifying one file upload.

    Returns:
        A random 32-character hex id.
    """
    return uuid.uuid4().hex


def add_upload_raw_articles(
    db: Session, project_id: int, file_upload_id: str, records: list[dict[str, Any]]
) -> list[RawArticleModel]:
    """Store an upload's parsed records as raw articles under `file_upload_id`.

    Args:
        db: Database session.
        project_id: Project this upload belongs to.
        file_upload_id: Id identifying this upload.
        records: Parsed article records.

    Returns:
        The inserted rows.
    """
    rows = [_row_from_record(project_id, None, record, file_upload_id) for record in records]
    db.add_all(rows)
    db.commit()
    return rows


def file_upload_exists(db: Session, file_upload_id: str) -> bool:
    """Whether any raw article rows were stored under this upload id."""
    return (
        db.query(RawArticleModel.id)
        .filter(RawArticleModel.file_upload_id == file_upload_id)
        .first()
        is not None
    )


def assign_uploads_to_session(
    db: Session, session_id: int, file_upload_ids: list[str]
) -> int:
    """Attach the rows stored under these upload ids to a session.

    Args:
        db: Database session.
        session_id: Session that now owns the uploaded articles.
        file_upload_ids: Upload ids from the workflow's file data nodes.

    Returns:
        The number of rows updated.
    """
    if not file_upload_ids:
        return 0
    updated = (
        db.query(RawArticleModel)
        .filter(
            RawArticleModel.file_upload_id.in_(file_upload_ids),
            RawArticleModel.session_id.is_(None),
        )
        .update({RawArticleModel.session_id: session_id}, synchronize_session=False)
    )
    db.commit()
    return updated


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


def get_untagged_raw_articles(db: Session, session_id: int) -> list[dict[str, Any]]:
    """Return the session's raw articles that have no tagged row yet.

    Matched on article_id (the url hash both tables share). Rows without an
    article_id can't be matched, so they are always returned.

    Args:
        db: Database session.
        session_id: Session to read.

    Returns:
        The untagged raw article records, oldest first.
    """
    tagged_ids = {
        article_id
        for (article_id,) in db.query(TaggedArticleModel.article_id)
        .filter(TaggedArticleModel.session_id == session_id)
        .all()
        if article_id is not None
    }
    rows = (
        db.query(RawArticleModel)
        .filter(RawArticleModel.session_id == session_id)
        .order_by(RawArticleModel.id)
        .all()
    )
    return [row.data for row in rows if row.article_id not in tagged_ids]


def count_raw_articles(db: Session, session_id: int) -> int:
    return (
        db.query(RawArticleModel)
        .filter(RawArticleModel.session_id == session_id)
        .count()
    )


def delete_raw_articles(db: Session, session_id: int) -> None:
    db.query(RawArticleModel).filter(RawArticleModel.session_id == session_id).delete()
    db.commit()
