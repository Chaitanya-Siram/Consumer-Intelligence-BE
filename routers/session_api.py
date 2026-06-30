from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from configs import logger
from db_helpers.database import get_db
from db_helpers.models.session_model import SessionResponse
from db_helpers.repository.projects_db import get_project
from db_helpers.repository.sessions_db import (
    delete_session,
    get_session,
    list_sessions_by_project,
    update_session_workflow,
)

router = APIRouter(tags=["sessions"])


class WorkflowUpdate(BaseModel):
    workflow: Optional[Any] = None


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
def save_workflow(session_id: int, payload: WorkflowUpdate, db: Session = Depends(get_db)) -> SessionResponse:
    """Persist the workflow designer graph (nodes + edges) on the session."""
    session = get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    session = update_session_workflow(db, session, payload.workflow)
    logger.info(f"Saved workflow for session id={session_id}")
    return session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(session_id: int, db: Session = Depends(get_db)) -> None:
    """Delete a single session."""
    session = get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    delete_session(db, session)
    logger.info(f"Deleted session id={session_id}")
