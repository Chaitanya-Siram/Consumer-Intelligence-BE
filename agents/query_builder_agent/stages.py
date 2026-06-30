"""Stage handlers for the query-builder agent.

Each handler turns the running conversation into one model call and returns a
normalized envelope: {"message": str, "data": dict, "complete": bool}. The model
does the reasoning; these functions just frame the prompt (stage instructions +
current known state + transcript) and defend against malformed output.

The whole conversation is sent as a rendered transcript inside the user turn,
which keeps both providers (Claude / Azure OpenAI) behaving identically through
the shared `complete_json` surface — no provider-specific multi-turn handling.
"""
from __future__ import annotations

import json
from typing import Any

from configs import logger
from db_helpers.models.agent_state_model import AgentState
from agents.chart_generator.llm_client import complete_json, complete_json_web
from agents.query_builder_agent.prompts import (
    COMPETITOR_RESEARCH,
    EXTRACT_QUERIES,
    OUTPUT_CONTRACT,
    STAGE_PROMPTS,
)

# Smart-quote → straight-quote map (search APIs need straight quotes).
_QUOTE_MAP = {
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "′": "'", "″": '"',
}


def normalize_quotes(text: str) -> str:
    for bad, good in _QUOTE_MAP.items():
        text = text.replace(bad, good)
    return text


def _known_state(state: AgentState) -> str:
    """Compact snapshot of what we've learned, injected into the system prompt so
    the model has structured context beyond the raw transcript."""
    snapshot = {
        "brand": state.brand,
        "topics": state.topics,
        "geography": state.geography or "Global",
        "competitors": state.competitors,
        "query_groups": state.query_groups,
    }
    return json.dumps(snapshot, ensure_ascii=False)


def _render_transcript(history: list[dict[str, Any]]) -> str:
    if not history:
        return "(no messages yet)"
    lines = []
    for m in history:
        role = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content', '')}")
    return "\n".join(lines)


def _normalize(raw: Any) -> dict[str, Any]:
    """Coerce model output into the strict envelope, with safe fallbacks."""
    if not isinstance(raw, dict):
        return {"message": str(raw or "").strip(), "data": {}, "complete": False, "options": []}
    message = str(raw.get("message") or "").strip()
    data = raw.get("data")
    if not isinstance(data, dict):
        data = {}
    raw_options = raw.get("options")
    options = [str(o).strip() for o in raw_options if str(o).strip()] if isinstance(raw_options, list) else []
    return {"message": message, "data": data, "complete": bool(raw.get("complete")), "options": options}


def run_stage(state: AgentState, kickoff: bool) -> dict[str, Any]:
    """Run the model for the current stage and return the normalized envelope.

    `kickoff=True` means we just entered this stage with no new user input, so the
    model should produce the stage's opening message (probe / queries / competitor
    list). The transcript already contains the latest user message otherwise.
    """
    prompt = STAGE_PROMPTS.get(state.stage)
    if prompt is None:
        return {"message": "", "data": {}, "complete": True, "options": []}

    system = f"{prompt}\n{OUTPUT_CONTRACT}\n\nCURRENT KNOWN STATE (JSON):\n{_known_state(state)}"
    instruction = (
        "Begin this stage now — produce your opening message per your instructions."
        if kickoff
        else "Respond to the latest user message per your instructions."
    )
    user = (
        f"CONVERSATION SO FAR:\n{_render_transcript(state.history)}\n\n"
        f"{instruction}\nRespond with the JSON object only."
    )

    try:
        raw = complete_json(system, user, max_tokens=4096)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"query-builder stage {state.stage} LLM call failed: {exc}")
        return {
            "message": "Sorry — I hit a problem generating that. Could you rephrase or try again?",
            "data": {},
            "complete": False,
            "options": [],
        }
    return _normalize(raw)


def apply_stage_data(state: AgentState, data: dict[str, Any]) -> None:
    """Merge whichever fields are present (non-empty) into the running state.

    Stage-agnostic so Stage 4 can edit any field. Empty lists/dicts are ignored so
    a carry-forward turn never wipes a previously-set value.
    """
    brand = data.get("brand")
    if isinstance(brand, str) and brand.strip():
        state.brand = brand.strip()

    topics = data.get("topics")
    if isinstance(topics, list) and topics:
        state.topics = [str(t).strip() for t in topics if str(t).strip()]

    geo = data.get("geography")
    if isinstance(geo, str) and geo.strip():
        state.geography = geo.strip()

    competitors = data.get("competitors")
    if isinstance(competitors, list) and competitors:
        state.competitors = [str(c).strip() for c in competitors if str(c).strip()]

    groups = data.get("query_groups")
    if isinstance(groups, list) and groups:
        normalized = normalize_query_groups({"query_groups": groups})[0]
        if normalized:
            state.query_groups = normalized


# ---------------------------------------------------------------------------
# Bulk query-spec import (paste → structured, grouped query lists)
# ---------------------------------------------------------------------------

def normalize_query_groups(raw: Any) -> tuple[list[dict[str, Any]], str]:
    """Clean the extractor's output: straighten quotes, trim, drop empties, and
    de-duplicate queries within each group (case-insensitively, first wins).
    Returns (groups, brand)."""
    if not isinstance(raw, dict):
        return [], ""
    raw_groups = raw.get("query_groups")
    groups: list[dict[str, Any]] = []
    if isinstance(raw_groups, list):
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            label = str(g.get("label") or "Queries").strip() or "Queries"
            raw_q = g.get("queries")
            if not isinstance(raw_q, list):
                continue
            seen: set[str] = set()
            queries: list[str] = []
            for q in raw_q:
                s = normalize_quotes(str(q)).strip()
                if not s:
                    continue
                key = s.lower()
                if key in seen:
                    continue
                seen.add(key)
                queries.append(s)
            if queries:
                groups.append({"label": label, "queries": queries})
    brand = normalize_quotes(str(raw.get("brand") or "")).strip()
    return groups, brand


def extract_query_groups(text: str) -> tuple[list[dict[str, Any]], str]:
    """Extract grouped query lists (and a best-guess brand) from a pasted spec.
    Returns ([], "") on failure so callers can fall back to normal handling."""
    try:
        raw = complete_json(EXTRACT_QUERIES, text, max_tokens=8192)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"query spec extraction failed: {exc}")
        return [], ""
    return normalize_query_groups(raw)


# ---------------------------------------------------------------------------
# Web-grounded competitor research
# ---------------------------------------------------------------------------

def _competitor_context(state: AgentState) -> str:
    """Build the research context from the brand and the user's queries — group
    labels plus the queries from any competitor-looking group (these usually name
    the real rivals: drugs, products, companies)."""
    lines = [f"Primary brand: {state.brand}"]
    if state.topics:
        lines.append(f"Topics monitored: {', '.join(state.topics)}")
    labels = [str(g.get("label") or "") for g in state.query_groups]
    if labels:
        lines.append("Query group labels: " + "; ".join(l for l in labels if l))
    for g in state.query_groups:
        label = str(g.get("label") or "").lower()
        if "competitor" in label or "rival" in label:
            qs = [str(q) for q in (g.get("queries") or [])]
            if qs:
                lines.append(f"Competitor-related queries under '{g.get('label')}':")
                lines.extend(f"  - {q}" for q in qs[:40])
    return "\n".join(lines)


def research_competitors(state: AgentState) -> list[str]:
    """Find the brand's main real-world competitors via live web search, seeded by
    the competitor names already present in the user's queries. Returns up to 8
    distinct names (excluding the brand). Returns [] on any failure."""
    if not state.brand.strip():
        return []
    try:
        raw = complete_json_web(COMPETITOR_RESEARCH, _competitor_context(state), max_tokens=2048, max_uses=5)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"competitor research failed: {exc}")
        return []

    names = raw.get("competitors") if isinstance(raw, dict) else None
    if not isinstance(names, list):
        return []

    brand_lc = state.brand.strip().lower()
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        name = normalize_quotes(str(n)).strip()
        key = name.lower()
        if not name or key == brand_lc or key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out[:8]
