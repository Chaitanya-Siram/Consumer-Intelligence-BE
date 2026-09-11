from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from db_helpers.repository.auth_repository.dependencies import get_current_user
from db_helpers.repository.auth_repository.organizations_db import get_organization
from db_helpers.repository.auth_repository.refresh_tokens_db import create_refresh_token, get_active_refresh_token, revoke_all_for_user, revoke_refresh_token
from db_helpers.repository.auth_repository.security import create_access_token, verify_password
from configs import logger
from db_helpers.database import get_db
from db_helpers.models.user_model import UserModel, UserResponse
from db_helpers.models.user_org_mapping_model import UserOrgMappingModel
from db_helpers.repository.users_db import (
    get_default_mapping,
    get_mapping,
    get_user,
    get_user_by_email,
    set_default_mapping,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)
    organization_id: Optional[int] = Field(
        default=None, description="Re-scope the new token to this org. Omit to use the default org."
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class OrgContext(BaseModel):
    """The organization the issued token is scoped to (from the user-org mapping)."""
    mapping_id: int
    organization_id: int
    organization_name: Optional[str] = None
    role: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse
    organization: Optional[OrgContext] = None


def _build_org_context(db: Session, mapping: Optional[UserOrgMappingModel]) -> Optional[OrgContext]:
    if mapping is None:
        return None
    org = get_organization(db, mapping.organization_id)
    return OrgContext(
        mapping_id=mapping.id,
        organization_id=mapping.organization_id,
        organization_name=org.name if org is not None else None,
        role=mapping.role,
    )


def _resolve_mapping(
    db: Session, user: UserModel, organization_id: Optional[int]
) -> Optional[UserOrgMappingModel]:
    """Pick the org membership to scope the token to.

    - organization_id given  -> that exact mapping (403 if the user isn't a member);
      it also becomes the user's new default org.
    - organization_id omitted -> the user's default membership, if any
    """
    if organization_id is not None:
        mapping = get_mapping(db, user.id, organization_id)
        if mapping is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="User is not a member of this organization.",
            )
        return set_default_mapping(db, user.id, mapping)
    return get_default_mapping(db, user.id)


def _issue_tokens(
    db: Session, user: UserModel, mapping: Optional[UserOrgMappingModel] = None
) -> TokenResponse:
    """Mint a fresh access token + a new DB-stored refresh token for a user,
    scoped to the given organization membership (if any)."""
    extra_claims: dict = {"email": user.email}
    if mapping is not None:
        extra_claims["org_id"] = mapping.organization_id
        extra_claims["role"] = mapping.role
        # Identifies the user *within* the org; recorded as created_by_id on
        # the rows they create.
        extra_claims["mapping_id"] = mapping.id
    access_token = create_access_token(subject=user.id, extra_claims=extra_claims)
    _, raw_refresh = create_refresh_token(db, user.id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        user=user,
        organization=_build_org_context(db, mapping),
    )


@router.post("/login", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    organization_id: Optional[int] = Form(
        default=None,
        description="Organization to scope the token to. Omit to use the default org.",
    ),
    db: Session = Depends(get_db),
) -> TokenResponse:
    """OAuth2 password-flow login (username field = email). Returns a JWT scoped to
    an organization: the one named by `organization_id`, or the user's default org.

    This is the token endpoint Swagger's "Authorize" button uses.
    """
    user = get_user_by_email(db, form_data.username)
    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user.")

    mapping = _resolve_mapping(db, user, organization_id)
    logger.info(
        f"User logged in id={user.id} email={user.email!r} "
        f"org_id={mapping.organization_id if mapping else None}"
    )
    return _issue_tokens(db, user, mapping)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Exchange a valid refresh token for a new access + refresh pair.
    The presented refresh token is rotated (revoked) so it cannot be reused."""
    record = get_active_refresh_token(db, payload.refresh_token)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )

    user = get_user(db, record.user_id)
    if user is None or not user.is_active:
        # Token orphaned or user deactivated — burn it and reject.
        revoke_refresh_token(db, record)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User is no longer active.")

    # Resolve the org first: a rejected organization must not burn the caller's
    # refresh token, or a failed org switch would end the session.
    mapping = _resolve_mapping(db, user, payload.organization_id)

    # Rotate: revoke the used token, then issue a brand-new pair.
    revoke_refresh_token(db, record)
    logger.info(
        f"Refreshed tokens for user id={user.id} "
        f"org_id={mapping.organization_id if mapping else None}"
    )
    return _issue_tokens(db, user, mapping)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(payload: RefreshRequest, db: Session = Depends(get_db)) -> None:
    """Revoke a refresh token (this device's session). Idempotent — an unknown
    or already-revoked token is treated as success."""
    record = get_active_refresh_token(db, payload.refresh_token)
    if record is not None:
        revoke_refresh_token(db, record)
        logger.info(f"Logged out (refresh token revoked) user id={record.user_id}")


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Revoke every refresh token for the authenticated user (logout everywhere)."""
    revoked = revoke_all_for_user(db, current_user.id)
    logger.info(f"Logged out everywhere for user id={current_user.id}; revoked {revoked} token(s)")


@router.get("/me", response_model=UserResponse)
def me(current_user: UserModel = Depends(get_current_user)) -> UserResponse:
    """Return the currently authenticated user (token introspection)."""
    return current_user
