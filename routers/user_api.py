from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from db_helpers.repository.auth_repository.dependencies import (
    get_current_user,
    get_org_id,
    require_superadmin_or_secret,
)
from db_helpers.repository.auth_repository.security import hash_password
from configs import logger
from db_helpers.database import get_db
from db_helpers.models.organization_model import OrganizationResponse
from db_helpers.models.user_model import UserModel, UserResponse
from db_helpers.models.user_org_mapping_model import UserOrgMappingResponse
from db_helpers.repository.auth_repository.organizations_db import get_organization
from db_helpers.repository.users_db import (
    add_user_to_org,
    create_user,
    delete_user,
    get_mapping,
    get_user,
    get_user_by_email,
    list_user_orgs,
    list_users,
    remove_user_from_org,
    update_user,
)

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    organization_id: int
    role: str = Field(default="analyst", max_length=64)


class UserUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None


class OrgMembershipCreate(BaseModel):
    organization_id: int
    role: str = Field(default="analyst", max_length=64)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _: UserModel = Depends(require_superadmin_or_secret),
) -> UserResponse:
    """Create a new user and attach them to an organization. Superadmin only.

    If a user with this email already exists, no new user is created — they are
    simply added to the organization, unless they already belong to it (409).
    """
    if get_organization(db, payload.organization_id) is None:
        raise HTTPException(status_code=404, detail="Organization not found.")

    user = get_user_by_email(db, payload.email)
    if user is None:
        user = create_user(
            db,
            name=payload.name,
            email=payload.email,
            hashed_password=hash_password(payload.password),
        )
        logger.info(f"Created user id={user.id} email={user.email!r}")
    elif get_mapping(db, user.id, payload.organization_id) is not None:
        raise HTTPException(
            status_code=409,
            detail="User already exists and is a member of this organization.",
        )
    else:
        logger.info(
            f"User id={user.id} already exists; adding to organization id={payload.organization_id}"
        )

    add_user_to_org(db, user.id, payload.organization_id, role=payload.role)
    logger.info(
        f"Added user id={user.id} to organization id={payload.organization_id} role={payload.role!r}"
    )
    return user


@router.get("", response_model=list[UserResponse])
def list_all(
    include_inactive: bool = Query(True, description="Include deactivated users."),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    org_id: int = Depends(get_org_id),
) -> list[UserResponse]:
    """List the members of the caller's organization (from the token), newest first."""
    return list_users(
        db,
        organization_id=org_id,
        include_inactive=include_inactive,
        skip=skip,
        limit=limit,
    )


@router.get("/{user_id}", response_model=UserResponse)
def retrieve(
    user_id: int,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> UserResponse:
    """Fetch a single user by id."""
    user = get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


@router.put("/{user_id}", response_model=UserResponse)
def update(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> UserResponse:
    """Partially update a user (only the fields provided are changed)."""
    user = get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    # Guard against colliding with another user's email.
    new_email = fields.get("email")
    if new_email and new_email != user.email:
        existing = get_user_by_email(db, new_email)
        if existing is not None and existing.id != user_id:
            raise HTTPException(status_code=409, detail="A user with this email already exists.")

    user = update_user(db, user, **fields)
    logger.info(f"Updated user id={user_id}: {sorted(fields)}")
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    user_id: int,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> None:
    """Delete a user (and its org mappings, via ON DELETE CASCADE)."""
    user = get_user(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    delete_user(db, user)
    logger.info(f"Deleted user id={user_id}")


# --- Organization membership --------------------------------------------------

@router.get("/{user_id}/organizations", response_model=list[OrganizationResponse])
def list_organizations_for_user(
    user_id: int,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> list[OrganizationResponse]:
    """List the organizations a user belongs to."""
    if get_user(db, user_id) is None:
        raise HTTPException(status_code=404, detail="User not found.")
    return list_user_orgs(db, user_id)


@router.post(
    "/{user_id}/organizations",
    response_model=UserOrgMappingResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_organization_membership(
    user_id: int,
    payload: OrgMembershipCreate,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> UserOrgMappingResponse:
    """Add a user to an organization with a role."""
    if get_user(db, user_id) is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if get_organization(db, payload.organization_id) is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if get_mapping(db, user_id, payload.organization_id) is not None:
        raise HTTPException(status_code=409, detail="User is already a member of this organization.")

    mapping = add_user_to_org(db, user_id, payload.organization_id, role=payload.role)
    logger.info(
        f"Added user id={user_id} to organization id={payload.organization_id} role={payload.role!r}"
    )
    return mapping


@router.delete(
    "/{user_id}/organizations/{organization_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_organization_membership(
    user_id: int,
    organization_id: int,
    db: Session = Depends(get_db),
    _: UserModel = Depends(get_current_user),
) -> None:
    """Remove a user from an organization."""
    mapping = get_mapping(db, user_id, organization_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="Membership not found.")
    remove_user_from_org(db, mapping)
    logger.info(f"Removed user id={user_id} from organization id={organization_id}")
