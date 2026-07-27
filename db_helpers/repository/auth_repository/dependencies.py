"""FastAPI dependencies for extracting and validating the authenticated user."""
import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from configs import envs
from db_helpers.repository.auth_repository.security import decode_access_token
from db_helpers.database import get_db
from db_helpers.models.user_model import UserModel
from db_helpers.repository.users_db import get_user

# tokenUrl points at the login endpoint so Swagger's "Authorize" flow works.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# Same scheme, but doesn't auto-error when the Authorization header is absent —
# used by endpoints that also accept the admin shared-secret as an alternative.
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> UserModel:
    """Resolve the bearer token to a live, active user or raise 401."""
    claims = decode_access_token(token)
    if not claims or not claims.get("sub"):
        raise _CREDENTIALS_EXC

    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError):
        raise _CREDENTIALS_EXC

    user = get_user(db, user_id)
    if user is None:
        raise _CREDENTIALS_EXC
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user.")
    return user


def require_superadmin(
    current_user: UserModel = Depends(get_current_user),
) -> UserModel:
    """Allow only superadmins through; raise 403 otherwise."""
    if not current_user.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Superadmin privileges required.",
        )
    return current_user


def _superadmin_from_token(token: Optional[str], db: Session) -> Optional[UserModel]:
    """Resolve a bearer token to an active superadmin, or None if it doesn't qualify."""
    if not token:
        return None
    claims = decode_access_token(token)
    if not claims or not claims.get("sub"):
        return None
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError):
        return None
    user = get_user(db, user_id)
    if user is None or not user.is_active or not user.is_superadmin:
        return None
    return user


def require_superadmin_or_secret(
    token: Optional[str] = Depends(optional_oauth2_scheme),
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
    db: Session = Depends(get_db),
) -> Optional[UserModel]:
    """Authorize the request via EITHER an active-superadmin bearer token OR the
    admin shared secret (`X-Admin-Secret` header matching `JWT_SECRET_KEY`).

    Returns the superadmin user when the token path is used, or None when the
    shared-secret path is used (no associated user).
    """
    if x_admin_secret and secrets.compare_digest(x_admin_secret, envs.JWT_SECRET_KEY):
        return None

    user = _superadmin_from_token(token, db)
    if user is not None:
        return user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Superadmin privileges or a valid admin secret required.",
    )
