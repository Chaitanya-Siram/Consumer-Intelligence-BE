from sqlalchemy.orm import Session

from db_helpers.models.user_model import UserModel
from db_helpers.models.organization_model import OrganizationModel
from db_helpers.models.user_org_mapping_model import UserOrgMappingModel

# Fields a client is allowed to change via update_user.
_UPDATABLE_FIELDS = {"name", "email", "is_active"}


def create_user(
    db: Session,
    name: str,
    email: str,
    hashed_password: str | None = None,
) -> UserModel:
    user = UserModel(name=name, email=email, hashed_password=hashed_password)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user(db: Session, user_id: int) -> UserModel | None:
    return db.query(UserModel).filter(UserModel.id == user_id).first()


def get_user_by_email(db: Session, email: str) -> UserModel | None:
    return db.query(UserModel).filter(UserModel.email == email).first()


def list_users(
    db: Session,
    organization_id: int | None = None,
    include_inactive: bool = True,
    skip: int = 0,
    limit: int = 100,
) -> list[UserModel]:
    """List users, newest first, optionally restricted to one organization.

    Args:
        db: Database session.
        organization_id: Only return members of this org. None lists all users.
        include_inactive: Include deactivated users.
        skip: Rows to skip.
        limit: Max rows to return.

    Returns:
        List of matching users.
    """
    query = db.query(UserModel)
    if organization_id is not None:
        query = query.join(
            UserOrgMappingModel, UserOrgMappingModel.user_id == UserModel.id
        ).filter(UserOrgMappingModel.organization_id == organization_id)
    if not include_inactive:
        query = query.filter(UserModel.is_active.is_(True))
    return (
        query.order_by(UserModel.created_at.desc()).offset(skip).limit(limit).all()
    )


def update_user(db: Session, user: UserModel, **fields) -> UserModel:
    """Partial update: only the whitelisted fields actually passed are applied."""
    for key, value in fields.items():
        if key in _UPDATABLE_FIELDS:
            setattr(user, key, value)
    db.commit()
    db.refresh(user)
    return user


def set_password(db: Session, user: UserModel, hashed_password: str) -> UserModel:
    user.hashed_password = hashed_password
    db.commit()
    db.refresh(user)
    return user


def delete_user(db: Session, user: UserModel) -> None:
    """Hard-delete a user. Org mappings referencing it are removed by the
    ON DELETE CASCADE on user_org_mapping.user_id."""
    db.delete(user)
    db.commit()


# --- Organization membership --------------------------------------------------

def add_user_to_org(
    db: Session, user_id: int, organization_id: int, role: str = "analyst"
) -> UserOrgMappingModel:
    mapping = UserOrgMappingModel(
        user_id=user_id, organization_id=organization_id, role=role
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def get_mapping(
    db: Session, user_id: int, organization_id: int
) -> UserOrgMappingModel | None:
    return (
        db.query(UserOrgMappingModel)
        .filter(
            UserOrgMappingModel.user_id == user_id,
            UserOrgMappingModel.organization_id == organization_id,
        )
        .first()
    )


def get_default_mapping(db: Session, user_id: int) -> UserOrgMappingModel | None:
    """The user's default organization membership: the one flagged `is_default`,
    falling back to the earliest one they were added to."""
    return (
        db.query(UserOrgMappingModel)
        .filter(UserOrgMappingModel.user_id == user_id)
        .order_by(UserOrgMappingModel.is_default.desc(), UserOrgMappingModel.id.asc())
        .first()
    )


def set_default_mapping(
    db: Session, user_id: int, mapping: UserOrgMappingModel
) -> UserOrgMappingModel:
    """Mark `mapping` as the user's default org, clearing the flag on their others."""
    if not mapping.is_default:
        (
            db.query(UserOrgMappingModel)
            .filter(
                UserOrgMappingModel.user_id == user_id,
                UserOrgMappingModel.is_default.is_(True),
            )
            .update({UserOrgMappingModel.is_default: False})
        )
        mapping.is_default = True
        db.commit()
        db.refresh(mapping)
    return mapping


def list_user_orgs(db: Session, user_id: int) -> list[OrganizationModel]:
    return (
        db.query(OrganizationModel)
        .join(
            UserOrgMappingModel,
            UserOrgMappingModel.organization_id == OrganizationModel.id,
        )
        .filter(UserOrgMappingModel.user_id == user_id)
        .all()
    )


def remove_user_from_org(db: Session, mapping: UserOrgMappingModel) -> None:
    db.delete(mapping)
    db.commit()
