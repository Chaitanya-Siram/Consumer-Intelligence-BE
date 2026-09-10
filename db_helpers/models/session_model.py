from datetime import datetime
import enum
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, JSON, Text, func
from db_helpers.database import Base
from db_helpers.mutable_json import NestedMutableDict


class SessionModel(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    created_by_id = Column(Integer, ForeignKey("user_org_mapping.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    name = Column(String, nullable=False)
    brand_keywords = Column(JSON, nullable=False)
    competitor_keywords = Column(JSON, nullable=False)
    message_keywords = Column(JSON, nullable=False)
    relevancy_prompt = Column(Text, nullable=True)
    relevancy_domains = Column(JSON, nullable=True)
    # Visual pipeline graph (nodes + edges) built in the workflow designer.
    workflow = Column(NestedMutableDict.as_mutable(JSON), nullable=True)
    status = Column(String, nullable=False, default="Created")
    charts_data_file = Column(String, nullable=True)
    # schedule_time = Column(String, nullable=True)
    # schedule_timezone = Column(String, nullable=True)
    # schedule_time_utc = Column(String, nullable=True)
    # last_run_at = Column(DateTime, nullable=True)


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    charts_data_file: Optional[str] = None
    brand_keywords: list[str] = []
    competitor_keywords: list[str] = []
    message_keywords: list[str] = []
    relevancy_prompt: Optional[str] = None
    relevancy_domains: Optional[Any] = None
    workflow: Optional[Any] = None
    status: str