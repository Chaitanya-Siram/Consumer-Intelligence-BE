"""Adds 2-line LLM insights to each chart in a chart_data dict, plus per-date
date_insights for time-series charts.

Runs ONCE per branch after the charts are computed. Reads the raw chart_data
dict produced by `get_charts_data_by_lens_label(...)` and asks the LLM, via a
forced tool call, to attach a short narrative to every chart.

Returns a dict keyed by chart_key. Callers store it alongside chart_data and
tagged_articles in the S3 envelope.
"""

from collections import defaultdict
import json
from pathlib import Path
from typing import Any
from configs import envs, logger
from db_helpers.schema import DASHBOARDS_ENUM


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_TEMPLATE = (_PROMPTS_DIR / "chart_insight_synthesizer.txt").read_text(encoding="utf-8")

_CLAUDE_ALIASES = {"claude", "anthropic"}
_AZURE_ALIASES = {"azure_openai", "azure-openai", "azure", "openai", "gpt", "gpt-azure"}

# Cap the article digest so the user message stays well under context limits.
# Insights are primarily driven by chart_data — the articles are supplementary
# colour for the LLM to ground specific phrases / quotes / pattern types.
_MAX_ARTICLES_IN_DIGEST = 100
_SNIPPET_CHARS = 200

_INSIGHT_TOOL_NAME = "record_chart_insights"
_INSIGHT_TOOL_DESCRIPTION = (
    "Record an overall assessment for the dashboards, a 2-line insight for "
    "every chart, plus per-date date_insights for time-series charts."
)

def build_insights_tool_parameters(dashboards: list[str]):
    dashboards = [d.lower() for d in dashboards]
    overall_summaries = defaultdict(dict)
    required_fields = []

    if DASHBOARDS_ENUM.media_monitoring in dashboards:
        overall_summaries["media_monitoring_overall_summary"] = {
            "type": "string",
            "description": "A 3-5 sentence executive summary of the WHOLE Media Monitoring dashboard — the headline coverage story across the brand, competitor, and industry feeds, what is currently driving volume and sentiment, any priority-watch or breaking items, and the single most important takeaway for the brand. Synthesise across charts; do not just restate one chart. Wrap standout numbers/phrases in **double asterisks**.",
        }
        required_fields.append("media_monitoring_overall_summary")

    if DASHBOARDS_ENUM.media_measurement in dashboards:
        overall_summaries["media_measurement_overall_summary"] = {
            "type": "string",
            "description": "A 3-5 sentence executive summary of the WHOLE Media Measurement dashboard — the headline story across coverage volume, total reach, sentiment distribution, and share of voice, the prevailing sentiment and trajectory, and the single most important takeaway for the brand. Synthesise across charts; do not just restate one chart. Wrap standout numbers/phrases in **double asterisks**.",
        }
        required_fields.append("media_measurement_overall_summary")

    if DASHBOARDS_ENUM.narrative_intelligence in dashboards:
        overall_summaries["narrative_intelligence_overall_summary"] = {
            "type": "string",
            "description": "A 3-5 sentence executive summary of the WHOLE Narrative Intelligence dashboard — the dominant themes and narratives shaping coverage, how consistently the brand's key messages are landing versus competitors, where the framing is favourable or at risk, and the single most important takeaway for the brand. Synthesise across charts; do not just restate one chart. Wrap standout numbers/phrases in **double asterisks**.",
        }
        required_fields.append("narrative_intelligence_overall_summary")

    if DASHBOARDS_ENUM.pr_impact in dashboards:
        overall_summaries["pr_impact_overall_summary"] = {
            "type": "string",
            "description": "A 3-5 sentence executive summary of the WHOLE PR Impact dashboard — the overall PR impact score and rating, how the brand's share of voice and sentiment compare against competitors, what is driving the impact trajectory, and the single most important takeaway for the brand. Synthesise across charts; do not just restate one chart. Wrap standout numbers/phrases in **double asterisks**.",
        }
        required_fields.append("pr_impact_overall_summary")

    if DASHBOARDS_ENUM.reputation_index in dashboards:
        overall_summaries["reputation_index_overall_summary"] = {
            "type": "string",
            "description": "A 3-5 sentence executive summary of the WHOLE Reputation Index dashboard — the headline reputation index score and its movement, which pillars (Trust, Value, Advocacy, Social, Brand, Risk) are strengthening or dragging it down, the prevailing trajectory, and the single most important takeaway for the brand. Synthesise across charts; do not just restate one chart. Wrap standout numbers/phrases in **double asterisks**.",
        }
        required_fields.append("reputation_index_overall_summary")

    required_fields.append("chart_insights")
    
    tool_paramters =  {
        "type": "object",
        "properties": {
            **overall_summaries,
            "chart_insights": {
                "type": "array",
                "description": "One entry per chart key in the input chart_data.",
                "items": {
                    "type": "object",
                    "properties": {
                        "chart_key": {
                            "type": "string",
                            "description": "The EXACT key from the input chart_data dict.",
                        },
                        "insight": {
                            "type": "string",
                            "description": "Two-line executive insight citing concrete numbers. Wrap standout phrases/numbers in **double asterisks**.",
                        },
                        "analysis": {
                            "type": "string",
                            "description": "Deeper 3-4 bullet markdown analysis of this chart for the modal/expanded view. Each bullet MUST start with a bold lead phrase wrapped in **double asterisks** that states the finding, then a hyphen/em-dash and 2-3 sentences explaining the why (causal driver, implication, what to do). Cite concrete numbers and named entities from the chart data and the article digest. Use '- ' markdown bullets, one blank line between bullets.",
                        },
                        "date_insights": {
                            "type": "array",
                            "description": "ONLY for date-keyed charts. One entry per notable date (peak, dip, inflexion). Omit for non-time-series charts.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "date": {"type": "string", "description": "YYYY-MM-DD."},
                                    "title": {"type": "string", "description": "Short tag e.g. 'Pattern', 'Spike'."},
                                    "summary": {"type": "string", "description": "3-5 sentence explanation. Use **double asterisks** for emphasis."},
                                    "pattern_type": {"type": "string", "description": "Short label e.g. 'Earnings spike', 'Regulatory ripple'."},
                                    "peak_day": {"type": "string", "description": "Dataset peak e.g. 'May 1 · 14 articles'."},
                                    "avg": {"type": "string", "description": "Window average e.g. '5.3 / day'."},
                                },
                                "required": ["date", "title", "summary", "pattern_type", "peak_day", "avg"],
                            },
                        },
                    },
                    "required": ["chart_key", "insight", "analysis"],
                },
            }
        },
        "required": required_fields,
    }

    return tool_paramters



def _render_system_prompt(brand_keywords: list[str] | None) -> str:
    brands = [b.strip() for b in (brand_keywords or []) if b and b.strip()]
    brand_str = ", ".join(brands) if brands else "(not specified)"
    
    return (
        _PROMPT_TEMPLATE
        .replace("{{BRAND}}", brand_str)
    )


def _build_article_digest(tagged_articles: list[dict[str, Any]] | None) -> str:
    """Compact per-article view so the LLM can ground date_insights in actual
    headlines / quotes instead of inferring purely from the numbers. Capped at
    _MAX_ARTICLES_IN_DIGEST to keep the user message manageable."""
    if not tagged_articles:
        return "ARTICLE DIGEST: (no tagged articles available)"

    sample = tagged_articles[:_MAX_ARTICLES_IN_DIGEST]
    overflow = len(tagged_articles) - len(sample)

    lines: list[str] = [
        f"TOTAL TAGGED ARTICLES: {len(tagged_articles)}",
        "",
        "ARTICLE DIGEST (one per line — id | date | sentiment | theme | title | snippet):",
    ]
    for a in sample:
        aid = a.get("id") or ""
        date = (a.get("date") or "")[:10]
        sentiment = a.get("sentiment") or "-"
        theme = a.get("theme") or "-"
        title = (a.get("title") or "").strip().replace("\n", " ")
        body = (a.get("article_text") or a.get("content") or "").strip().replace("\n", " ")
        snippet = body[:_SNIPPET_CHARS]
        lines.append(f"[{aid}] {date} | {sentiment} | {theme} | {title} | {snippet}")

    if overflow > 0:
        lines.append("")
        lines.append(f"(+ {overflow} additional articles omitted from digest)")
    return "\n".join(lines)


def _build_user_message(
    chart_data: Any,
    tagged_articles: list[dict[str, Any]] | None,
) -> str:
    """Send the raw chart_data dict plus a compact article digest. The chart
    numbers drive every insight; the digest gives the LLM material for direct
    quotes, pattern_type labelling, and date-specific colour."""
    return (
        "Below is the dashboard's chart_data dict followed by a digest of the "
        "tagged articles that produced it. Produce one entry per top-level "
        "key in chart_data. Ground date_insights in specific dates / headlines "
        "from the article digest.\n\n"
        "CHART DATA:\n"
        f"```json\n{json.dumps(chart_data, ensure_ascii=False, default=str)}\n```\n\n"
        f"{_build_article_digest(tagged_articles)}"
    )


def _expected_summary_keys(dashboards: list[str]) -> list[str]:
    """Per-dashboard overall-summary keys, kept in lockstep with
    build_insights_tool_parameters so _normalize extracts exactly what the tool
    schema declared as required."""
    dl = [d.lower() for d in dashboards]
    return [f"{member.value}_overall_summary" for member in DASHBOARDS_ENUM if member in dl]


def _expected_chart_keys(chart_data: dict[str, Any]) -> list[str]:
    """The chart keys the LLM should produce one insight per. chart_data nests
    the charts under a per-dashboard wrapper key, so the real chart keys live one
    level down; fall back to the top-level key when a value isn't a dict."""
    keys: list[str] = []
    for top_key, value in chart_data.items():
        if isinstance(value, dict):
            keys.extend(value.keys())
        else:
            keys.append(top_key)
    return keys


def _normalize(tool_input: Any, expected_keys: list[str], summary_keys: list[str]) -> dict[str, Any]:
    """Coerce the LLM tool output into
    {<dashboard>_overall_summary: str, ..., chart_insights: {chart_key: {insight, analysis, date_insights}}}.
    Each present dashboard's summary is a top-level key (absent dashboards are
    omitted). Drops malformed entries; backfills any summary or chart the LLM
    forgot with empty placeholders so the frontend never KeyErrors."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    overall_summaries = {key: str(tool_input.get(key) or "").strip() for key in summary_keys}
    raw_entries = tool_input.get("chart_insights")

    charts: dict[str, dict[str, Any]] = {}
    if isinstance(raw_entries, list):
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            key = entry.get("chart_key")
            if not isinstance(key, str) or not key:
                continue
            insight = str(entry.get("insight") or "").strip()
            analysis = str(entry.get("analysis") or "").strip()
            date_raw = entry.get("date_insights")
            date_insights: list[dict[str, Any]] = []
            if isinstance(date_raw, list):
                for ci in date_raw:
                    if not isinstance(ci, dict):
                        continue
                    date_insights.append({
                        "date": str(ci.get("date") or "").strip(),
                        "title": str(ci.get("title") or "Pattern").strip(),
                        "summary": str(ci.get("summary") or "").strip(),
                        "pattern_type": str(ci.get("pattern_type") or "").strip(),
                        "peak_day": str(ci.get("peak_day") or "").strip(),
                        "avg": str(ci.get("avg") or "").strip(),
                    })
            charts[key] = {"insight": insight, "analysis": analysis, "date_insights": date_insights}

    for key in expected_keys:
        charts.setdefault(key, {"insight": "", "analysis": "", "date_insights": []})

    # Spread each present dashboard's summary as a top-level key (e.g.
    # "media_measurement_overall_summary"); summary_keys already excludes absent ones.
    return {**overall_summaries, "chart_insights": charts}


def _call_claude(system_prompt: str, user_msg: str,dashboards: list[str]) -> Any:
    from anthropic import Anthropic

    if not envs.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=envs.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=envs.CLAUDE_MODEL,
        max_tokens=envs.MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[{
            "name": _INSIGHT_TOOL_NAME,
            "description": _INSIGHT_TOOL_DESCRIPTION,
            "input_schema": build_insights_tool_parameters(dashboards),
        }],
        tool_choice={"type": "tool", "name": _INSIGHT_TOOL_NAME},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _INSIGHT_TOOL_NAME:
            return dict(block.input)
    raise ValueError(f"Claude did not return a {_INSIGHT_TOOL_NAME} tool call")


def _call_azure(system_prompt: str, user_msg: str, dashboards: list[str]) -> Any:
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
        max_tokens=envs.MAX_OUTPUT_TOKENS,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": _INSIGHT_TOOL_NAME,
                "description": _INSIGHT_TOOL_DESCRIPTION,
                "parameters": build_insights_tool_parameters(dashboards),
            },
        }],
        tool_choice={"type": "function", "function": {"name": _INSIGHT_TOOL_NAME}},
    )
    if not completion.choices:
        raise ValueError("Azure OpenAI returned no choices for chart insight synthesis")
    tool_calls = getattr(completion.choices[0].message, "tool_calls", None) or []
    for call in tool_calls:
        if call.function.name == _INSIGHT_TOOL_NAME:
            return json.loads(call.function.arguments or "{}")
    raise ValueError(f"Azure OpenAI did not call {_INSIGHT_TOOL_NAME}")


def synthesize_chart_insights(
    dashboards: list[str],
    chart_data: Any | None,
    brand_keywords: list[str] | None = None,
    tagged_articles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Produce
    {<dashboard>_overall_summary: str, ..., chart_insights: {chart_key: {insight, analysis, date_insights}}}.

    One 3-5 sentence executive summary per requested dashboard lens is returned
    as a top-level key (e.g. `media_measurement_overall_summary`); only dashboards
    present in `dashboards` appear. `chart_insights` carries a 2-line insight +
    analysis per chart, plus per-date date_insights for time-series charts.

    The chart numbers anchor every insight; `tagged_articles` (if provided) is
    sent as a compact digest so the LLM can pull specific headlines / quotes /
    pattern labels. Returns empty placeholders on failure so callers can still
    persist the chart data and tagged articles even if the insight pass hiccups.
    """
    summary_keys = _expected_summary_keys(dashboards)
    if not chart_data or not isinstance(chart_data, dict):
        return {**{key: "" for key in summary_keys}, "chart_insights": {}}

    expected_keys = _expected_chart_keys(chart_data)
    system_prompt = _render_system_prompt(brand_keywords)
    user_msg = _build_user_message(chart_data, tagged_articles)

    provider = envs.LLM_PROVIDER
    try:
        if provider in _AZURE_ALIASES:
            raw = _call_azure(system_prompt, user_msg, dashboards)
        elif provider in _CLAUDE_ALIASES:
            raw = _call_claude(system_prompt, user_msg, dashboards)
        else:
            raise RuntimeError(f"Unknown LLM_PROVIDER='{provider}'")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Chart insight synthesis failed: {exc}")
        return {
            **{key: "" for key in summary_keys},
            "chart_insights": {key: {"insight": "", "analysis": "", "date_insights": []} for key in expected_keys},
        }

    return _normalize(raw, expected_keys, summary_keys)
