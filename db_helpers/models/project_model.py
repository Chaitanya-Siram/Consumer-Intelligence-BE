from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, JSON, Text, func
from db_helpers.database import Base

class ProjectModel(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    created_by_id = Column(Integer, ForeignKey("user_org_mapping.id", ondelete="SET NULL"), nullable=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    is_active = Column(Boolean, default=True)
    monitoring_sections_prompt = Column(Text, nullable=True)
    sections_orders = Column(JSON, nullable=True)
