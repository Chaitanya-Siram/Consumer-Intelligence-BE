"""Session/project-scoped hybrid retriever.

This is the single chokepoint for tenant isolation: every retriever is built with
a mandatory ``session_id`` and/or ``project_id`` metadata filter, so no endpoint
can accidentally query across tenants. Optional author / date filters are AND-ed
on top. (Ported from RAG_System, where the isolation key was ``user_id``.)
"""

from __future__ import annotations

from functools import lru_cache

from configs import envs
from rag_helpers.embeddings import get_embed_model
from rag_helpers.vector_store import get_vector_store


@lru_cache
def get_index():
    from llama_index.core import VectorStoreIndex

    return VectorStoreIndex.from_vector_store(
        vector_store=get_vector_store(),
        embed_model=get_embed_model(),
    )


def build_filters(
    *,
    session_id: int | None = None,
    project_id: int | None = None,
    author: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    """Build metadata filters, always scoped to a session and/or project.

    At least one of ``session_id`` / ``project_id`` is required so retrieval can
    never span tenants. Dates are matched lexically on the ISO-8601
    ``published_date`` string, which sorts correctly for YYYY-MM-DD values.
    """
    from llama_index.core.vector_stores import (
        FilterCondition,
        FilterOperator,
        MetadataFilter,
        MetadataFilters,
    )

    if session_id is None and project_id is None:
        raise ValueError("build_filters requires session_id and/or project_id for tenant isolation.")

    filters: list = []
    if session_id is not None:
        filters.append(
            MetadataFilter(key="session_id", value=str(session_id), operator=FilterOperator.EQ)
        )
    if project_id is not None:
        filters.append(
            MetadataFilter(key="project_id", value=str(project_id), operator=FilterOperator.EQ)
        )
    if author:
        filters.append(
            MetadataFilter(key="author", value=author, operator=FilterOperator.EQ)
        )
    if date_from:
        filters.append(
            MetadataFilter(key="published_date", value=date_from, operator=FilterOperator.GTE)
        )
    if date_to:
        filters.append(
            MetadataFilter(key="published_date", value=date_to, operator=FilterOperator.LTE)
        )
    return MetadataFilters(filters=filters, condition=FilterCondition.AND)


def get_retriever(filters):
    """Hybrid retriever (dense pgvector + sparse tsvector) for the given filters."""
    return get_index().as_retriever(
        similarity_top_k=envs.RETRIEVE_TOP_K,
        sparse_top_k=envs.RETRIEVE_TOP_K,
        vector_store_query_mode="hybrid",
        filters=filters,
    )
