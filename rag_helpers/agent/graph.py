"""LangGraph state machine wiring the retrieval agent together.

    analyze -> retrieve -> rerank -> grade -> [finish | rewrite -> retrieve ...]

The graph produces the reranked, relevance-checked context + an ``answerable``
flag. Final answer generation is done by the caller (the chat handler) so tokens
can flow over the WebSocket.
"""

from __future__ import annotations

from functools import lru_cache

from rag_helpers.agent.nodes import (
    analyze,
    decide_after_grade,
    grade,
    rerank,
    retrieve,
    rewrite,
)
from rag_helpers.agent.state import AgentState


@lru_cache
def get_agent():
    from langgraph.graph import END, StateGraph

    builder = StateGraph(AgentState)
    builder.add_node("analyze", analyze)
    builder.add_node("retrieve", retrieve)
    builder.add_node("rerank", rerank)
    builder.add_node("grade", grade)
    builder.add_node("rewrite", rewrite)

    builder.set_entry_point("analyze")
    builder.add_edge("analyze", "retrieve")
    builder.add_edge("retrieve", "rerank")
    builder.add_edge("rerank", "grade")
    builder.add_conditional_edges(
        "grade", decide_after_grade, {"finish": END, "rewrite": "rewrite"}
    )
    builder.add_edge("rewrite", "retrieve")
    return builder.compile()


async def run_retrieval(
    session_id: int | None,
    question: str,
    chat_history: list[dict[str, str]] | None = None,
    project_id: int | None = None,
) -> AgentState:
    """Execute the agent and return the final state (nodes + answerable).

    Isolation is enforced by session_id and/or project_id (at least one required
    by ``build_filters``)."""
    initial: AgentState = {
        "session_id": session_id,
        "project_id": project_id,
        "original_query": question,
        "chat_history": chat_history or [],
    }
    return await get_agent().ainvoke(initial)
