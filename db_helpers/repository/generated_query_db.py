from datetime import datetime
from typing import Any, Optional
from sqlalchemy.orm import Session
from db_helpers.models.generated_query_model import GeneratedQueryModel


def create_generated_query_record(
    db: Session,
    project_id: int,
    name: str,
    brand_keywords: list[str],
    competitor_keywords: list[str],
    message_keywords: list[str],
    queries: Any | None = None,
) -> GeneratedQueryModel:
    new_query = GeneratedQueryModel(
        project_id=project_id,
        name=name,
        brand_keywords=brand_keywords,
        competitor_keywords=competitor_keywords,
        message_keywords=message_keywords,
        queries=queries,
    )
    db.add(new_query)
    db.commit()
    db.refresh(new_query)
    return new_query


def list_generated_query_by_project(db: Session, project_id: int) -> list[GeneratedQueryModel]:
    return (
        db.query(GeneratedQueryModel)
        .filter(GeneratedQueryModel.project_id == project_id)
        .order_by(GeneratedQueryModel.id.desc())
        .all()
    )


def get_generated_query(db: Session, query_id: int) -> Optional[GeneratedQueryModel]:
    return db.query(GeneratedQueryModel).filter(GeneratedQueryModel.id == query_id).first()


def set_generated_query_schedule(
    db: Session,
    gq: GeneratedQueryModel,
    schedule_time: Optional[str],
    schedule_timezone: Optional[str],
    schedule_time_utc: Optional[str],
) -> GeneratedQueryModel:
    """Set (or clear) a recurring daily schedule. Passing a falsy time clears it."""
    if schedule_time:
        gq.schedule_time = schedule_time
        gq.schedule_timezone = schedule_timezone
        gq.schedule_time_utc = schedule_time_utc
        gq.status = "Scheduled"
    else:
        gq.schedule_time = None
        gq.schedule_timezone = None
        gq.schedule_time_utc = None
        gq.status = "Unscheduled"
    db.commit()
    db.refresh(gq)
    return gq


def list_scheduled_generated_queries(db: Session) -> list[GeneratedQueryModel]:
    """All generated queries with an active daily schedule (used by the scheduler)."""
    return (
        db.query(GeneratedQueryModel)
        .filter(GeneratedQueryModel.status == "Scheduled")
        .filter(GeneratedQueryModel.schedule_time.isnot(None))
        .all()
    )


def mark_generated_query_run(db: Session, gq: GeneratedQueryModel, when_utc: datetime) -> GeneratedQueryModel:
    """Record the most recent scheduled execution time (UTC)."""
    gq.last_run_at = when_utc
    db.commit()
    db.refresh(gq)
    return gq