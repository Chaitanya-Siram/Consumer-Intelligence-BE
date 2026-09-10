from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB

from db_helpers.database import Base


class RawArticleModel(Base):
    __tablename__ = "raw_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True)
    file_upload_id = Column(String, nullable=True)
    article_id = Column(String, nullable=True, index=True)  # sha256(url without trailing slash)
    url = Column(Text, nullable=True)
    date = Column(DateTime(timezone=True), nullable=True, index=True)
    data = Column(JSONB, nullable=False)
    created_at = Column(DateTime, default=func.now())


class RawArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: Optional[int] = None
    session_id: Optional[int] = None
    file_upload_id: Optional[str] = None
    article_id: Optional[str] = None
    url: Optional[str] = None
    date: Optional[str] = None
    data: Any
    created_at: Optional[datetime] = None
