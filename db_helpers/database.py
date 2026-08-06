import json
import math

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from configs import envs, logger

# All tables live under a dedicated schema (default "ai_solution") instead of
# PostgreSQL's "public". Setting the schema on the shared MetaData applies it to
# every model and automatically resolves string ForeignKey references within it.
DB_SCHEMA = envs.DB_SCHEMA
Base = declarative_base(metadata=MetaData(schema=DB_SCHEMA))


def _json_safe(value):
    """Recursively replace NaN/Infinity floats with None.

    Uploaded files often contain empty cells, which pandas represents as float
    NaN. Python's json.dumps emits a bare `NaN`/`Infinity` token, which is invalid
    JSON and rejected by PostgreSQL's JSONB. This turns them into null instead.
    """
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _json_serializer(value) -> str:
    """JSON serializer for all JSON/JSONB columns — NaN/Inf-safe."""
    return json.dumps(_json_safe(value))


DATABASE_URL = f'postgresql://{envs.DB_USER}:{envs.DB_PASSWORD}@{envs.DB_HOST}:{envs.DB_PORT}/{envs.DB_NAME}'
engine = create_engine(DATABASE_URL, json_serializer=_json_serializer)
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
    from .models import (
        project_model,
        session_model,
        organization_model,
        user_model,
        user_org_mapping_model,
        raw_article_model,
        tagged_article_model,
    )

    # pgvector backs the RAG chunk store (the chunk/embedding table itself is
    # created later by LlamaIndex). Enabling the extension here is idempotent and
    # needs a role with rds_superuser (the RDS master user has it). Guarded so a
    # missing privilege can't block startup before the RAG phase actually uses it.
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception:
        logger.warning(
            "Could not enable the pgvector extension; RAG features will be "
            "unavailable until it is installed.",
            exc_info=True,
        )

    Base.metadata.create_all(bind=engine)
