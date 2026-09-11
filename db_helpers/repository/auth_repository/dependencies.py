"""FastAPI dependencies for extracting and validating the authenticated user."""
import secrets
from typing import Optional
from fastapi import Depends, Header, HTTPException, WebSocketException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection
from configs import envs
from db_helpers.repository.auth_repository.security import decode_access_token
from db_helpers.database import get_db
from db_helpers.models.user_model import CurrentUser, UserModel
from db_helpers.repository.users_db import get_mapping, get_user

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


# Roles on user_org_mapping that count as administering an organization.
ORG_ADMIN_ROLES = {"admin", "org_admin", "owner"}

ORG_ADMIN_EXC = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Organization admin privileges required for this action.",
)

def require_org_admin(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> CurrentUser:
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

    role = claims.get("role")
    if not role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No role assign.")

    if claims.get("org_id") is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No organization is associated with this session.",
        )

    # Superadmins pass regardless; everyone else needs an admin role in the org.
    if not user.is_superadmin and role.lower() not in ORG_ADMIN_ROLES:
        raise ORG_ADMIN_EXC
    return CurrentUser(
        id=user.id,
        name=user.name,
        email=user.email,
        created_at=user.created_at,
        updated_at=user.updated_at,
        is_active=user.is_active,
        is_superadmin=user.is_superadmin,
        org_id=claims.get("org_id"),
        role=role,
    )


def _extract_token(conn: HTTPConnection) -> str:
    """Pull the bearer token from the Authorization header, falling back to the
    `token` query parameter (used by browser WebSocket clients)."""
    auth = conn.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return conn.query_params.get("token", "")


def _reject(conn: HTTPConnection, detail: str) -> Exception:
    """Build the right rejection for the connection type (WS close vs HTTP 401)."""
    if conn.scope.get("type") == "websocket":
        return WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=detail)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_connection_claims(conn: HTTPConnection) -> dict:
    """Validate the token on an HTTP or WebSocket connection and return its claims.

    Args:
        conn: The incoming HTTP or WebSocket connection.

    Returns:
        The decoded JWT claims.
    """
    token = _extract_token(conn)
    if not token:
        raise _reject(conn, "Not authenticated.")
    claims = decode_access_token(token)
    if not claims or not claims.get("sub"):
        raise _reject(conn, "Could not validate credentials.")
    return claims


def get_connection_org_id(conn: HTTPConnection) -> int:
    """Resolve the caller's organization from an HTTP or WebSocket connection.

    Args:
        conn: The incoming HTTP or WebSocket connection.

    Returns:
        The org_id claim on the token.
    """
    org_id = get_connection_claims(conn).get("org_id")
    if org_id is None:
        raise _reject(conn, "No organization is associated with this session.")
    return org_id


def get_org_id(token: str = Depends(oauth2_scheme)) -> int:
    """Resolve the caller's organization from the bearer token on an HTTP request.

    Unlike get_connection_org_id this declares the OAuth2 scheme, so it shows up as
    a secured endpoint in Swagger and the Authorize button applies to it.

    Args:
        token: Bearer token supplied by the OAuth2 scheme.

    Returns:
        The org_id claim on the token.
    """
    claims = decode_access_token(token)
    if not claims or not claims.get("sub"):
        raise _CREDENTIALS_EXC
    org_id = claims.get("org_id")
    if org_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No organization is associated with this session.",
        )
    return org_id


def get_mapping_id(token: str = Depends(oauth2_scheme)) -> int:
    """Resolve the caller's user-org mapping id from the bearer token.

    Args:
        token: Bearer token supplied by the OAuth2 scheme.

    Returns:
        The mapping_id claim on the token.
    """
    claims = decode_access_token(token)
    if not claims or not claims.get("sub"):
        raise _CREDENTIALS_EXC
    mapping_id = claims.get("mapping_id")
    if mapping_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No organization membership is associated with this session. Please sign in again.",
        )
    return mapping_id


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
