from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB

from db_helpers.database import Base


class RawArticleModel(Base):
    __tablename__ = "raw_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    article_id = Column(String, nullable=True, index=True)  # sha256(url without trailing slash)
    url = Column(Text, nullable=True)
    date = Column(String, nullable=True, index=True)  # ISO-8601 string; sorts lexically
    data = Column(JSONB, nullable=False)
    created_at = Column(DateTime, default=func.now())


class RawArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    article_id: Optional[str] = None
    url: Optional[str] = None
    date: Optional[str] = None
    data: Any
    created_at: Optional[datetime] = None
