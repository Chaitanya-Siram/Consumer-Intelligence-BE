import os
import time
from datetime import datetime
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends
from pydantic import BaseModel
from configs import logger
from db_helpers.repository.sessions_db import create_session, update_session_source_file
from db_helpers.repository.projects_db import get_project
from file_helpers.file_parser import parse_upload
from file_helpers.s3_file import s3_file
from sqlalchemy.orm import Session
from db_helpers.database import get_db

router = APIRouter(tags=["upload"])


class UploadResponse(BaseModel):
    session_id: int
    source_file: str
    record_count: int


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

    # Upload Original file to S3
    current_date = datetime.now().strftime("%Y-%m-%d")
    original_filename = file.filename or ""
    name, ext = os.path.splitext(original_filename)
    file_key = f"session_files/session_{session.id}/{current_date}/raw_{name}_{int(time.time())}{ext}"
    s3_file.upload_file(file_key, file_content)

    update_session_source_file(db, session, file_key)

    logger.info(f"Uploaded {len(records)} records to key='{file_key}'")
    return UploadResponse(session_id=session.id, source_file=file_key, record_count=len(records))
