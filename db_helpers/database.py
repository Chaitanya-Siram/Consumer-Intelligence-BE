
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from configs import envs

Base = declarative_base()

DATABASE_URL = f'postgresql://{envs.DB_USER}:{envs.DB_PASSWORD}@{envs.DB_HOST}:{envs.DB_PORT}/{envs.DB_NAME}'
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create any tables that don't exist yet.

    Imports model modules first so Base.metadata knows about every table
    before create_all runs. Safe to call repeatedly — create_all is a no-op
    for tables that already exist.
    """
    from .models import project_model, session_model  # noqa: F401

    Base.metadata.create_all(bind=engine)
