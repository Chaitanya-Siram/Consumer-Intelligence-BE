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
    The article dict is split across `data` and the columns above it: url, date
    and the flags are promoted copies (also in `data`) so dashboards/filters can
    query them in SQL, while title/content live in their columns *only* to keep
    the JSON small. Reads recombine the two halves into the single article dict
    the charts calculators and the E2B sandbox consume — see
    `tagged_articles_db.article_dict`. `article_ref` is the "A1" id used across
    the app.
    """
    __tablename__ = "tagged_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    article_id = Column(String, nullable=True, index=True)
    article_ref = Column(String, nullable=False)
    url = Column(Text, nullable=True)
    date = Column(String, nullable=True, index=True)  # ISO-8601 string; sorts lexically
    title = Column(Text, nullable=True)
    content = Column(Text, nullable=True)
    is_relevant = Column(Boolean, nullable=False, default=True, index=True)
    is_approved_for_monitoring = Column(Boolean, nullable=False, default=False)
    is_approved_for_dashboards = Column(Boolean, nullable=False, default=False)

    # Every other article field (sentiment, theme, section, reach,
    # priority_watch, …) — title/content are stored in their columns only.
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
