from typing import Annotated, Any
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from configs import logger
from db_helpers.database import get_db
from db_helpers.models.session_model import SessionResponse
from db_helpers.repository.auth_repository.dependencies import get_org_id
from db_helpers.repository.projects_db import get_project
from db_helpers.repository.raw_articles_db import assign_uploads_to_session, file_upload_exists
from db_helpers.workflow_validator import (
    WorkflowValidationError,
    collect_file_upload_ids,
    collect_keywords,
    validate_workflow,
    CREATE_EXAMPLE, WORKFLOW_EXAMPLE_BODY
)
from db_helpers.repository.sessions_db import (
    create_session,
    delete_session,
    get_session,
    list_sessions_by_project,
    update_session_workflow,
)

router = APIRouter(tags=["sessions"])

class WorkflowUpdate(BaseModel):
    workflow: dict[str, Any]

class CreateSessionRequest(BaseModel):
    project_id: int
    workflow: dict[str, Any]

class CreateSessionResponse(BaseModel):
    session_id: int


@router.post("/session", response_model=CreateSessionResponse)
def create(
    payload: Annotated[CreateSessionRequest, Body(openapi_examples=CREATE_EXAMPLE)],
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> CreateSessionResponse:
    """Create a session from a workflow graph.

    Each data node must declare sourceType 
    - "file" (with a file_upload_id from /upload)
    - "api" (with data_sources and queries)
    
    The other type's fields are stripped before the workflow is saved. Keywords are collected off
    the data nodes.
    """
    logger.info(f"Creating session for project_id={payload.project_id}")
    project = get_project(db, payload.project_id, org_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    try:
        workflow = validate_workflow(payload.workflow)
    except WorkflowValidationError as exc:
        logger.warning(f"Invalid workflow for project_id={payload.project_id}: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    file_upload_ids = collect_file_upload_ids(workflow)
    for file_upload_id in file_upload_ids:
        if not file_upload_exists(db, file_upload_id):
            raise HTTPException(
                status_code=404, detail=f"No uploaded articles found for file_upload_id {file_upload_id}."
            )

    keywords = collect_keywords(workflow)
    session = create_session(
        db,
        payload.project_id,
        project.name,
        keywords["brand_keywords"],
        keywords["competitor_keywords"],
        keywords["message_keywords"],
        workflow=workflow,
    )

    # The uploaded articles have no session until now; claim them for this one.
    claimed = assign_uploads_to_session(db, session.id, file_upload_ids)
    logger.info(
        f"Created session id={session.id} for project_id={payload.project_id}; "
        f"claimed {claimed} uploaded article(s)"
    )
    return CreateSessionResponse(session_id=session.id)


@router.get("/projects/{project_id}/sessions", response_model=list[SessionResponse])
def list_sessions(project_id: int, db: Session = Depends(get_db)) -> list[SessionResponse]:
    """List all sessions (uploaded files) for a project, newest first."""
    if get_project(db, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")
    return list_sessions_by_project(db, project_id)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_one(session_id: int, db: Session = Depends(get_db)) -> SessionResponse:
    """Fetch a single session (including its saved workflow graph)."""
    session = get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


@router.put("/sessions/{session_id}/workflow", response_model=SessionResponse)
def update_workflow(
    session_id: int,
    payload: Annotated[WorkflowUpdate, Body(openapi_examples=WORKFLOW_EXAMPLE_BODY)],
    db: Session = Depends(get_db),
) -> SessionResponse:
    """Persist the workflow designer graph (nodes + edges) on the session."""
    session = get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    try:
        workflow = validate_workflow(payload.workflow)
    except WorkflowValidationError as exc:
        logger.warning(f"Invalid workflow for session id={session_id}: {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    file_upload_ids = collect_file_upload_ids(workflow)
    for file_upload_id in file_upload_ids:
        if not file_upload_exists(db, file_upload_id):
            raise HTTPException(
                status_code=404, detail=f"No uploaded articles found for file_upload_id {file_upload_id}."
            )

    session = update_session_workflow(db, session, workflow)
    # An edited workflow may point at an upload that hasn't been claimed yet.
    claimed = assign_uploads_to_session(db, session_id, file_upload_ids)
    logger.info(f"Saved workflow for session id={session_id}; claimed {claimed} uploaded article(s)")
    return session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(session_id: int, db: Session = Depends(get_db)) -> None:
    """Delete a single session."""
    session = get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    delete_session(db, session)
    logger.info(f"Deleted session id={session_id}")
