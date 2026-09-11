from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from agents.section_fetcher.sections_helper import extract_section_names
from configs import logger
from db_helpers.database import get_db
from db_helpers.repository.auth_repository.dependencies import get_mapping_id, get_org_id
from db_helpers.repository.projects_db import (
    create_project,
    delete_project,
    get_project,
    list_projects,
    set_sections_orders,
    set_sections_prompt,
    update_project,
)

router = APIRouter(prefix="/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None


class SectionsPromptUpdate(BaseModel):
    sections_prompt: Optional[str] = Field(
        default=None, description="Prompt guiding section tagging. Pass null to clear it."
    )


class SectionsOrderUpdate(BaseModel):
    sections_orders: list[str] = Field(
        default_factory=list, description="Ordered section names (e.g. from drag-reorder)."
    )
    session_id: Optional[int] = Field(
        default=None, description="If set, also reorder that session's cached charts file."
    )


def _reorder_session_mm_sections(db: Session, session_id: int, sections_orders: list[str]) -> None:
    """Reorder the media-monitoring sections inside a session's cached charts file so
    the saved dashboard reflects the new order without regenerating. Best-effort."""
    import json
    from charts_helpers.media_monitoring import apply_section_order
    from db_helpers.repository.sessions_db import get_session
    from file_helpers.s3_file import s3_file

    record = get_session(db, session_id)
    if record is None or not record.charts_data_file:
        return
    raw = s3_file.download_file(record.charts_data_file)
    data = json.loads(raw)
    mm = data.get("media_monitoring")
    if isinstance(mm, list) and mm and isinstance(mm[0], dict) and isinstance(mm[0].get("data"), dict):
        mm[0]["data"] = apply_section_order(mm[0]["data"], sections_orders)
        s3_file.upload_file(record.charts_data_file, json.dumps(data, default=str).encode("utf-8"))


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    name: str
    description: Optional[str] = None
    is_active: bool
    monitoring_sections_prompt: Optional[str] = None
    sections_orders: Optional[list[str]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create(
    payload: ProjectCreate,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
    mapping_id: int = Depends(get_mapping_id),
) -> ProjectResponse:
    """Create a new project in the caller's organization."""
    project = create_project(
        db,
        org_id=org_id,
        created_by_id=mapping_id,
        name=payload.name,
        description=payload.description,
    )
    logger.info(
        f"Created project id={project.id} org_id={org_id} "
        f"created_by_id={mapping_id} name={project.name!r}"
    )
    return project


@router.get("", response_model=list[ProjectResponse])
def list_all(
    include_inactive: bool = Query(True, description="Include deactivated projects."),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> list[ProjectResponse]:
    """List the caller's organization projects, newest first."""
    return list_projects(
        db, org_id=org_id, include_inactive=include_inactive, skip=skip, limit=limit
    )


@router.get("/{project_id}", response_model=ProjectResponse)
def retrieve(
    project_id: int,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> ProjectResponse:
    """Fetch a single project by id."""
    project = get_project(db, project_id, org_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return project


@router.put("/{project_id}", response_model=ProjectResponse)
def update(
    project_id: int,
    payload: ProjectUpdate,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> ProjectResponse:
    """Partially update a project (only the fields provided are changed)."""
    project = get_project(db, project_id, org_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    project = update_project(db, project, **fields)
    logger.info(f"Updated project id={project_id}: {sorted(fields)}")
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    project_id: int,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> None:
    """Delete a project (and its sessions, via ON DELETE CASCADE)."""
    project = get_project(db, project_id, org_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    delete_project(db, project)
    logger.info(f"Deleted project id={project_id}")


@router.post("/{project_id}/add_sections_prompt", response_model=ProjectResponse)
def add_sections_prompt(
    project_id: int,
    payload: SectionsPromptUpdate,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> ProjectResponse:
    """Set (or clear) the monitoring sections prompt for a project."""
    project = get_project(db, project_id, org_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    sections_orders = extract_section_names(payload.sections_prompt) if payload.sections_prompt else None
    project = set_sections_prompt(db, project, payload.sections_prompt, sections_orders)
    logger.info(f"Updated sections prompt for project id={project_id}; sections={sections_orders}")
    return project


@router.put("/{project_id}/sections_orders", response_model=ProjectResponse)
def update_sections_orders(
    project_id: int,
    payload: SectionsOrderUpdate,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> ProjectResponse:
    """Persist the section display order (e.g. after the user drags sections in the
    Media Monitoring dashboard)."""
    project = get_project(db, project_id, org_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    project = set_sections_orders(db, project, payload.sections_orders)
    logger.info(f"Updated sections order for project id={project_id}: {payload.sections_orders}")

    # Keep the viewed session's cached charts file in sync with the new order.
    if payload.session_id is not None:
        try:
            _reorder_session_mm_sections(db, payload.session_id, payload.sections_orders)
        except Exception:  # noqa: BLE001
            logger.exception(f"Failed to reorder charts file for session_id={payload.session_id}")

    return project