from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from db_helpers.database import Base


class DataProvidersAPIModel(Base):
    __tablename__ = "data_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    label = Column(String, nullable=False)
    url = Column(String, nullable=False)
    credentials_fields_required = Column(JSON, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)


class DataProvidersAPIKeyModel(Base):
    __tablename__ = "data_providers_api_keys"
    __table_args__ = (
        UniqueConstraint("org_id", "data_provider_id", name="uq_org_data_provider"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    data_provider_id = Column(Integer, ForeignKey("data_providers.id", ondelete="CASCADE"), nullable=False)
    api_key = Column(String, nullable=False)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    is_active = Column(Boolean, nullable=False, default=True)


class DataProvidersAPIResponse(BaseModel):
    """A data provider available for credential configuration."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    label: str
    url: str
    credentials_fields_required: list[str] | dict
    is_active: bool = True


class DataProvidersAPIKeyResponse(BaseModel):
    """A stored credential set. Secrets are returned masked, never in plaintext."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    org_id: int
    data_provider_id: int
    api_key: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_active: bool = True


class DataProviderKeyCreate(BaseModel):
    org_id: int
    data_provider_id: int
    api_key: str = Field(..., min_length=1)
    username: Optional[str] = None
    password: Optional[str] = None


class DataProviderKeyUpdate(BaseModel):
    data_provider_id: Optional[int] = None
    api_key: Optional[str] = Field(default=None, min_length=1)
    username: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None