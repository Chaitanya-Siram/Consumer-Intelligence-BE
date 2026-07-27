from datetime import datetime, timezone

from sqlalchemy.orm import Session

from db_helpers.repository.auth_repository.security import (
    generate_refresh_token,
    hash_refresh_token,
    refresh_token_expiry,
)
from db_helpers.models.refresh_token_model import RefreshTokenModel


def create_refresh_token(db: Session, user_id: int) -> tuple[RefreshTokenModel, str]:
    """Issue a new refresh token for a user. Returns (record, raw_token).
    The raw token is only available here — the DB stores just its hash."""
    raw_token = generate_refresh_token()
    record = RefreshTokenModel(
        user_id=user_id,
        token_hash=hash_refresh_token(raw_token),
        expires_at=refresh_token_expiry(),
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record, raw_token


def get_active_refresh_token(db: Session, raw_token: str) -> RefreshTokenModel | None:
    """Look up a refresh token by its raw value, returning it only if it is
    valid: exists, not revoked, and not expired."""
    record = (
        db.query(RefreshTokenModel)
        .filter(RefreshTokenModel.token_hash == hash_refresh_token(raw_token))
        .first()
    )
    if record is None or record.revoked_at is not None:
        return None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        return None
    return record


def revoke_refresh_token(db: Session, record: RefreshTokenModel) -> None:
    """Mark a single refresh token as revoked (idempotent)."""
    if record.revoked_at is None:
        record.revoked_at = datetime.now(timezone.utc)
        db.commit()


def revoke_all_for_user(db: Session, user_id: int) -> int:
    """Revoke every active refresh token for a user (logout-everywhere).
    Returns the number of tokens revoked."""
    now = datetime.now(timezone.utc)
    count = (
        db.query(RefreshTokenModel)
        .filter(
            RefreshTokenModel.user_id == user_id,
            RefreshTokenModel.revoked_at.is_(None),
        )
        .update({RefreshTokenModel.revoked_at: now}, synchronize_session=False)
    )
    db.commit()
    return count
