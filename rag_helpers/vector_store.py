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


@lru_cache
def get_vector_store():
    from llama_index.vector_stores.postgres import PGVectorStore

    return PGVectorStore.from_params(
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
