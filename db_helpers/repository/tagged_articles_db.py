import math

from typing import Any, Optional

from sqlalchemy.orm import Session

from db_helpers.models.raw_article_model import RawArticleModel
from db_helpers.models.tagged_article_model import TaggedArticleModel
from db_helpers.repository.raw_articles_db import _article_id_from_url


def _raw_article_id_map(db: Session, session_id: int) -> dict[str, str]:
    """{normalized_url -> article_id} from the session's raw_articles rows, so a
    tagged article inherits the SAME article_id its raw counterpart was given."""
    rows = (
        db.query(RawArticleModel.url, RawArticleModel.article_id)
        .filter(RawArticleModel.session_id == session_id)
        .all()
    )
    mapping: dict[str, str] = {}
    for url, article_id in rows:
        if url and article_id:
            mapping[url.strip().rstrip("/")] = article_id
    return mapping


def _resolve_article_id(article: dict[str, Any], raw_map: Optional[dict[str, str]] = None) -> Optional[str]:
    """Article_id for a tagged article: take it from the raw_articles table (matched
    by URL) when available, otherwise fall back to the same URL hash — this covers
    manually-added articles that were never in the raw set."""
    url = article.get("url")
    if raw_map and isinstance(url, str) and url.strip():
        hit = raw_map.get(url.strip().rstrip("/"))
        if hit:
            return hit
    return _article_id_from_url(url)


# Postgres bigint bounds; a value past these aborts the whole insert batch.
_BIGINT_MAX = 2**63 - 1
_BIGINT_MIN = -(2**63)


def _to_int(value: Any) -> Optional[int]:
    """Coerce a reach-like value to int, tolerating None / "" / floats / junk.

    Args:
        value: Raw reach value.

    Returns:
        Int clamped to Postgres bigint range, or None when unusable.
    """
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return max(_BIGINT_MIN, min(_BIGINT_MAX, int(number)))


def _str_or_none(value: Any) -> Optional[str]:
    """A string promoted-column value, treating NaN/Inf floats (from empty cells in
    re-uploaded files) as None so they don't land as the literal 'nan'."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _apply_article(
    row: TaggedArticleModel,
    session_id: int,
    article: dict[str, Any],
    article_id: Optional[str] = None,
) -> TaggedArticleModel:
    """Populate a row's promoted columns + canonical `data` from an article dict."""
    row.session_id = session_id
    row.article_id = article_id
    row.article_ref = str(article.get("id") or "")
    row.title = _str_or_none(article.get("title"))
    row.url = _str_or_none(article.get("url"))
    row.date = _str_or_none(article.get("date")) or None
    row.sentiment = _str_or_none(article.get("sentiment"))
    row.theme = _str_or_none(article.get("theme"))
    row.section = _str_or_none(article.get("section"))
    row.reach = _to_int(article.get("reach"))
    row.priority_watch = bool(article.get("priority_watch") or False)
    row.is_approved_for_dashboards = bool(article.get("is_approved_for_dashboards") or False)
    row.is_approved_for_monitoring = bool(article.get("is_approved_for_monitoring") or False)
    row.data = article
    return row


def _row_from_article(
    session_id: int, article: dict[str, Any], article_id: Optional[str] = None
) -> TaggedArticleModel:
    return _apply_article(TaggedArticleModel(), session_id, article, article_id)


def replace_tagged_articles(
    db: Session, session_id: int, articles: list[dict[str, Any]]
) -> list[TaggedArticleModel]:
    """Replace all tagged articles for a session (re-tagging overwrites).

    Each tagged article's ``article_id`` is inherited from the matching raw article
    (by URL), falling back to the URL hash for manually-added articles."""
    db.query(TaggedArticleModel).filter(
        TaggedArticleModel.session_id == session_id
    ).delete()
    raw_map = _raw_article_id_map(db, session_id)
    rows = [
        _row_from_article(session_id, a, _resolve_article_id(a, raw_map))
        for a in articles
        if isinstance(a, dict)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def next_article_ref_number(db: Session, session_id: int) -> int:
    """The next free "A{n}" number for a session, so a new batch of tagged
    articles continues the numbering instead of colliding with existing rows.

    Args:
        db: Database session.
        session_id: Session to inspect.

    Returns:
        1 when the session has no tagged rows, else the highest ref number + 1.
    """
    refs = (
        db.query(TaggedArticleModel.article_ref)
        .filter(TaggedArticleModel.session_id == session_id)
        .all()
    )
    highest = 0
    for (ref,) in refs:
        if isinstance(ref, str) and ref.startswith("A") and ref[1:].isdigit():
            highest = max(highest, int(ref[1:]))
    return highest + 1


def add_tagged_articles(
    db: Session, session_id: int, articles: list[dict[str, Any]]
) -> list[TaggedArticleModel]:
    """Append newly tagged articles, leaving the session's existing rows in place.

    Args:
        db: Database session.
        session_id: Session the articles belong to.
        articles: Freshly tagged article dicts.

    Returns:
        The inserted rows.
    """
    raw_map = _raw_article_id_map(db, session_id)
    rows = [
        _row_from_article(session_id, a, _resolve_article_id(a, raw_map))
        for a in articles
        if isinstance(a, dict)
    ]
    db.add_all(rows)
    db.commit()
    return rows


def get_tagged_articles(db: Session, session_id: int) -> list[dict[str, Any]]:
    """Return tagged articles for a session as list[dict] — the exact shape the
    charts calculators and the E2B sandbox consume."""
    rows = (
        db.query(TaggedArticleModel)
        .filter(TaggedArticleModel.session_id == session_id)
        .order_by(TaggedArticleModel.id)
        .all()
    )
    return [row.data for row in rows]


def get_tagged_article(
    db: Session, session_id: int, article_ref: str
) -> TaggedArticleModel | None:
    return (
        db.query(TaggedArticleModel)
        .filter(
            TaggedArticleModel.session_id == session_id,
            TaggedArticleModel.article_ref == article_ref,
        )
        .first()
    )


def upsert_tagged_article(
    db: Session, session_id: int, article: dict[str, Any]
) -> TaggedArticleModel:
    """Insert or update one article's row from its full dict (used by the
    review/edit endpoints in place of rewriting the whole tagged file)."""
    raw_map = _raw_article_id_map(db, session_id)
    article_id = _resolve_article_id(article, raw_map)
    row = get_tagged_article(db, session_id, str(article.get("id") or ""))
    if row is None:
        row = _row_from_article(session_id, article, article_id)
        db.add(row)
    else:
        _apply_article(row, session_id, article, article_id)
    db.commit()
    db.refresh(row)
    return row


def delete_tagged_article(db: Session, session_id: int, article_ref: str) -> bool:
    """Delete a single tagged article by its "A1" ref. Returns True if a row was
    removed."""
    deleted = (
        db.query(TaggedArticleModel)
        .filter(
            TaggedArticleModel.session_id == session_id,
            TaggedArticleModel.article_ref == article_ref,
        )
        .delete()
    )
    db.commit()
    return bool(deleted)


def count_tagged_articles(db: Session, session_id: int) -> int:
    return (
        db.query(TaggedArticleModel)
        .filter(TaggedArticleModel.session_id == session_id)
        .count()
    )


def delete_tagged_articles(db: Session, session_id: int) -> None:
    db.query(TaggedArticleModel).filter(
        TaggedArticleModel.session_id == session_id
    ).delete()
    db.commit()
