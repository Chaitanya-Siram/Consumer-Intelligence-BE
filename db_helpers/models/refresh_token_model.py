from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, func

from db_helpers.database import Base


class RefreshTokenModel(Base):
    """A revocable, DB-stored refresh token. Only the SHA-256 hash of the raw
    token is persisted — the raw value is shown to the client exactly once."""
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
