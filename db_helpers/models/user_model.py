from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import Boolean, Column, DateTime, Integer, String, func
from db_helpers.database import Base


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    is_active = Column(Boolean, default=True)
    is_superadmin = Column(Boolean, default=False, nullable=False)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: EmailStr
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_active: bool = True
    is_superadmin: bool = False


class CurrentUser(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    
    id: int
    name: str
    email: EmailStr
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    is_active: bool = True
    is_superadmin: bool = False
    org_id: int
    role: str