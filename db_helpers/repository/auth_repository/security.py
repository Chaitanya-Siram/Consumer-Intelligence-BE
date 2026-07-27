"""Password hashing (bcrypt) and JWT access-token helpers."""
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any, Optional
import bcrypt
import jwt
from configs import envs, logger


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt and return the UTF-8 digest."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash."""
    if not hashed_password:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(
    subject: str | int,
    expires_minutes: Optional[int] = None,
    extra_claims: Optional[dict[str, Any]] = None,
) -> str:
    """Build a signed JWT whose `sub` is the user id."""
    expire_minutes = expires_minutes or envs.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, envs.JWT_SECRET_KEY, algorithm=envs.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """Decode and validate a JWT. Returns the claims, or None if invalid/expired."""
    try:
        return jwt.decode(token, envs.JWT_SECRET_KEY, algorithms=[envs.JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        logger.debug(f"JWT decode failed: {exc}")
        return None



def generate_refresh_token() -> str:
    """Generate a new opaque, URL-safe refresh token (the raw value handed to the client)."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw_token: str) -> str:
    """Hash a raw refresh token for storage/lookup. Only the hash is persisted."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def refresh_token_expiry() -> datetime:
    """Absolute expiry timestamp for a newly issued refresh token."""
    return datetime.now(timezone.utc) + timedelta(days=envs.REFRESH_TOKEN_EXPIRE_DAYS)