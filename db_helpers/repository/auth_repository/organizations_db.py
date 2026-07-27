from sqlalchemy.orm import Session

from db_helpers.models.organization_model import OrganizationModel

# Fields a client is allowed to change via update_organization.
_UPDATABLE_FIELDS = {"name", "description", "is_active"}


def create_organization(
    db: Session, name: str, description: str | None = None
) -> OrganizationModel:
    organization = OrganizationModel(name=name, description=description)
    db.add(organization)
    db.commit()
    db.refresh(organization)
    return organization


def get_organization(db: Session, organization_id: int) -> OrganizationModel | None:
    return (
        db.query(OrganizationModel)
        .filter(OrganizationModel.id == organization_id)
        .first()
    )


def list_organizations(
    db: Session,
    include_inactive: bool = True,
    skip: int = 0,
    limit: int = 100,
) -> list[OrganizationModel]:
    query = db.query(OrganizationModel)
    if not include_inactive:
        query = query.filter(OrganizationModel.is_active.is_(True))
    return (
        query.order_by(OrganizationModel.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


def update_organization(
    db: Session, organization: OrganizationModel, **fields
) -> OrganizationModel:
    """Partial update: only the whitelisted fields actually passed are applied."""
    for key, value in fields.items():
        if key in _UPDATABLE_FIELDS:
            setattr(organization, key, value)
    db.commit()
    db.refresh(organization)
    return organization


def delete_organization(db: Session, organization: OrganizationModel) -> None:
    """Hard-delete an organization. User mappings referencing it are removed by the
    ON DELETE CASCADE on user_org_mapping.organization_id."""
    db.delete(organization)
    db.commit()
