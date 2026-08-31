from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Depends
from pydantic import BaseModel
from configs import logger
from db_helpers.repository.auth_repository.dependencies import get_org_id
from db_helpers.repository.raw_articles_db import (
    add_upload_raw_articles,
    new_file_upload_id,
)
from db_helpers.repository.projects_db import get_project
from file_helpers.file_parser import parse_upload
from sqlalchemy.orm import Session
from db_helpers.database import get_db

router = APIRouter(tags=["upload"])

class UploadResponse(BaseModel):
    file_upload_id: str
    record_count: int


@router.post("/upload", response_model=UploadResponse)
def upload(
    project_id: int = Form(..., description="ID of the project this upload belongs to."),
    file: UploadFile = File(..., description="CSV, Excel (.xlsx/.xls), or JSON file."),
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> UploadResponse:
    logger.info(f"Upload received for filename='{file.filename}' project_id={project_id}")

    if get_project(db, project_id, org_id) is None:
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
    
    # Store the parsed records against a fresh upload id; no session yet.
    file_upload_id = new_file_upload_id()
    add_upload_raw_articles(db, project_id, file_upload_id, records)

    logger.info(f"Stored {len(records)} raw records for file_upload_id={file_upload_id}")
    return UploadResponse(file_upload_id=file_upload_id, record_count=len(records))
