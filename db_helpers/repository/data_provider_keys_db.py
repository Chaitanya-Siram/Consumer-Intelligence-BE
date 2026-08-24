from sqlalchemy.orm import Session

from db_helpers.models.data_providers_model import (
    DataProvidersAPIKeyModel,
    DataProvidersAPIModel,
)
from db_helpers.repository.auth_repository.crypto import decrypt_secret, encrypt_secret

# Credential fields encrypted before they are written to the database.
_SECRET_FIELDS = {"api_key", "username", "password"}

# Fields a client is allowed to change via update_provider_key.
_UPDATABLE_FIELDS = _SECRET_FIELDS | {"data_provider_id", "is_active"}


def list_active_data_providers(db: Session) -> list[DataProvidersAPIModel]:
    """List every active data provider, by name.

    Args:
        db: Database session.

    Returns:
        Active DataProvidersAPIModel rows.
    """
    return (
        db.query(DataProvidersAPIModel)
        .filter(DataProvidersAPIModel.is_active.is_(True))
        .order_by(DataProvidersAPIModel.name)
        .all()
    )


def create_provider_key(
    db: Session,
    org_id: int,
    data_provider_id: int,
    api_key: str,
    username: str | None = None,
    password: str | None = None,
) -> DataProvidersAPIKeyModel:
    """Store a new credential set with its secrets encrypted at rest.

    Args:
        db: Database session.
        org_id: Owning organization id.
        data_provider_id: Provider the credentials belong to.
        api_key: Plaintext API key.
        username: Optional plaintext username.
        password: Optional plaintext password.

    Returns:
        The persisted DataProvidersAPIKeyModel row.
    """
    provider_key = DataProvidersAPIKeyModel(
        org_id=org_id,
        data_provider_id=data_provider_id,
        api_key=encrypt_secret(api_key),
        username=encrypt_secret(username),
        password=encrypt_secret(password),
    )
    db.add(provider_key)
    db.commit()
    db.refresh(provider_key)
    return provider_key


def get_provider_key(db: Session, key_id: int, org_id: int) -> DataProvidersAPIKeyModel | None:
    """Fetch one credential set by id.

    Args:
        db: Database session.
        key_id: Row id.

    Returns:
        The row, or None if it does not exist.
    """
    return (
        db.query(DataProvidersAPIKeyModel)
        .filter(DataProvidersAPIKeyModel.id == key_id, DataProvidersAPIKeyModel.org_id == org_id)
        .first()
    )


def list_provider_keys(
    db: Session,
    org_id: int | None = None,
    data_provider_id: int | None = None,
    include_inactive: bool = True,
    skip: int = 0,
    limit: int = 100,
) -> list[DataProvidersAPIKeyModel]:
    """List credential sets, newest first.

    Args:
        db: Database session.
        org_id: Restrict to this organization when given.
        data_provider_id: Restrict to this provider when given.
        include_inactive: Include deactivated rows.
        skip: Rows to skip.
        limit: Max rows to return.

    Returns:
        Matching DataProvidersAPIKeyModel rows.
    """
    query = db.query(DataProvidersAPIKeyModel)
    if org_id is not None:
        query = query.filter(DataProvidersAPIKeyModel.org_id == org_id)
    if data_provider_id is not None:
        query = query.filter(DataProvidersAPIKeyModel.data_provider_id == data_provider_id)
    if not include_inactive:
        query = query.filter(DataProvidersAPIKeyModel.is_active.is_(True))
    return (
        query.order_by(DataProvidersAPIKeyModel.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def update_provider_key(
    db: Session, provider_key: DataProvidersAPIKeyModel, **fields
) -> DataProvidersAPIKeyModel:
    """Partial update: only whitelisted fields are applied, secrets re-encrypted.

    Args:
        db: Database session.
        provider_key: Row to update.
        **fields: Plaintext field values to apply.

    Returns:
        The refreshed row.
    """
    for key, value in fields.items():
        if key not in _UPDATABLE_FIELDS:
            continue
        setattr(provider_key, key, encrypt_secret(value) if key in _SECRET_FIELDS else value)
    db.commit()
    db.refresh(provider_key)
    return provider_key


def delete_provider_key(db: Session, provider_key: DataProvidersAPIKeyModel) -> None:
    """Hard-delete a credential set.

    Args:
        db: Database session.
        provider_key: Row to delete.
    """
    db.delete(provider_key)
    db.commit()


def get_decrypted_credentials(provider_details: DataProvidersAPIKeyModel) -> dict[str, str | None]:
    """Decrypt a credential set for internal use when calling the provider.

    Args:
        provider_key: Row holding the encrypted values.

    Returns:
        Dict with plaintext api_key, username and password.
    """
    return {
        "api_key": decrypt_secret(provider_details.api_key),
        "username": decrypt_secret(provider_details.username),
        "password": decrypt_secret(provider_details.password),
    }
