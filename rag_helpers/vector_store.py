"""PGVectorStore setup (hybrid: dense pgvector + sparse tsvector).

Points at the application's own Postgres (same RDS as everything else) and lives
in the ``DB_SCHEMA`` schema. LlamaIndex owns the physical chunk + embedding table
(``data_<VECTOR_TABLE_NAME>``) and, with ``hybrid_search=True``, manages a
tsvector column for keyword search. Tenant isolation is enforced upstream in
``retriever.py`` via metadata filters (session_id / project_id).
"""

from __future__ import annotations

from functools import lru_cache

from configs import envs


def _build_engines(store, search_path: str) -> None:
    """Build PGVectorStore's two engines with the app's search_path applied.

    PGVectorStore creates a sync (psycopg2) and an async (asyncpg) engine and
    hands both the same ``create_engine_kwargs``, but the drivers spell the
    search_path differently — psycopg2 takes ``options="-csearch_path=..."``
    while asyncpg has no ``options`` argument at all and raises "connect() got an
    unexpected keyword argument 'options'". So build each engine here instead.

    It has to be a connection-time setting rather than a ``SET`` afterwards:
    asyncpg prepares statements server-side, and an unqualified ``vector`` type
    and its ``<=>`` operator only resolve if the schema is already on the path
    when the statement is prepared.

    Args:
        store: The PGVectorStore instance.
        search_path: Comma-separated schema list.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    store._engine = create_engine(
        store.connection_string,
        echo=store.debug,
        connect_args={"options": f"-csearch_path={search_path}"},
    )
    store._session = sessionmaker(store._engine)

    store._async_engine = create_async_engine(
        store.async_connection_string,
        connect_args={"server_settings": {"search_path": search_path}},
    )
    store._async_session = sessionmaker(store._async_engine, class_=AsyncSession)

    # _connect() would rebuild both engines without these settings; the store
    # calls it lazily on first use, so make it a no-op now that we're set up.
    store._connect = lambda: None


@lru_cache
def get_vector_store():
    from llama_index.vector_stores.postgres import PGVectorStore

    store = PGVectorStore.from_params(
        host=envs.DB_HOST,
        port=str(envs.DB_PORT or 5432),
        database=envs.DB_NAME,
        user=envs.DB_USER,
        password=envs.DB_PASSWORD,
        schema_name=envs.DB_SCHEMA,
        table_name=envs.VECTOR_TABLE_NAME,
        embed_dim=envs.EMBED_DIM,
        hybrid_search=True,
        text_search_config="english",
        # IVFFlat keeps writes cheap; switch to HNSW for larger corpora.
        hnsw_kwargs=None,
    )
    _build_engines(store, f"{envs.DB_SCHEMA},public")
    return store
