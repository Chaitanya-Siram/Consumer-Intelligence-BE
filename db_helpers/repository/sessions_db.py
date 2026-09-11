from typing import Any
from sqlalchemy.orm import Session
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


def update_session_workflow(db: Session, session: SessionModel, workflow: Any | None) -> SessionModel:
    """Persist the visual pipeline graph (nodes + edges) for a session."""
    session.workflow = workflow
    session.status = "Workflow Saved"
    db.commit()
    db.refresh(session)
    return session