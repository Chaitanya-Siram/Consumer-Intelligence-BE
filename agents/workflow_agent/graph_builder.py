"""Build the workflow graph (nodes + edges) from the agent's gathered state.

Pure and deterministic — no LLM. The model decides *what* to monitor; the ids, edges,
positions and field names are mechanical, so they are built here rather than asked for.
"""
from typing import Any

from agents.workflow_agent.state import WorkflowAgentState

# Mirrors frontend/src/workflow/constants.js, which is the source of truth for these.
LENSES = ("media_measurement", "media_monitoring", "narrative_intelligence",
          "pr_impact", "reputation_index")
LLM_MODELS = ("openai", "claude", "gemini")
CHARTS = ("volume", "sentiment", "share_of_voice", "themes", "sources", "geography")
LAYOUTS = ("Classic", "Editorial", "Merger", "PR Impact", "Glass", "Bento")
OUTPUT_FORMATS = ("Dashboard", "Intelligence Brief", "PDF Report", "API Webhook")

DEFAULT_CHARTS = ["volume", "sentiment", "share_of_voice", "themes"]

NODE_ORDER = ("data", "analysis", "review", "assembly", "output")

# Same column/row math as WorkflowScreen.autoLayout, so the graph renders correctly
# without the user pressing Auto-format.
COL_W, ROW_H, X0, Y0 = 320, 210, 60, 40


def _pick(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    """The value if it is allowed, else the fallback."""
    return value if isinstance(value, str) and value in allowed else fallback


def _pick_many(values: Any, allowed: tuple[str, ...], fallback: list[str]) -> list[str]:
    """The allowed values in order, de-duplicated; the fallback when none survive."""
    if not isinstance(values, list):
        return list(fallback)
    kept: list[str] = []
    for v in values:
        if isinstance(v, str) and v in allowed and v not in kept:
            kept.append(v)
    return kept or list(fallback)


def _clean_list(values: Any) -> list[str]:
    """Non-blank strings, trimmed and de-duplicated in first-seen order."""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        s = str(v or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _layout(nodes_by_type: dict[str, list[dict]]) -> None:
    """Position each stage in its own column, vertically centred against the tallest."""
    tallest = max((len(nodes_by_type[t]) for t in NODE_ORDER), default=1) or 1
    for col, node_type in enumerate(NODE_ORDER):
        group = nodes_by_type[node_type]
        start_y = Y0 + ((tallest - len(group)) * ROW_H) / 2
        for row, node in enumerate(group):
            node["position"] = {"x": X0 + col * COL_W, "y": start_y + row * ROW_H}


def build_graph(state: WorkflowAgentState, providers: dict[str, str]) -> dict[str, Any]:
    """Assemble the workflow graph from the gathered state.

    Args:
        state: The agent's gathered state.
        providers: The org's active providers (name -> label), used to filter data sources.

    Returns:
        A workflow graph dict with "nodes" and "edges".
    """
    brand = state.brand.strip()
    competitors = _clean_list(state.competitors)
    themes = _clean_list(state.message_themes)
    queries = _clean_list(state.queries)

    allowed_labels = tuple(providers.values())
    data_sources = [p for p in _clean_list(state.providers) if p in allowed_labels]
    if not data_sources:
        data_sources = ["google_news"]

    lenses = _pick_many(state.lenses, LENSES, ["media_monitoring"])
    llm = _pick(state.llm, LLM_MODELS, "openai")
    charts = _pick_many(state.charts, CHARTS, DEFAULT_CHARTS)
    layout = _pick(state.layout, LAYOUTS, "Classic")
    output_format = _pick(state.output_format, OUTPUT_FORMATS, "Dashboard")
    title = state.title.strip() or (f"{brand} Media Intelligence" if brand else "Media Intelligence")

    nodes_by_type: dict[str, list[dict]] = {t: [] for t in NODE_ORDER}
    seq = 0

    def _add(node_type: str, data: dict[str, Any]) -> dict[str, Any]:
        nonlocal seq
        seq += 1
        node = {"id": f"{node_type}_{seq}", "type": node_type, "position": {"x": 0, "y": 0},
                "data": data}
        nodes_by_type[node_type].append(node)
        return node

    _add("data", {
        "label": "Data",
        "sourceType": "api",
        "brandKeywords": [brand] if brand else [],
        "messageKeywords": themes,
        "competitorKeywords": competitors,
        "data_sources": data_sources,
        "queries": queries,
    })
    for lens in lenses:
        # collect_keywords also reads competitorKeywords off analysis nodes.
        _add("analysis", {"label": "Analysis", "lens": lens, "llm": llm, "skill": "",
                          "competitorKeywords": competitors})
    _add("review", {"label": "Review", "flag": 50, "auto": 75, "requiresSignOff": False})
    _add("assembly", {"label": "Dashboard Builder", "clientName": brand,
                      "charts": charts, "layout": layout})
    _add("output", {
        "label": "Output",
        "format": output_format,
        "projectName": title,
        "projectDescription": state.intent_summary.strip() or title,
    })

    _layout(nodes_by_type)

    nodes = [n for t in NODE_ORDER for n in nodes_by_type[t]]
    edges = [
        {"id": f"e_{src['id']}_{tgt['id']}", "source": src["id"], "target": tgt["id"],
         "invalid": False}
        for a, b in zip(NODE_ORDER, NODE_ORDER[1:])
        for src in nodes_by_type[a]
        for tgt in nodes_by_type[b]
    ]
    return {"nodes": nodes, "edges": edges}


def summarize(graph: dict[str, Any], providers: dict[str, str] | None = None) -> str:
    """One-line description of what was built, for the chat transcript.

    Args:
        graph: The built workflow graph.
        providers: Provider name to label, so sources read as names not internal labels.

    Returns:
        A one-line summary.
    """
    counts: dict[str, int] = {}
    for node in graph["nodes"]:
        counts[node["type"]] = counts.get(node["type"], 0) + 1
    lenses = [n["data"]["lens"] for n in graph["nodes"] if n["type"] == "analysis"]
    data_node = next(n for n in graph["nodes"] if n["type"] == "data")

    label_to_name = {v: k for k, v in (providers or {}).items()}
    sources = [label_to_name.get(s, s) for s in data_node["data"]["data_sources"]]
    return (
        f"Built a workflow: Data ({', '.join(sources)}) -> "
        f"{counts.get('analysis', 0)} Analysis node(s) ({', '.join(lenses)}) -> "
        f"Review -> Assembly -> Output."
    )
