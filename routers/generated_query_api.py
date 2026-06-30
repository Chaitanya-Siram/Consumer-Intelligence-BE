from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from db_helpers.database import get_db
from configs import logger
from db_helpers.models.generated_query_model import GeneratedQueryResponse
from db_helpers.repository.generated_query_db import (
    get_generated_query,
    list_generated_query_by_project,
    set_generated_query_schedule,
)
from db_helpers.repository.projects_db import get_project


router = APIRouter(tags=["generated-query"])


class ScheduleRequest(BaseModel):
    """Set a recurring daily schedule. Pass a null/empty time to unschedule."""
    schedule_time: Optional[str] = Field(default=None, description="Local time-of-day 'HH:MM'.")
    schedule_timezone: Optional[str] = Field(default=None, description="IANA timezone, e.g. 'Asia/Kolkata'.")


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    """Parse a wall-clock time string into (hour, minute). Tolerates 'H:MM',
    'HH:MM', and 'HH:MM:SS' (seconds ignored) plus surrounding whitespace."""
    parts = str(hhmm).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"Expected 'HH:MM', got {hhmm!r}.")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time out of range: {hhmm!r}.")
    return hour, minute


def _local_hhmm_to_utc(hhmm: str, tzname: str) -> str:
    """Convert a 'HH:MM' wall-clock time in an IANA timezone to the UTC 'HH:MM'
    (snapshot based on today's date — may shift across DST boundaries)."""
    hour, minute = _parse_hhmm(hhmm)
    local_now = datetime.now(ZoneInfo(tzname))
    local_dt = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local_dt.astimezone(ZoneInfo("UTC")).strftime("%H:%M")


@router.get("/projects/{project_id}/generated-queries", response_model=list[GeneratedQueryResponse])
def list_sessions(project_id: int, db: Session = Depends(get_db)) -> list[GeneratedQueryResponse]:
    """List all generated queries for a project, newest first."""
    if get_project(db, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return list_generated_query_by_project(db, project_id)


@router.put(
    "/projects/{project_id}/generated-queries/{query_id}/schedule",
    response_model=GeneratedQueryResponse,
)
def schedule_generated_query(
    project_id: int, query_id: int, payload: ScheduleRequest, db: Session = Depends(get_db)
) -> GeneratedQueryResponse:
    """Set (or clear) a recurring daily schedule for a generated query. Stores the
    local time + timezone and the equivalent UTC time; a background scheduler runs
    the fetch+tag pipeline daily at that time."""
    if get_project(db, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    gq = get_generated_query(db, query_id)
    if gq is None or gq.project_id != project_id:
        raise HTTPException(status_code=404, detail="Generated query not found.")

    time_utc = None
    if payload.schedule_time:
        if not payload.schedule_timezone:
            raise HTTPException(status_code=400, detail="A timezone is required to schedule.")
        try:
            time_utc = _local_hhmm_to_utc(payload.schedule_time, payload.schedule_timezone)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"Invalid time or timezone: {exc}") from exc

    gq = set_generated_query_schedule(
        db, gq, payload.schedule_time, payload.schedule_timezone, time_utc
    )
    logger.info(
        f"Scheduled generated query id={query_id}: {gq.schedule_time} {gq.schedule_timezone} "
        f"(UTC {gq.schedule_time_utc}) status={gq.status}"
    )
    return gq
