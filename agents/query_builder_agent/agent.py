"""Query-builder agent — orchestrates the 4-stage intake flow.

The model handles all reasoning per stage (see stages.py); this module owns the
state machine: it appends turns to history, advances stages when the model marks
one complete, auto-runs the next stage's opening (queries / competitors / final),
and assembles the final config.

Usable two ways:
  - Programmatically (the WebSocket router): `begin_session()` then `process_turn()`.
  - As a CLI chatbot: `python -m agents.query_builder_agent.agent`.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from configs import logger
from db_helpers.models.agent_state_model import AgentState
from db_helpers.models.session_model import SessionType
from agents.chart_generator.llm_client import complete
from agents.query_builder_agent.stages import (
    apply_stage_data,
    extract_query_groups,
    research_competitors,
    run_stage,
)


def _advance(state: AgentState) -> None:
    """Mark the just-completed stage confirmed and move to the next one.

    Flow: 1 brand → 2 queries → 3 competitors → 4 refine (open).
    """
    if state.stage == 1:
        state.confirmed_intent = True        # brand confirmed
        state.stage = 2
    elif state.stage == 2:
        state.confirmed_queries = True       # queries confirmed
        state.stage = 3
    elif state.stage == 3:
        state.confirmed_competitors = True   # competitors confirmed
        state.stage = 4


def assemble_config(state: AgentState) -> dict[str, Any]:
    """Build the final media-monitoring configuration dict."""
    return {
        "brand": state.brand,
        "topics": state.topics,
        "geography": state.geography or "Global",
        "query_groups": state.query_groups,
        "competitors": state.competitors,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def flatten_queries(state: AgentState) -> list[dict[str, str]]:
    """Flatten query_groups into [{group, query}, ...] for the fetch layer, so each
    fetched article can be attributed to the group it matched."""
    flat: list[dict[str, str]] = []
    for group in state.query_groups:
        label = group.get("label") or "Queries"
        for q in group.get("queries") or []:
            flat.append({"group": label, "query": q})
    return flat


# Short affirmations that should reliably advance a stage even if the model is
# hesitant to set "complete": true on its own.
_APPROVALS = {
    "yes", "y", "yep", "yeah", "ok", "okay", "k", "sure", "confirm", "confirmed",
    "approve", "approved", "looks good", "lgtm", "good", "great", "perfect",
    "proceed", "go ahead", "go", "next", "continue", "done", "yes please",
}


def _is_approval(text: str) -> bool:
    t = text.strip().lower().rstrip("!.")
    if not t:
        return False
    if t in _APPROVALS:
        return True
    return any(t.startswith(a) for a in ("yes", "looks good", "approve", "confirm", "proceed", "go ahead"))


def _enter_competitors(state: AgentState) -> list[dict[str, Any]]:
    """Build the Competitors-stage kickoff deterministically (caller has set stage=3).

    Researches the brand's competitors (live web + the user's queries) and offers them
    as a SELECTABLE list only — nothing is added to state.competitors and no card is
    shown until the user actually picks. The confirmed card appears later, after the
    user's selection (see _save_prompt_events).
    """
    try:
        candidates = research_competitors(state)  # already deduped, brand excluded
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Could not research competitors: {exc}")
        candidates = []

    brand = state.brand or "your brand"
    if candidates:
        message = (
            f"Based on **{brand}**, your queries, and live web research, here are competitors "
            "you may want to monitor. Select the ones to monitor, add your own, or say “skip”."
        )
        options = candidates
    else:
        message = (
            f"Which competitors would you like to monitor for **{brand}**? "
            "List the brand names, or say “skip”."
        )
        options = []

    state.history.append({"role": "assistant", "content": message})
    return [{"type": "message", "message": message, "options": options}]


def _decide_competitors(state: AgentState, message: str) -> list[str] | None:
    """Resolve a Stage-3 competitor reply deterministically so a confirmation never
    bounces back through the model (which would re-offer the same picker).

    Returns the final competitor list, or None to defer to the model for free-form
    edits like "add Pfizer" / "drop Roche".
    """
    t = message.strip()
    low = t.lower()
    if low in {"skip", "none", "no", "no competitors", "skip competitors"} or low.startswith("skip"):
        return []  # user opted out of competitors
    # The option picker sends "My selection: A, B, C" (selected chips + custom adds).
    if low.startswith("my selection:") or low.startswith("selection:"):
        return [n.strip() for n in t.split(":", 1)[1].split(",") if n.strip()]
    # A plain "yes / looks good" keeps the pre-filled (researched) list as-is.
    if _is_approval(message):
        return list(state.competitors)
    return None


_NAME_SYSTEM = (
    "You name media-monitoring query sets. Given a brand, its competitors, topics and "
    "the search queries, reply with ONE short, human-readable label (3-6 words, Title "
    "Case) that captures what this monitoring set tracks. No quotes, no trailing "
    "punctuation, no explanation — return ONLY the name."
)


def generate_query_name(state: AgentState) -> str:
    """Ask the LLM for a short descriptive name for this query set. Falls back to a
    deterministic brand-based label if the model is unavailable or returns nothing."""
    fallback = (f"{state.brand} Media Monitoring".strip() if state.brand.strip()
                else "Media Monitoring Query")
    labels = [str(g.get("label")) for g in state.query_groups if g.get("label")]
    sample = [q for g in state.query_groups for q in (g.get("queries") or [])][:8]
    context = (
        f"Brand: {state.brand or '(unspecified)'}\n"
        f"Competitors: {', '.join(state.competitors) or '(none)'}\n"
        f"Topics: {', '.join(state.topics) or '(none)'}\n"
        f"Query groups: {', '.join(labels) or '(none)'}\n"
        f"Example queries: {'; '.join(sample) or '(none)'}"
    )
    try:
        raw = complete(_NAME_SYSTEM, context, max_tokens=40, temperature=0.0)
        name = (raw or "").strip().strip('"').splitlines()[0].strip() if raw else ""
        return name[:120] or fallback
    except Exception:  # noqa: BLE001
        logger.exception("Query-name generation failed; using fallback")
        return fallback


def build_session_payload(state: AgentState) -> dict[str, Any]:
    """Map the gathered config onto the Session DB columns (see create_session):
      brand        -> brand_keywords (as a single-item list)
      competitors  -> competitor_keywords
      topics       -> message_keywords
      query_groups -> queries
      session_type -> "query"
    An LLM-generated `name` labels the saved query set.
    """
    return {
        "session_type": SessionType.QUERY,
        "name": generate_query_name(state),
        "brand_keywords": [state.brand] if state.brand.strip() else [],
        "competitor_keywords": list(state.competitors),
        "message_keywords": list(state.topics),
        "queries": state.query_groups,
    }


def _save_prompt_events(state: AgentState) -> list[dict[str, Any]]:
    """Emit the confirmed competitors card, a summary, and a Save / Cancel prompt.
    The actual DB write happens in the transport when the user clicks Save."""
    total_q = sum(len(g.get("queries") or []) for g in state.query_groups)
    msg = (
        f"Here's the media-monitoring setup for **{state.brand or 'your brand'}** — "
        f"{len(state.competitors)} competitor(s) and {total_q} quer{'y' if total_q == 1 else 'ies'}. "
        "Save it to create a monitoring session, or cancel to keep editing."
    )
    state.history.append({"role": "assistant", "content": msg})
    events: list[dict[str, Any]] = []
    if state.competitors:
        events.append(_artifact_event(state, "competitors"))
    events.append({"type": "message", "message": msg, "options": []})
    events.append({"type": "confirm"})  # transport renders Save / Cancel buttons
    return events


# Turns are an ORDERED list of events the transport replays verbatim, so an
# artifact card (e.g. the confirmed brand box) lands exactly where it belongs —
# right after the message that confirmed it, before the next stage's prompt.

def _msg_event(result: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message", "message": result["message"], "options": result.get("options") or []}


def _artifact_event(state: AgentState, which: str) -> dict[str, Any]:
    if which == "brand":
        return {"type": "artifact", "artifact": "brand", "brand": state.brand}
    if which == "competitors":
        return {"type": "artifact", "artifact": "competitors", "competitors": state.competitors}
    return {
        "type": "artifact", "artifact": "query_groups",
        "query_groups": state.query_groups, "topics": state.topics, "geography": state.geography,
    }


# Which artifact a just-completed stage confirms.
_STAGE_ARTIFACT = {1: "brand", 2: "query_groups", 3: "competitors"}


def _fields(state: AgentState) -> dict[str, Any]:
    return {
        "brand": state.brand,
        "competitors": tuple(state.competitors),
        "topics_geo": (tuple(state.topics), state.geography),
        "query_groups": json.dumps(state.query_groups, sort_keys=True),
    }


def _stage_value_changed(state: AgentState, artifact: str, before: dict[str, Any]) -> bool:
    """Whether the value behind `artifact` differs from the start-of-turn snapshot."""
    now = _fields(state)
    if artifact == "brand":
        return now["brand"] != before["brand"]
    if artifact == "competitors":
        return now["competitors"] != before["competitors"]
    return now["query_groups"] != before["query_groups"] or now["topics_geo"] != before["topics_geo"]


# A pasted spec looks like queries when it's sizeable, multi-line, and carries
# search syntax (quotes / boolean / NEAR operators).
def _looks_like_query_spec(text: str) -> bool:
    if len(text) < 200:
        return False
    line_count = sum(1 for ln in text.splitlines() if ln.strip())
    quote_count = text.count('"') + text.count("“") + text.count("”")
    has_ops = any(op in text for op in (" OR ", " AND ", " NOT ", "NEAR/"))
    return line_count >= 4 and (has_ops or quote_count >= 6)


def _import_spec(state: AgentState, text: str) -> dict[str, Any] | None:
    """Extract a pasted query spec into grouped query lists, then move on to the
    COMPETITORS stage so the user can confirm competitors derived from those queries.
    Returns None if nothing could be extracted (caller falls back)."""
    groups, brand = extract_query_groups(text)
    if not groups:
        return None

    state.query_groups = groups
    if brand and not state.brand:
        state.brand = brand
    state.confirmed_intent = bool(state.brand)
    state.confirmed_queries = True
    state.stage = 3  # queries are settled — next we derive & confirm competitors

    total = sum(len(g.get("queries") or []) for g in groups)
    intro = (
        f"Imported your query spec — **{total}** queries across **{len(groups)}** group(s)"
        + (f" for **{state.brand}**" if state.brand else "")
        + ". Review them below — then let's settle the competitors to monitor."
    )
    state.history.append({"role": "assistant", "content": intro})

    events: list[dict[str, Any]] = [{"type": "message", "message": intro, "options": []}]
    if state.brand:
        events.append(_artifact_event(state, "brand"))
    events.append(_artifact_event(state, "query_groups"))

    # Move straight on to competitors: research web-grounded candidates from the brand
    # + the pasted queries, merge them in, and prompt the user to confirm (or skip).
    events.extend(_enter_competitors(state))

    return {"turns": events, "stage": state.stage, "final": None}


def begin_session(state: AgentState) -> dict[str, Any]:
    """Produce the agent's opening turn (Stage 1 — ask for the brand)."""
    result = run_stage(state, kickoff=True)
    apply_stage_data(state, result["data"])
    state.history.append({"role": "assistant", "content": result["message"]})
    return {"turns": [_msg_event(result)], "stage": state.stage, "final": None}


def process_turn(state: AgentState, user_message: str) -> dict[str, Any]:
    """Advance the conversation by one user message.

    Returns {"turns": [event, ...], "stage": int, "final": dict|None} where each
    event is {"type":"message", ...} or {"type":"artifact", ...}, in display order.

    Stage 4 is open-ended: the session never disconnects there — the user can ask
    questions or edit competitors/topics/queries, and "new"/"start over" resets.
    """
    user_message = (user_message or "").strip()

    # Explicit restart only — Stage 4 otherwise stays open for follow-ups/edits.
    if user_message.lower() in {"new", "restart", "start over", "new brand"}:
        state.__dict__.update(AgentState().__dict__)
        return begin_session(state)

    # Bulk paste of an existing query spec (any stage) → extract + import.
    if _looks_like_query_spec(user_message):
        state.history.append({"role": "user", "content": user_message})
        imported = _import_spec(state, user_message)
        if imported is not None:
            return imported
        # Extraction yielded nothing — fall through to normal handling.

    elif user_message:
        state.history.append({"role": "user", "content": user_message})

    # Stage-3 competitor confirmation is resolved deterministically (picker selection
    # / approval / skip) so a confirm never bounces back to the same picker. Free-form
    # edits ("add Pfizer") return None here and fall through to the model.
    if state.stage == 3 and user_message:
        decided = _decide_competitors(state, user_message)
        if decided is not None:
            state.competitors = decided
            _advance(state)  # Stage 3 → 4, confirmed_competitors = True
            return {"turns": _save_prompt_events(state), "stage": state.stage, "final": None}

    before = _fields(state)
    turns: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None

    result = run_stage(state, kickoff=False)
    apply_stage_data(state, result["data"])
    state.history.append({"role": "assistant", "content": result["message"]})
    turns.append(_msg_event(result))

    # The queries are settled once they're shown and the user approves — advance
    # reliably even when the model is hesitant to set "complete" itself.
    if state.stage == 2 and state.query_groups and _is_approval(user_message):
        result["complete"] = True

    # Cascade through any stages that complete on this turn, emitting the confirmed
    # stage's artifact card BEFORE the next stage's opening message.
    while result["complete"] and state.stage < 4:
        completed = state.stage
        _advance(state)
        # Only re-show the completed stage's card if its value actually changed this
        # turn (avoids a duplicate query card on a bare "looks good" approval).
        if _stage_value_changed(state, _STAGE_ARTIFACT[completed], before):
            turns.append(_artifact_event(state, _STAGE_ARTIFACT[completed]))
        if state.stage == 4:
            # Competitors confirmed via the model → summarise and prompt Save / Cancel.
            turns.extend(_save_prompt_events(state))
            break
        if state.stage == 3:
            # Deterministic competitor kickoff: research + merge web competitors,
            # present them as selectable options, then wait for the user's pick.
            turns.extend(_enter_competitors(state))
            break
        result = run_stage(state, kickoff=True)
        apply_stage_data(state, result["data"])
        state.history.append({"role": "assistant", "content": result["message"]})
        turns.append(_msg_event(result))

    # Surface any newly-populated / changed value as an artifact card, even when the
    # stage hasn't formally completed — so the user SEES the queries the agent just
    # generated (or competitors just confirmed) while reviewing, not only after the
    # stage advances. Skip anything the cascade already emitted this turn.
    after = _fields(state)
    if after != before:
        emitted = {ev.get("artifact") for ev in turns if ev.get("type") == "artifact"}
        if "brand" not in emitted and after["brand"] != before["brand"] and state.brand:
            turns.append(_artifact_event(state, "brand"))
        if ("query_groups" not in emitted
                and (after["query_groups"] != before["query_groups"] or after["topics_geo"] != before["topics_geo"])
                and state.query_groups):
            turns.append(_artifact_event(state, "query_groups"))
        if "competitors" not in emitted and after["competitors"] != before["competitors"] and state.competitors:
            turns.append(_artifact_event(state, "competitors"))
        # A change made at Stage 4 (refine) → re-offer Save / Cancel so the user can
        # persist the edited config (skip if a prompt was already emitted this turn).
        if state.stage == 4 and not any(ev.get("type") == "confirm" for ev in turns):
            turns.append({"type": "confirm"})

    return {"turns": turns, "stage": state.stage, "final": final}


# ---------------------------------------------------------------------------
# CLI chatbot
# ---------------------------------------------------------------------------

def _cli() -> None:
    try:
        from rich.console import Console
        from rich.json import JSON
        from rich.panel import Panel
        console = Console()
        rprint = console.print
    except Exception:  # noqa: BLE001 — rich optional
        console = None
        def rprint(*a, **k):  # type: ignore
            print(*a)

    def agent_say(turns: list[dict]) -> None:
        for t in turns:
            if t.get("type") == "artifact":
                art = t.get("artifact")
                if art == "brand":
                    body = f"Primary Brand: {t.get('brand')}"
                elif art == "competitors":
                    body = f"Competitors: {', '.join(t.get('competitors') or [])}"
                elif art == "query_groups":
                    lines = []
                    for g in t.get("query_groups") or []:
                        qs = g.get("queries") or []
                        lines.append(f"[{g.get('label')}] ({len(qs)})")
                        lines += [f"  - {q}" for q in qs]
                    body = "Query Groups:\n" + "\n".join(lines)
                else:
                    body = "Queries:\n" + json.dumps(t.get("queries") or {}, indent=2)
                if console:
                    rprint(Panel(body, title="✓ Confirmed", border_style="green"))
                else:
                    print(f"\n[Confirmed] {body}\n")
                continue
            msg = t.get("message", "")
            options = t.get("options") or []
            if options:
                msg = f"{msg}\n\nOptions: {', '.join(options)}"
            if console:
                rprint(Panel(msg, title="Agent", border_style="magenta"))
            else:
                print(f"\nAgent: {msg}\n")

    state = AgentState()
    agent_say(begin_session(state)["turns"])

    while True:
        try:
            user = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return
        if user.lower() in {"exit", "quit"}:
            print("Goodbye.")
            return
        if not user:
            continue

        result = process_turn(state, user)
        agent_say(result["turns"])

        if result["final"]:
            if console:
                rprint(Panel(JSON(json.dumps(result["final"])), title="Media Monitoring Config", border_style="green"))
            else:
                print(json.dumps(result["final"], indent=2))
            print("(Config saved to the output/ folder. Type 'new' for another brand or 'exit' to quit.)")


if __name__ == "__main__":
    _cli()
