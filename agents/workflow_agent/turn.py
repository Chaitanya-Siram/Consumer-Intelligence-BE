"""The agent's per-turn model call, plus the helpers that fold its output into state.

One handler serves every turn: the model picks its own next action (ask / propose_query /
build / message) rather than walking a fixed script, so the conversation adapts to what
the user actually asked for.
"""
import json
from pathlib import Path
from typing import Any

from agents.chart_generator.llm_client import complete_json, complete_json_web
from agents.workflow_agent.provider_tool import (
    format_providers_block,
    provider_name,
    resolve_provider_names,
)
from agents.workflow_agent.state import WorkflowAgentState
from configs import logger

_PROMPTS = Path(__file__).parent / "prompts"
_TURN_PROMPT = (_PROMPTS / "turn_agent.txt").read_text(encoding="utf-8")
_COMPETITOR_PROMPT = (_PROMPTS / "competitor_research.txt").read_text(encoding="utf-8")

ACTIONS = ("ask", "propose_query", "build", "message")

# Slots whose change invalidates an already-confirmed query.
_QUERY_INPUTS = ("brand", "competitors", "providers")

_APPROVALS = {
    "yes", "y", "yep", "yeah", "ok", "okay", "k", "sure", "confirm", "confirmed",
    "approve", "approved", "looks good", "lgtm", "good", "great", "perfect",
    "proceed", "go ahead", "go", "next", "continue", "done", "build it", "yes please",
}
_APPROVAL_PREFIXES = ("yes", "looks good", "approve", "confirm", "proceed", "go ahead",
                      "build it", "that works", "sounds good")


def is_approval(text: str) -> bool:
    """Whether a reply is a plain affirmation, so a "yes" never round-trips the model.

    Args:
        text: The user's message.

    Returns:
        True when the message is an approval and nothing more.
    """
    t = (text or "").strip().lower().rstrip("!.")
    if not t:
        return False
    if t in _APPROVALS:
        return True
    # Match a prefix only on a word boundary, so "yesterday's data" isn't a "yes".
    return any(
        t == p or t.startswith(f"{p} ") or t.startswith(f"{p},")
        for p in _APPROVAL_PREFIXES
    )


def _clean_strings(value: Any) -> list[str]:
    """Non-blank strings, trimmed and de-duplicated in first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for v in value if isinstance(value, list) else []:
        s = str(v or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def apply_state_data(
    state: WorkflowAgentState, data: dict[str, Any], providers: dict[str, str]
) -> None:
    """Merge the fields this turn learned into the state.

    Only non-empty values are applied, so a carry-forward turn never wipes something
    already gathered. Changing a query input clears the query confirmation.

    Args:
        state: State to update in place.
        data: The model's "data" object for this turn.
        providers: The org's active providers, used to filter data sources.
    """
    if not isinstance(data, dict):
        return

    before = {f: getattr(state, f) for f in _QUERY_INPUTS}

    for field in ("brand", "title", "layout", "output_format", "llm"):
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            setattr(state, field, value.strip())

    for field in ("competitors", "message_themes", "lenses", "charts"):
        values = _clean_strings(data.get(field))
        if values:
            setattr(state, field, values)

    # The model works in provider names; the graph and fetcher work in labels.
    selected = resolve_provider_names(_clean_strings(data.get("providers")), providers)
    if selected:
        state.providers = selected

    if any(getattr(state, f) != before[f] for f in _QUERY_INPUTS):
        # The query was built from these; it no longer matches, so it must be re-proposed.
        if state.confirmed_queries:
            logger.info("Workflow agent: query inputs changed — clearing confirmation")
        state.confirmed_queries = False


# Generic words that match brand-free articles when searched on their own. Entity
# aliases (tickers, product names, executives) are fine in the OR position.
_TOPIC_WORDS = {
    "feedback", "sentiment", "coverage", "mentions", "buzz", "perception", "opinion",
    "review", "reviews", "complaint", "complaints", "praise", "experience", "rating",
    "ratings", "news", "media", "press", "article", "articles", "post", "posts",
    "recall", "lawsuit", "layoffs", "outbreak", "boycott", "earnings", "scandal",
    "crisis", "launch", "campaign", "customer", "customers", "service", "quality",
    "brand", "company", "product", "products",
}


# Words describing the analysis rather than the coverage. Unlike the rest of
# _TOPIC_WORDS ("recall", "lawsuit"), these never appear in an article, so requiring
# one with AND returns almost nothing.
_ANALYSIS_WORDS = {
    "feedback", "sentiment", "coverage", "mentions", "buzz", "perception", "opinion",
    "review", "reviews", "complaint", "complaints", "praise", "rating", "ratings",
    "news", "media", "press", "article", "articles", "post", "posts", "brand",
    # Modifiers that only ever pair with the above ("customer experience").
    "customer", "customers", "consumer", "consumers", "public", "online", "social",
    "experience", "satisfaction", "reputation", "image", "awareness", "discussion",
}


def _is_analysis_term(term: str) -> bool:
    """Whether an AND term describes the analysis instead of the coverage.

    Args:
        term: A single AND term from the query.

    Returns:
        True when requiring the term would match almost no articles.
    """
    words = term.strip().lower().split()
    # "customer experience" and "brand perception" are analysis phrases even though
    # neither word alone is conclusive.
    return bool(words) and all(w in _ANALYSIS_WORDS for w in words)


def _is_topic_term(term: str, entity_set: set[str]) -> bool:
    """Whether an OR term is generic vocabulary rather than an entity or its alias.

    Args:
        term: A single OR term from the query.
        entity_set: Lowercased brand and competitor names.

    Returns:
        True when the term would match articles with no brand in them.
    """
    t = term.strip().lower()
    if not t or t in entity_set:
        return False
    # An alias usually contains or sits inside a tracked name ("Chipotle Mexican Grill").
    if any(t in e or e in t for e in entity_set):
        return False
    # A multi-word term ("Brian Niccol") is a name unless it is all topic vocabulary
    # ("customer service").
    words = t.split()
    if len(words) > 1:
        return all(w in _TOPIC_WORDS for w in words)
    return t in _TOPIC_WORDS


def query_warnings(queries: list[str], entities: list[str]) -> list[str]:
    """Ways a query would be mangled by the fetchers, as feedback for the model.

    Checked against the real splitter (scrapper_utils), because a query that parses
    differently there silently returns the wrong articles rather than failing.

    Args:
        queries: Proposed boolean query strings.
        entities: The brand and competitors the query is supposed to track.

    Returns:
        Human-readable problems; empty when the queries are safe.
    """
    from data_source_helpers.scrapper_utils import split_boolean_query

    problems: list[str] = []
    entity_set = {e.lower() for e in entities if e}
    for query in queries:
        if "(" in query or ")" in query:
            problems.append(f"{query!r} uses parentheses, which are stripped before parsing.")
        for op in ("and", "or", "not"):
            if f" {op} " in query:
                problems.append(f"{query!r} has a lowercase {op!r}; it must be uppercase.")
        groups = split_boolean_query(query)
        # Every OR term is searched on its own, so a generic topic word there matches
        # articles with no brand in them. Entity aliases are fine and are how the
        # query gets its breadth.
        stray = [t for t in groups["or"] if _is_topic_term(t, entity_set)]
        if stray:
            problems.append(
                f"{query!r} searches {stray} as standalone terms, which match articles "
                "with no brand in them. Attach a topic to an entity with AND in its "
                "own query instead."
            )
        if len(groups["and"]) > 1:
            problems.append(
                f"{query!r} requires all of {groups['and']} to appear, which is too "
                "narrow. Split them into one query per AND term."
            )
        # An AND term must be a word the coverage itself would contain; analysis
        # vocabulary matches almost nothing.
        analysis = [t for t in groups["and"] if _is_analysis_term(t)]
        if analysis:
            problems.append(
                f"{query!r} requires {analysis}, which describes the analysis rather "
                "than the coverage — almost no article contains those words. Use a "
                "real event or issue, or drop the query."
            )
    return problems


def _render_transcript(history: list[dict]) -> str:
    if not history:
        return "(no messages yet)"
    return "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('content', '')}"
        for m in history
    )


def _known_state(state: WorkflowAgentState, providers: dict[str, str]) -> str:
    """The gathered state as JSON for the prompt, with providers shown by name."""
    return json.dumps(
        {
            "brand": state.brand,
            "competitors": state.competitors,
            "source_type": state.source_type,
            "file_name": state.file_name,
            "file_uploaded": bool(state.file_upload_id),
            "providers": [provider_name(p, providers) for p in state.providers],
            "title": state.title,
            "message_themes": state.message_themes,
            "lenses": state.lenses,
            "queries": state.queries,
            "query_confirmed": state.confirmed_queries,
        },
        ensure_ascii=False,
    )


def _normalize(raw: Any) -> dict[str, Any]:
    """Coerce the model's output into the turn envelope, with safe fallbacks."""
    if not isinstance(raw, dict):
        return {"action": "message", "message": str(raw or "").strip(), "options": [],
                "data": {}, "intent_summary": "", "queries": [], "rationale": ""}
    action = str(raw.get("action") or "").strip().lower()
    return {
        "action": action if action in ACTIONS else "message",
        "message": str(raw.get("message") or "").strip(),
        "options": _clean_strings(raw.get("options")),
        "data": raw.get("data") if isinstance(raw.get("data"), dict) else {},
        "intent_summary": str(raw.get("intent_summary") or "").strip(),
        "queries": _clean_strings(raw.get("queries")),
        "rationale": str(raw.get("rationale") or "").strip(),
    }


def run_turn(state: WorkflowAgentState, providers: dict[str, str]) -> dict[str, Any]:
    """Run one model turn against the current transcript.

    Args:
        state: The conversation state (its history already holds the latest user message).
        providers: The org's active providers.

    Returns:
        The normalized turn envelope.
    """
    system = (
        f"{_TURN_PROMPT}\n\n{format_providers_block(providers)}\n\n"
        f"CURRENT KNOWN STATE (JSON):\n{_known_state(state, providers)}"
    )
    user = (
        f"CONVERSATION SO FAR:\n{_render_transcript(state.history)}\n\n"
        "Respond to the latest user message. Reply with the JSON object only."
    )
    try:
        raw = complete_json(system, user, max_tokens=4096)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Workflow agent turn failed: {exc}")
        return {
            "action": "message",
            "message": "Sorry — I hit a problem there. Could you rephrase that?",
            "options": [], "data": {}, "intent_summary": "", "queries": [], "rationale": "",
        }
    return _normalize(raw)


def research_competitors(brand: str, context: str) -> list[str]:
    """Suggest a brand's real competitors, grounded in web search where available.

    Args:
        brand: The brand to benchmark.
        context: What the user is researching, for disambiguation.

    Returns:
        Up to 6 competitor names, excluding the brand. Empty on any failure.
    """
    if not brand.strip():
        return []
    try:
        raw = complete_json_web(
            _COMPETITOR_PROMPT,
            f"Which companies compete directly with {brand}?\n"
            f"Industry context (for disambiguation only): {context or '(none given)'}",
            max_tokens=1024,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Competitor research failed for {brand!r}: {exc}")
        return []
    names = raw.get("competitors") if isinstance(raw, dict) else None
    brand_lc = brand.strip().lower()
    return [n for n in _clean_strings(names) if n.lower() != brand_lc][:6]
