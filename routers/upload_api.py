from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends
from pydantic import BaseModel
from configs import logger
from db_helpers.repository.sessions_db import create_session, update_session_source_file, DATA_IN_DB
from db_helpers.repository.raw_articles_db import replace_raw_articles
from db_helpers.repository.tagged_articles_db import delete_tagged_articles
from db_helpers.repository.projects_db import get_project
from file_helpers.file_parser import parse_upload
from sqlalchemy.orm import Session
from db_helpers.database import get_db

router = APIRouter(tags=["upload"])


class UploadResponse(BaseModel):
    session_id: int
    source_file: str
    record_count: int


class CreateSessionRequest(BaseModel):
    project_id: int
    brand_keywords: list[str]
    competitor_keywords: list[str] = []
    message_keywords: list[str]


class CreateSessionResponse(BaseModel):
    session_id: int


@router.post("/upload", response_model=UploadResponse)
def upload(
    project_id: int = Form(..., description="ID of the project this upload belongs to."),
    file: UploadFile = File(..., description="CSV, Excel (.xlsx/.xls), or JSON file."),
    brand_keywords: list[str] = Form(default_factory=list, min_items=1, max_items=1),
    competitor_keywords: list[str] = Form(default_factory=list, min_items=1),
    message_keywords: list[str] = Form(default_factory=list, min_items=1),
    db: Session = Depends(get_db),
) -> UploadResponse:
    logger.info(f"Upload received for filename='{file.filename}' project_id={project_id}")

    if get_project(db, project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    file_content = file.file.read()
    if not file_content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    
    # File Validation and Parsing
    try:
        records = parse_upload(file.filename or "", file_content)
    except ValueError as exc:
        logger.warning(f"Parse failed for '{file.filename}': {exc}")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(f"Unexpected parse failure for '{file.filename}'")
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {exc}") from exc

    if not records:
        raise HTTPException(status_code=400, detail="No records found in the uploaded file.")
    
    session = create_session(db, project_id, brand_keywords, competitor_keywords, message_keywords)

    # Store the parsed raw records in the database (raw_articles) instead of the
    # original file on S3. A fresh source invalidates any prior tagged rows.
    replace_raw_articles(db, session.id, records)
    delete_tagged_articles(db, session.id)
    update_session_source_file(db, session, DATA_IN_DB)

    logger.info(f"Stored {len(records)} raw records for session_id={session.id}")
    return UploadResponse(session_id=session.id, source_file=DATA_IN_DB, record_count=len(records))


@router.post("/session", response_model=CreateSessionResponse)
def create_session_no_file(
    payload: CreateSessionRequest,
    db: Session = Depends(get_db),
) -> CreateSessionResponse:
    """Create a session without uploading a file.

    Same as /upload but for a source that has no file — e.g. a workflow Data node
    that pulls articles from a REST API / Google News RSS. No file is parsed or
    stored, so `source_file` stays empty until the tagging step materializes it
    (see fetch_and_merge_workflow_rss).
    """
    logger.info(f"Creating file-less session for project_id={payload.project_id}")

    if get_project(db, payload.project_id) is None:
        raise HTTPException(status_code=404, detail="Project not found.")

    session = create_session(
        db,
        payload.project_id,
        payload.brand_keywords,
        payload.competitor_keywords,
        payload.message_keywords,
    )

    logger.info(f"Created file-less session id={session.id} for project_id={payload.project_id}")
    return CreateSessionResponse(session_id=session.id)
