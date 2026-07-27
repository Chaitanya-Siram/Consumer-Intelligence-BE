from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db_helpers.repository.auth_repository.dependencies import (
    get_current_user,
    require_superadmin_or_secret,
)
from configs import logger
from db_helpers.database import get_db
from db_helpers.models.organization_model import OrganizationResponse
from db_helpers.models.user_model import UserModel
from db_helpers.repository.auth_repository.organizations_db import (
    create_organization,
    delete_organization,
    get_organization,
    list_organizations,
    update_organization,
)

router = APIRouter(prefix="/organizations", tags=["organizations"])


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    is_active: Optional[bool] = None


@router.post("", response_model=OrganizationResponse, status_code=status.HTTP_201_CREATED)
def create(
    payload: OrganizationCreate,
    db: Session = Depends(get_db),
    _: Optional[UserModel] = Depends(require_superadmin_or_secret),
) -> OrganizationResponse:
    """Create a new organization. Superadmin (or the admin shared secret) only."""
    organization = create_organization(db, name=payload.name, description=payload.description)
    logger.info(f"Created organization id={organization.id} name={organization.name!r}")
    return organization


@router.get("", response_model=list[OrganizationResponse])
def list_all(
    include_inactive: bool = Query(True, description="Include deactivated organizations."),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> list[OrganizationResponse]:
    """List organizations, newest first."""
    return list_organizations(db, include_inactive=include_inactive, skip=skip, limit=limit)


@router.get("/{organization_id}", response_model=OrganizationResponse)
def retrieve(
    organization_id: int,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> OrganizationResponse:
    """Fetch a single organization by id."""
    organization = get_organization(db, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return organization


@router.put("/{organization_id}", response_model=OrganizationResponse)
def update(
    organization_id: int,
    payload: OrganizationUpdate,
    db: Session = Depends(get_db),
    _: Optional[UserModel] = Depends(require_superadmin_or_secret),
) -> OrganizationResponse:
    """Partially update an organization. Superadmin (or the admin shared secret) only."""
    organization = get_organization(db, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    organization = update_organization(db, organization, **fields)
    logger.info(f"Updated organization id={organization_id}: {sorted(fields)}")
    return organization


@router.delete("/{organization_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    organization_id: int,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> None:
    """Delete an organization (and its user mappings, via ON DELETE CASCADE)."""
    organization = get_organization(db, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    delete_organization(db, organization)
    logger.info(f"Deleted organization id={organization_id}")
