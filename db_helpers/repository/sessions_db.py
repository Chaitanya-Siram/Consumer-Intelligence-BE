from datetime import datetime
from typing import Any
from sqlalchemy import func
from sqlalchemy.orm import Session
from configs import logger
from db_helpers.models.session_model import SessionModel


def get_session(db: Session, session_id: int) -> SessionModel | None:
    return db.query(SessionModel).filter(SessionModel.id == session_id).first()


def list_sessions_by_project(db: Session, project_id: int) -> list[SessionModel]:
    return (
        db.query(SessionModel)
        .filter(SessionModel.project_id == project_id)
        .order_by(SessionModel.id.desc())
        .all()
    )


def delete_session(db: Session, session: SessionModel) -> None:
    db.delete(session)
    db.commit()

def create_session(
    db: Session,
    project_id: int,
    created_by_id: int,
    name: str,
    brand_keywords: list[str],
    competitor_keywords: list[str],
    message_keywords: list[str],
    workflow: Any | None = None,
) -> SessionModel:
    """Create a session under a project.

    Args:
        db: Database session.
        project_id: Project the session belongs to.
        created_by_id: user_org_mapping id of the creator.
        name: Session name.
        brand_keywords: Brand keywords collected from the workflow.
        competitor_keywords: Competitor keywords collected from the workflow.
        message_keywords: Message keywords collected from the workflow.
        workflow: Validated workflow graph.

    Returns:
        The created session.
    """
    new_session = SessionModel(
        project_id=project_id,
        created_by_id=created_by_id,
        name=name,
        brand_keywords=brand_keywords,
        competitor_keywords=competitor_keywords,
        message_keywords=message_keywords,
        workflow=workflow,
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    return new_session


def update_session_charts_data_file(db: Session, session: SessionModel, charts_data_file: str) -> SessionModel:
    session.charts_data_file = charts_data_file
    session.status = "Completed"
    db.commit()
    db.refresh(session)
    return session


def invalidate_session_charts(db: Session, session: SessionModel) -> SessionModel:
    """Drop any cached charts (e.g. after the tagged articles are edited) so the
    dashboards regenerate next time. Leaves the tagged file untouched and rolls a
    "Completed" session back to "Tagged"."""
    session.charts_data_file = None
    if session.status == "Completed":
        session.status = "Tagged"
    db.commit()
    db.refresh(session)
    return session


def drop_cached_charts(db: Session, session: SessionModel) -> SessionModel:
    """Delete the session's cached charts file from S3 and clear it on the row.

    The S3 delete is best-effort — a stale object left behind is harmless once the
    row no longer points at it, and must never fail the caller's own work."""
    if session.charts_data_file:
        try:
            from file_helpers.s3_file import s3_file

            s3_file.delete_file(session.charts_data_file)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to delete stale charts file; continuing.")
    return invalidate_session_charts(db, session)


def update_session_status(db: Session, session: SessionModel, status: str) -> SessionModel:
    session.status = status
    db.commit()
    db.refresh(session)
    return session


def set_relevancy_prompt(
    db: Session,
    session: SessionModel,
    relevancy_prompt: str | None,
    relevancy_domains: dict[str, list[str]] | None = None,
) -> SessionModel:
    """Set (or clear) the criteria the relevancy gate applies before tagging.

    Args:
        db: Open session.
        session: The session row to update.
        relevancy_prompt: The criteria text, or None to clear it.
        relevancy_domains: ``{"include": [...], "exclude": [...]}`` extracted from
            the prompt; written alongside it so the two never disagree.

    Returns:
        The refreshed session.
    """
    session.relevancy_prompt = relevancy_prompt
    session.relevancy_domains = relevancy_domains
    db.commit()
    db.refresh(session)
    return session


def update_session_workflow(
    db: Session,
    session: SessionModel,
    workflow: Any | None,
    keywords: dict[str, list[str]],
    has_tagged_articles: bool = False,
) -> tuple[SessionModel, bool]:
    """Persist the visual pipeline graph and the keywords derived from it.

    Args:
        db: Database session.
        session: The session row to update.
        workflow: Validated workflow graph.
        keywords: Output of collect_keywords(workflow).
        has_tagged_articles: Whether the session already has tagged rows.

    Returns:
        The refreshed session, and whether a retag is now pending.
    """
    # Only brand/competitor keywords reach the tagging prompt and tool schema, so
    # only they can invalidate existing tags. Compared as sets: reordering the
    # same keywords is not a change.
    keywords_changed = (
        set(session.brand_keywords or []) != set(keywords["brand_keywords"])
        or set(session.competitor_keywords or []) != set(keywords["competitor_keywords"])
    )

    session.brand_keywords = keywords["brand_keywords"]
    session.competitor_keywords = keywords["competitor_keywords"]
    session.message_keywords = keywords["message_keywords"]
    session.workflow = workflow

    # Stamped by the database clock, so it is comparable with the equally naive
    # tagged_articles.created_at. An unrelated save leaves a pending stamp alone.
    if keywords_changed and has_tagged_articles:
        session.retag_after = func.now()

    # "Workflow Saved" would misreport an already-tagged session as unprocessed.
    if not has_tagged_articles:
        session.status = "Workflow Saved"

    db.commit()
    db.refresh(session)
    return session, session.retag_after is not None


def get_retag_marker(session: SessionModel) -> datetime | None:
    """When this session was marked for retag, or None when none is pending."""
    return session.retag_after


def clear_retag_marker(db: Session, session: SessionModel) -> SessionModel:
    """Drop the pending-retag marker once a retag run has completed."""
    if session.retag_after is not None:
        session.retag_after = None
        db.commit()
        db.refresh(session)
    return session