"""Shared state for the retrieval agent graph."""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class AgentState(TypedDict, total=False):
    # Inputs — isolation is by session and/or project (was user_id in RAG_System).
    session_id: Optional[int]
    project_id: Optional[int]
    original_query: str
    chat_history: list[dict[str, str]]  # [{role, content}, ...]

    # Working state
    query: str                          # current (possibly rewritten) query
    filters: dict[str, str | None]      # author / date_from / date_to
    nodes: list[Any]                    # reranked NodeWithScore list
    retries: int

    # Output
    answerable: bool
