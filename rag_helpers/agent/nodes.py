"""Agent nodes: analyze -> retrieve -> rerank -> grade (-> rewrite -> retry)."""

from __future__ import annotations

import json

from configs import logger
from rag_helpers.agent.prompts import ANALYZE_PROMPT, GRADE_PROMPT, REWRITE_PROMPT
from rag_helpers.agent.state import AgentState
from rag_helpers.llm import get_llm
from rag_helpers.reranker import get_reranker
from rag_helpers.retriever import build_filters, get_retriever

_MAX_RETRIES = 1
_EXCERPT_CHARS = 600


def format_context(nodes: list) -> str:
    """Numbered excerpts for grading / generation prompts."""
    parts = []
    for i, nws in enumerate(nodes, start=1):
        meta = nws.node.metadata or {}
        title = meta.get("title") or "Untitled"
        text = nws.node.get_content()[:_EXCERPT_CHARS]
        parts.append(f"[{i}] {title}\n{text}")
    return "\n\n".join(parts)


def _history_text(history: list[dict[str, str]]) -> str:
    if not history:
        return "(none)"
    return "\n".join(f"{m['role']}: {m['content']}" for m in history[-6:])


async def analyze(state: AgentState) -> AgentState:
    """Condense the question into a standalone query and extract filters."""
    question = state["original_query"]
    prompt = ANALYZE_PROMPT.format(
        history=_history_text(state.get("chat_history", [])),
        question=question,
    )
    query = question
    filters: dict[str, str | None] = {}
    try:
        resp = await get_llm().acomplete(prompt)
        data = json.loads(_extract_json(resp.text))
        query = (data.get("standalone_query") or question).strip()
        filters = {
            "author": data.get("author"),
            "date_from": data.get("date_from"),
            "date_to": data.get("date_to"),
        }
    except Exception as exc:  # noqa: BLE001 - degrade gracefully to raw query
        logger.warning(f"analyze step failed, using raw query: {exc}")

    return {**state, "query": query, "filters": filters, "retries": 0}


async def retrieve(state: AgentState) -> AgentState:
    f = state.get("filters") or {}
    md_filters = build_filters(
        session_id=state.get("session_id"),
        project_id=state.get("project_id"),
        author=f.get("author"),
        date_from=f.get("date_from"),
        date_to=f.get("date_to"),
    )
    retriever = get_retriever(md_filters)
    nodes = await retriever.aretrieve(state["query"])
    return {**state, "nodes": nodes}


async def rerank(state: AgentState) -> AgentState:
    nodes = state.get("nodes") or []
    if not nodes:
        return {**state, "nodes": []}
    from llama_index.core import QueryBundle

    reranked = get_reranker().postprocess_nodes(
        nodes, query_bundle=QueryBundle(state["query"])
    )
    return {**state, "nodes": reranked}


async def grade(state: AgentState) -> AgentState:
    """CRAG-style relevance check on the reranked context."""
    nodes = state.get("nodes") or []
    if not nodes:
        return {**state, "answerable": False}

    prompt = GRADE_PROMPT.format(
        question=state["query"], context=format_context(nodes)
    )
    try:
        resp = await get_llm().acomplete(prompt)
        relevant = resp.text.strip().lower().startswith("y")
    except Exception as exc:  # noqa: BLE001 - on grader failure, trust retrieval
        logger.warning(f"grade step failed, assuming relevant: {exc}")
        relevant = True

    return {**state, "answerable": relevant}


async def rewrite(state: AgentState) -> AgentState:
    """Broaden the query and drop metadata filters, then retry retrieval."""
    prompt = REWRITE_PROMPT.format(question=state["original_query"])
    try:
        resp = await get_llm().acomplete(prompt)
        new_query = resp.text.strip() or state["query"]
    except Exception:  # noqa: BLE001
        new_query = state["original_query"]
    return {
        **state,
        "query": new_query,
        "filters": {},  # over-restrictive filters are a common failure cause
        "retries": state.get("retries", 0) + 1,
    }


def decide_after_grade(state: AgentState) -> str:
    """Route: relevant -> finish; not relevant & retries left -> rewrite; else finish."""
    if state.get("answerable"):
        return "finish"
    if state.get("retries", 0) < _MAX_RETRIES:
        return "rewrite"
    return "finish"


def _extract_json(text: str) -> str:
    """Pull the first {...} block out of an LLM response."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text
