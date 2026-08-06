from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from db_helpers.database import Base


class TaggedArticleModel(Base):
    """One tagged article belonging to a session.

    Replaces the tagged JSON file that used to live on S3 (session.tagged_file).
    The canonical article dict — exactly what the charts calculators and the E2B
    sandbox consume — is kept in `data`. The columns above it are promoted copies
    of the "hot" fields so dashboards/filters can query them in SQL without
    unpacking the JSON. `article_ref` is the "A1" id used across the app.
    """
    __tablename__ = "tagged_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    article_id = Column(String, nullable=True, index=True)
    article_ref = Column(String, nullable=False)

    # Promoted "hot" fields (also present inside `data`).
    title = Column(Text, nullable=True)
    url = Column(Text, nullable=True)
    date = Column(String, nullable=True, index=True)  # ISO-8601 string; sorts lexically
    sentiment = Column(String, nullable=True, index=True)
    theme = Column(String, nullable=True)
    section = Column(String, nullable=True, index=True)
    reach = Column(Integer, nullable=True)
    priority_watch = Column(Boolean, nullable=False, default=False)
    is_approved = Column(Boolean, nullable=False, default=False)
    is_approved_for_monitoring = Column(Boolean, nullable=False, default=False)

    # Canonical full article dict.
    data = Column(JSONB, nullable=False)

    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("session_id", "article_ref", name="uq_tagged_session_ref"),
    )


class TaggedArticleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    session_id: int
    article_ref: str
    data: Any
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
