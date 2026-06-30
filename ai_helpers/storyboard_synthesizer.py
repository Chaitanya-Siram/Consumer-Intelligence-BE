"""Builds a chapter-by-chapter "storyboard" for a dashboard.

Each dashboard splits its charts into TABS; this module turns each tab into a
narrative chapter (section label, headline, description, "what to watch for")
so the dashboard reads as a guided story.

Runs ONCE per branch after the charts are computed, in the same step as the
chart-insight synthesis. Returns a list of chapters keyed in tab order.
"""

import json
from pathlib import Path
from typing import Any
from configs import envs, logger
from db_helpers.schema import DASHBOARDS_ENUM


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_TEMPLATE = (_PROMPTS_DIR / "storyboard_synthesizer.txt").read_text(encoding="utf-8")

_CLAUDE_ALIASES = {"claude", "anthropic"}
_AZURE_ALIASES = {"azure_openai", "azure-openai", "azure", "openai", "gpt", "gpt-azure"}

_STORYBOARD_TOOL_NAME = "record_storyboard"
_STORYBOARD_TOOL_DESCRIPTION = (
    "Record one narrative chapter per dashboard tab, in tab order."
)

_STORYBOARD_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "storyboard": {
            "type": "array",
            "description": "One chapter per input tab, in the same order.",
            "items": {
                "type": "object",
                "properties": {
                    "tab_name": {"type": "string", "description": "EXACT tab name from the input."},
                    "section_label": {"type": "string", "description": "Uppercase 'CATEGORY · SUBTITLE' kicker."},
                    "title": {"type": "string", "description": "Punchy 2-5 word headline ending with a period."},
                    "description": {"type": "string", "description": "2-3 sentence framing of this chapter."},
                    "what_to_watch_for": {
                        "type": "array",
                        "description": "Exactly 3 short bullet phrases (3-9 words each), no trailing punctuation.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["tab_name", "section_label", "title", "description", "what_to_watch_for"],
            },
        }
    },
    "required": ["storyboard"],
}


# ---------------------------------------------------------------------------
# Dashboard → tab → chart-key configuration
# ---------------------------------------------------------------------------
# A tab's chart list of None means "all charts in chart_data" (single Overview
# tab). Dashboards not listed here (e.g. media monitoring) get no storyboard.

_ALL: Any = None

_DASHBOARD_TABS: dict[str, list[tuple[str, Any]]] = {
    DASHBOARDS_ENUM.media_measurement: [
        ("Overview", ["total_count", "total_reach", "sentiment_distribution", "original_vs_syndicated", "theme_distribution"]),
        ("Sentiment Analysis", ["sentiment_distribution"]),
        ("Themes & Topics", ["theme_distribution"]),
        ("Media Coverage", ["top_publications", "publication_reach_sentiment", "publish_time_heatmap", "top_authors_by_publications"]),
        ("Key Stories", ["top_articles_by_sentiment"]),
    ],
    DASHBOARDS_ENUM.narrative_intelligence: [
        ("Overview", ["total_count", "total_reach", "top_narratives"]),
        ("Coverage", ["coverage_overtime_by_competitors", "media_types_by_competitors"]),
        ("Sentiment", ["sentiment_breakdown_by_competitors"]),
        ("Message Consistency", ["message_consistency", "coverage_message_consistency"]),
        ("Channels & Publications", ["publication_by_brands_and_competitors"])
    ],
    DASHBOARDS_ENUM.pr_impact: [
        ("Overview", ["total_count", "total_reach", "net_sentiment_score"]),
        ("Coverage & Sentiment", ["datewise_coverage", "sentiment_distribution"]),
        ("Share of Voice", ["share_of_voice", "publication_tier"]),
        ("PR Impact", ["pr_impact", "pr_impact_competitors"]),
        ("Competitive", ["competitive_matrix"])
    ],
    DASHBOARDS_ENUM.reputation_index: [
        ("Overview", ["ri_gauge", "ri_timeseries", "ri_decomposition_coverage"]),
        ("Pillar Analysis", ["pillar_radar", "pillar_bar", "pillar_weights", "pillar_small_multiples"]),
        ("Trust & Sentiment", ["trust_kpi_breakdown", "trust_waterfall", "sentiment_coverage", "net_sentiment_coverage"]),
        ("Media Coverage", ["coverage_volume", "tier1_share", "source_treemap", "theme_volume", "theme_pillar_heatmap"]),
        ("Risk & Sensitivity", ["risk_negative_coverage", "weight_sensitivity", "ri_decomposition_coverage"])
    ],
    # media monitoring: intentionally absent → no storyboard.
}


def _tabs_for_dashboard(dashboard: str | None, chart_data: Any) -> list[tuple[str, dict[str, Any]]]:
    """Resolve the (tab_name, chart_subset) list for this dashboard. Returns [] for
    dashboards that don't get a storyboard (e.g. media monitoring). `dashboard` is
    a DASHBOARDS_ENUM value (str-enum), matched case-insensitively.

    `chart_data` is the dashboard's list of ChartResult objects; we index it by
    `chart_id` and pick each tab's charts from that index."""
    key = (dashboard or "").strip().lower()
    tabs_spec = _DASHBOARD_TABS.get(key)
    if not tabs_spec:
        return []

    # Index the ChartResult list by chart_id, keeping a JSON-serialisable view.
    def _as_dict(chart: Any) -> dict[str, Any]:
        return chart.model_dump() if hasattr(chart, "model_dump") else chart

    by_id: dict[str, Any] = {}
    for chart in (chart_data or []):
        cid = getattr(chart, "chart_id", None)
        if cid:
            by_id[cid] = _as_dict(chart).get("data")

    tabs: list[tuple[str, dict[str, Any]]] = []
    for tab_name, keys in tabs_spec:
        if keys is _ALL:
            subset = dict(by_id)
        else:
            subset = {k: by_id[k] for k in keys if k in by_id}
        if subset:
            tabs.append((tab_name, subset))
    return tabs


def _render_system_prompt(dashboard: str | None, brand_keywords: list[str] | None) -> str:
    brands = [b.strip() for b in (brand_keywords or []) if b and b.strip()]
    brand_str = ", ".join(brands) if brands else "(not specified)"
    # "media_measurement" -> "Media Measurement"
    dashboard_str = (dashboard or "(unspecified)").strip().replace("_", " ").title()
    return (
        _PROMPT_TEMPLATE
        .replace("{{BRAND}}", brand_str)
        .replace("{{DASHBOARD}}", dashboard_str)
    )


def _build_user_message(tabs: list[tuple[str, dict[str, Any]]]) -> str:
    lines = [
        f"There are {len(tabs)} tabs. Produce one chapter per tab, in this order.\n"
    ]
    for tab_name, subset in tabs:
        lines.append(f"=== TAB: {tab_name} ===")
        lines.append(f"```json\n{json.dumps(subset, ensure_ascii=False, default=str)}\n```\n")
    return "\n".join(lines)


def _normalize(raw: Any, tabs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Align the LLM's chapters back onto the canonical tab order and assign
    chapter numbers. Tabs the LLM skipped get an empty placeholder so the
    frontend can still render the chapter shell."""
    tool_input = raw if isinstance(raw, dict) else {}
    entries = tool_input.get("storyboard")
    by_tab: dict[str, dict[str, Any]] = {}
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("tab_name"), str):
                by_tab[e["tab_name"].strip().lower()] = e

    total = len(tabs)
    out: list[dict[str, Any]] = []
    for idx, (tab_name, _subset) in enumerate(tabs, start=1):
        e = by_tab.get(tab_name.strip().lower(), {})
        watch = e.get("what_to_watch_for")
        if not isinstance(watch, list):
            watch = []
        out.append({
            "chapter": idx,
            "total_chapters": total,
            "tab_name": tab_name,
            "section_label": str(e.get("section_label") or "").strip(),
            "title": str(e.get("title") or "").strip(),
            "description": str(e.get("description") or "").strip(),
            "what_to_watch_for": [str(w).strip() for w in watch if str(w).strip()],
        })
    return out


def _call_claude(system_prompt: str, user_msg: str) -> Any:
    from anthropic import Anthropic

    if not envs.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=envs.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=envs.CLAUDE_MODEL,
        max_tokens=envs.MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[{
            "name": _STORYBOARD_TOOL_NAME,
            "description": _STORYBOARD_TOOL_DESCRIPTION,
            "input_schema": _STORYBOARD_TOOL_PARAMETERS,
        }],
        tool_choice={"type": "tool", "name": _STORYBOARD_TOOL_NAME},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _STORYBOARD_TOOL_NAME:
            return dict(block.input)
    raise ValueError(f"Claude did not return a {_STORYBOARD_TOOL_NAME} tool call")


def _call_azure(system_prompt: str, user_msg: str) -> Any:
    from openai import AzureOpenAI

    if not (envs.AZURE_OPENAI_API_KEY and envs.AZURE_OPENAI_ENDPOINT and envs.AZURE_OPENAI_MODEL):
        raise RuntimeError("Azure OpenAI is not configured")
    client = AzureOpenAI(
        api_key=envs.AZURE_OPENAI_API_KEY,
        azure_endpoint=envs.AZURE_OPENAI_ENDPOINT,
        api_version=envs.AZURE_OPENAI_API_VERSION,
        timeout=600.0,
    )
    completion = client.chat.completions.create(
        model=envs.AZURE_OPENAI_MODEL,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": _STORYBOARD_TOOL_NAME,
                "description": _STORYBOARD_TOOL_DESCRIPTION,
                "parameters": _STORYBOARD_TOOL_PARAMETERS,
            },
        }],
        tool_choice={"type": "function", "function": {"name": _STORYBOARD_TOOL_NAME}},
    )
    if not completion.choices:
        raise ValueError("Azure OpenAI returned no choices for storyboard synthesis")
    tool_calls = getattr(completion.choices[0].message, "tool_calls", None) or []
    for call in tool_calls:
        if call.function.name == _STORYBOARD_TOOL_NAME:
            return json.loads(call.function.arguments or "{}")
    raise ValueError(f"Azure OpenAI did not call {_STORYBOARD_TOOL_NAME}")


def synthesize_storyboard(
    chart_data: dict[str, Any] | None,
    dashboard: str | None = None,
    brand_keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Produce a list of chapters — one per tab for this dashboard.

    `dashboard` is a DASHBOARDS_ENUM value. Returns [] for dashboards without a
    storyboard (media monitoring / unknown) and on failure, so callers can always
    persist the rest of the envelope.
    """
    if not chart_data:
        return []

    tabs = _tabs_for_dashboard(dashboard, chart_data)
    if not tabs:
        return []

    system_prompt = _render_system_prompt(dashboard, brand_keywords)
    user_msg = _build_user_message(tabs)

    provider = envs.LLM_PROVIDER
    try:
        if provider in _AZURE_ALIASES:
            raw = _call_azure(system_prompt, user_msg)
        elif provider in _CLAUDE_ALIASES:
            raw = _call_claude(system_prompt, user_msg)
        else:
            raise RuntimeError(f"Unknown LLM_PROVIDER='{provider}'")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Storyboard synthesis failed: {exc}")
        return _normalize({}, tabs)

    return _normalize(raw, tabs)
