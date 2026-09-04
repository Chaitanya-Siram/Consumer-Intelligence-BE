from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from configs import logger
from db_helpers.database import get_db
from db_helpers.models.data_providers_model import *
from db_helpers.models.user_model import CurrentUser
from db_helpers.repository.auth_repository.crypto import mask_secret
from db_helpers.repository.auth_repository.dependencies import (
    ORG_ADMIN_EXC,
    get_current_user,
    require_org_admin,
)
from db_helpers.repository.data_provider_keys_db import (
    _CREDENTIAL_FIELDS,
    _check_required_credentials,
    _merged_credentials,
    create_provider_key,
    delete_provider_key,
    get_org_active_data_providers,
    get_provider_key,
    list_active_data_providers,
    list_provider_keys,
    update_provider_key,
)

router = APIRouter(tags=["data-provider-keys"])


@router.get("/data-providers", response_model=list[DataProvidersAPIResponse])
def get_data_providers(
    db: Session = Depends(get_db),
    _: CurrentUser = Depends(get_current_user),
) -> list[DataProvidersAPIModel]:
    """Get all active data providers."""
    return list_active_data_providers(db)


def _to_response(provider_key: DataProvidersAPIKeyModel) -> DataProvidersAPIKeyResponse:
    """Build the API response with secrets masked instead of decrypted.

    Args:
        provider_key: Row holding the encrypted credentials.

    Returns:
        A response model whose secret fields are masked previews.
    """
    response = DataProvidersAPIKeyResponse.model_validate(provider_key)
    response.api_key = mask_secret(provider_key.api_key)
    response.username = mask_secret(provider_key.username)
    response.password = mask_secret(provider_key.password)
    return response


@router.post("/data-provider-keys", response_model=DataProvidersAPIKeyResponse, status_code=status.HTTP_201_CREATED)
def create(
    payload: DataProviderKeyCreate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_org_admin),
) -> DataProvidersAPIKeyResponse:
    """Store credentials for a data provider. Admins of `org_id` only."""
    try:
        org_id = payload.org_id
        if not current_user.is_superadmin:
            org_id = current_user.org_id
        _check_required_credentials(db, payload.data_provider_id, payload)
        provider_key = create_provider_key(
            db,
            org_id=org_id,
            data_provider_id=payload.data_provider_id,
            api_key=payload.api_key,
            username=payload.username,
            password=payload.password,
        )
        logger.info(
            f"Created data provider key id={provider_key.id} org_id={org_id} "
            f"data_provider_id={payload.data_provider_id}"
        )
        return _to_response(provider_key)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Credentials for this data provider already exist. Update them instead.",
        )


@router.get("/data-provider-keys", response_model=list[DataProvidersAPIKeyResponse])
def list_all(
    org_id: int = Query(description="Filter by organization."),
    data_provider_id: Optional[int] = Query(None, description="Filter by data provider."),
    include_inactive: bool = Query(True, description="Include deactivated credentials."),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_org_admin),
) -> list[DataProvidersAPIKeyResponse]:
    """List stored credentials (masked), newest first. Restricted to organizations
    the caller administers."""

    if not current_user.is_superadmin:
        org_id = current_user.org_id

    keys = list_provider_keys(
        db,
        org_id=org_id,
        data_provider_id=data_provider_id,
        include_inactive=include_inactive,
        skip=skip,
        limit=limit,
    )
    
    return [_to_response(key) for key in keys]


@router.put("/data-provider-keys/{key_id}", response_model=DataProvidersAPIKeyResponse)
def update(
    key_id: int,
    payload: DataProviderKeyUpdate,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_org_admin),
) -> DataProvidersAPIKeyResponse:
    """Partially update a credential set. Admins of its organization only."""

    org_id = current_user.org_id
    record = get_provider_key(db, key_id, org_id)

    if not record:
        raise HTTPException(status_code=404, detail="No record found.")

    fields = payload.model_dump(exclude_unset=True)
    # A blank secret means "leave it alone" (the field is disabled or untouched in
    # the form), never "erase the stored one".
    for field in _CREDENTIAL_FIELDS:
        if field in fields and fields[field] is None:
            del fields[field]
    if not fields:
        raise HTTPException(status_code=400, detail="No fields provided to update.")

    _check_required_credentials(
        db,
        fields.get("data_provider_id", record.data_provider_id),
        _merged_credentials(record, fields),
    )

    try:
        provider_key = update_provider_key(db, record, **fields)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Credentials for this data provider already exist. Update them instead.",
        )

    logger.info(f"Updated data provider key id={key_id}: {sorted(fields)}")
    return _to_response(provider_key)


@router.delete("/data-provider-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(
    key_id: int,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_org_admin),
) -> None:
    """Delete a credential set. Admins of its organization only."""
    org_id = current_user.org_id
    record = get_provider_key(db, key_id, org_id)

    if not record:
        raise HTTPException(status_code=404, detail="No record found.")

    delete_provider_key(db, record)
    logger.info(f"Deleted data provider key id={key_id}")


@router.get("/data-provider-keys/active")
def get_list_active_api_key(
    org_id: int = Query(description="Filter by organization."),
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(require_org_admin),
):
    """API to get All active data providers in Organization"""
    if not current_user.is_superadmin:
        org_id = current_user.org_id
    records = get_org_active_data_providers(db, org_id)
    return records