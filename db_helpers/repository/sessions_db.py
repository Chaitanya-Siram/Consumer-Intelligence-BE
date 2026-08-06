from typing import Any
from sqlalchemy.orm import Session
from db_helpers.models.session_model import SessionModel, SessionType

# Marker stored in ``source_file`` / ``tagged_file`` now that the raw + tagged
# article payloads live in the raw_articles / tagged_articles tables instead of on
# S3. The columns are kept as lightweight "materialized" flags so existing presence
# checks (e.g. ``if not record.tagged_file``) keep working unchanged. Only
# ``charts_data_file`` still holds a real S3 key.
DATA_IN_DB = "db"


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
    brand_keywords: list[str],
    competitor_keywords: list[str],
    message_keywords: list[str],
    session_type: SessionType | str = SessionType.UPLOAD,
    queries: Any | None = None,
) -> SessionModel:
    new_session = SessionModel(
        project_id=project_id,
        brand_keywords=brand_keywords,
        competitor_keywords=competitor_keywords,
        message_keywords=message_keywords,
        session_type=session_type,
        queries=queries,
    )
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    return new_session


def update_session_source_file(db: Session, session: SessionModel, source_file: str) -> SessionModel:
    session.source_file = source_file
    session.tagged_file = None
    session.charts_data_file = None
    db.commit()
    db.refresh(session)
    return session


def update_session_tagged_file(db: Session, session: SessionModel, tagged_file: str) -> SessionModel:
    session.tagged_file = tagged_file
    session.charts_data_file = None
    session.status = "Tagged"
    db.commit()
    db.refresh(session)
    return session


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


def update_session_workflow(db: Session, session: SessionModel, workflow: Any | None, session_type: SessionType | str = SessionType.UPLOAD) -> SessionModel:
    """Persist the visual pipeline graph (nodes + edges) for a session."""
    session.workflow = workflow
    session.status = "Workflow Saved"
    session.session_type = session_type
    db.commit()
    db.refresh(session)
    return session