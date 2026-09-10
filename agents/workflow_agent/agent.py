"""Workflow-builder agent orchestration.

The model drives the conversation and picks its own next action each turn; this module
owns the invariants it must not be able to skip — above all that a workflow is only ever
built from a boolean query the user explicitly approved.
"""
from typing import Any

from agents.workflow_agent.graph_builder import build_graph, summarize
from agents.workflow_agent.state import WorkflowAgentState
from agents.workflow_agent.turn import (
    apply_state_data,
    is_approval,
    query_warnings,
    run_turn,
)
from configs import logger
from db_helpers.workflow_validator import WorkflowValidationError, validate_workflow

OPENING_MESSAGE = (
    "What would you like to research? Tell me the brand or topic and what you want to "
    "learn — I'll set up the pipeline and check the search query with you before building."
)


def begin_session(state: WorkflowAgentState) -> list[dict[str, Any]]:
    """The opening turn. No model call, so the connection greets immediately.

    Args:
        state: Fresh conversation state.

    Returns:
        The turn's events.
    """
    state.history.append({"role": "assistant", "content": OPENING_MESSAGE})
    return [{"type": "agent", "message": OPENING_MESSAGE, "options": []}]


def _events_for(state: WorkflowAgentState, result: dict[str, Any],
                providers: dict[str, str]) -> list[dict[str, Any]]:
    """Turn the model's chosen action into client events, enforcing the query gate."""
    action = result["action"]
    message = result["message"]

    # A file workflow analyses an upload, so there is no query to confirm.
    needs_query = state.source_type != "file"

    if action == "build" and needs_query and not state.confirmed_queries:
        # The model tried to skip confirmation. Fall back to proposing.
        logger.info("Workflow agent: build before confirmation — downgrading to propose_query")
        action = "propose_query"
        if not result["queries"]:
            result["queries"] = state.queries

    if action == "propose_query" and not needs_query:
        # Nothing to search — the file is the data. Build it instead.
        action = "build"

    if action == "propose_query":
        queries = result["queries"] or state.queries
        if not queries:
            text = message or "I still need a search query before I can build this."
            state.history.append({"role": "assistant", "content": text})
            return [{"type": "agent", "message": text, "options": result["options"]}]
        state.queries = queries
        state.proposed_queries = list(queries)
        state.confirmed_queries = False
        text = message or "Here's the search query I'd use. Shall I build the workflow?"
        state.history.append(
            {"role": "assistant", "content": f"{text}\nQueries: {queries}"}
        )
        return [{
            "type": "query", "message": text, "queries": queries,
            "rationale": result["rationale"], "options": result["options"],
        }]

    if action == "build":
        try:
            graph = validate_workflow(build_graph(state, providers))
        except WorkflowValidationError as exc:
            logger.warning(f"Workflow agent produced an invalid graph: {exc}")
            text = f"I couldn't build a valid workflow: {exc}"
            state.history.append({"role": "assistant", "content": text})
            return [{"type": "error", "detail": text, "fatal": False}]
        summary = message or summarize(graph, providers)
        state.history.append({"role": "assistant", "content": summary})
        return [{"type": "workflow", "workflow": graph, "summary": summary}]

    text = message or "Could you tell me a bit more?"
    state.history.append({"role": "assistant", "content": text})
    return [{"type": "agent", "message": text, "options": result["options"]}]


def process_turn(
    state: WorkflowAgentState, message: str, providers: dict[str, str]
) -> list[dict[str, Any]]:
    """Handle one user message and return the events to send back.

    Args:
        state: Conversation state, updated in place.
        message: The user's message.
        providers: The org's active providers.

    Returns:
        The turn's events.
    """
    state.history.append({"role": "user", "content": message})

    # A plain "yes" to a proposed query is resolved here rather than by the model, so
    # confirmation can never be granted by anything but the user's own approval.
    if state.proposed_queries and is_approval(message):
        state.confirmed_queries = True
        state.queries = list(state.proposed_queries)

    result = run_turn(state, providers)

    if result["intent_summary"]:
        state.intent_summary = result["intent_summary"]
    apply_state_data(state, result["data"], providers)

    # A query that the fetchers would re-parse into something else returns the wrong
    # articles silently, so give the model the specific problems and one chance to fix it.
    if result["action"] == "propose_query" and result["queries"]:
        entities = ([state.brand] if state.brand else []) + state.competitors
        warnings = query_warnings(result["queries"], entities)
        if warnings:
            logger.info(f"Workflow agent: regenerating query — {warnings}")
            state.history.append({
                "role": "user",
                "content": (
                    "Your query would be mangled by the search backends:\n- "
                    + "\n- ".join(warnings)
                    + "\nRe-read the BOOLEAN QUERY RULES and propose a corrected query."
                ),
            })
            retried = run_turn(state, providers)
            state.history.pop()  # the correction was scaffolding, not conversation
            if retried["action"] == "propose_query" and retried["queries"]:
                result = retried
                warnings = query_warnings(retried["queries"], entities)

            # Still flawed after the retry: keep the queries that are clean rather
            # than shipping ones that would silently return the wrong articles.
            if warnings:
                clean = [
                    q for q in result["queries"] if not query_warnings([q], entities)
                ]
                if clean:
                    logger.info(
                        f"Workflow agent: dropping {len(result['queries']) - len(clean)} "
                        "unfixable query/queries"
                    )
                    result["queries"] = clean

    return _events_for(state, result, providers)
