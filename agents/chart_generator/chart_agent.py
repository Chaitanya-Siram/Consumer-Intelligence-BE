"""Interactive data agent over a session's tagged articles.

Two capabilities, routed by an intent sub-agent:
  1. "chart"    — generate pandas code, run it in an E2B sandbox against the
                  tagged articles, return ChartResult(s).
  2. "question" — read the tagged articles + precomputed dashboard charts and
                  answer in words.

All functions here are synchronous (LLM + sandbox calls block). The WebSocket
router drives them via asyncio.to_thread and streams progress between steps.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from configs import logger
from db_helpers.schema import ChartResult
from agents.chart_generator import llm_client
from agents.chart_generator.sandbox import run_chart_code

_PROMPTS = Path(__file__).parent / "prompts"
_INTENT_PROMPT = (_PROMPTS / "intent_router.txt").read_text(encoding="utf-8")
_CHART_PROMPT = (_PROMPTS / "chart_code_agent.txt").read_text(encoding="utf-8")
_CHART_FIX_PROMPT = (_PROMPTS / "chart_code_fix_agent.txt").read_text(encoding="utf-8")
_QA_PROMPT = (_PROMPTS / "qa_agent.txt").read_text(encoding="utf-8")

# Keep the chart-gen sample small (the full dataset is injected into the sandbox,
# not the prompt). The QA digest is capped so the message stays within context.
_MAX_CODEGEN_SAMPLE = 5
_MAX_QA_ARTICLES = 120
_QA_SNIPPET_CHARS = 500


def _slugify_chart_id(value: str, fallback: str = "chart") -> str:
    """Normalize a chart id into a function-like snake_case slug
    (lowercase, digits, single underscores), e.g. "Sentiment by Reach" ->
    "sentiment_by_reach"."""
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return slug or fallback


# ---------------------------------------------------------------------------
# Sub-agent 1: intent routing
# ---------------------------------------------------------------------------

def classify_intent(query: str) -> str:
    """Return "chart" or "question". Defaults to "question" on any failure."""
    try:
        result = llm_client.complete_json(_INTENT_PROMPT, f"User message:\n{query}", max_tokens=50)
        intent = str(result.get("intent", "")).strip().lower() if isinstance(result, dict) else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Intent classification failed; defaulting to 'question': {exc}")
        intent = ""
    return "chart" if intent == "chart" else "question"


# ---------------------------------------------------------------------------
# Sub-agent 2: chart generation (code-gen -> sandbox)
# ---------------------------------------------------------------------------

def generate_chart_specs(query: str, articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask the LLM for 1-4 chart specs (each with standalone python_code)."""
    sample = articles[:_MAX_CODEGEN_SAMPLE]
    user_msg = (
        f"User query: {query}\n\n"
        f"Dataset sample ({len(sample)} of {len(articles)} rows):\n"
        f"{json.dumps(sample, default=str, indent=2)}\n\n"
        f"Total rows in full dataset: {len(articles)}"
    )
    specs = llm_client.complete_json(_CHART_PROMPT, user_msg, max_tokens=8192)
    if isinstance(specs, dict):
        # Tolerate {"charts": [...]} or a single bare spec object.
        specs = specs.get("charts") or [specs]
    if not isinstance(specs, list):
        return []

    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, spec in enumerate(specs, start=1):
        if not isinstance(spec, dict):
            continue
        # Prefer the model's chart_id; fall back to the title, then an index.
        slug = _slugify_chart_id(spec.get("chart_id") or spec.get("title") or "", fallback=f"chart_{i}")
        # Ensure uniqueness within the response.
        unique = slug
        n = 2
        while unique in seen:
            unique = f"{slug}_{n}"
            n += 1
        seen.add(unique)
        spec["chart_id"] = unique
        cleaned.append(spec)
    return cleaned


def chart_result_from_data(spec: dict[str, Any], data: dict[str, Any]) -> ChartResult:
    """Build a successful ChartResult from a spec and the sandbox's chart dict."""
    d = data or {}
    return ChartResult(
        chart_id=str(spec.get("chart_id") or "chart"),
        title=str(d.get("title") or spec.get("title") or "Chart"),
        description=str(spec.get("description") or ""),
        chart_type=str(d.get("chart_type") or "bar"),
        data=d.get("data", []),
        series=d.get("series", []) or [],
        x_label=str(d.get("x_label") or ""),
        y_label=str(d.get("y_label") or ""),
    )


def failed_chart_result(spec: dict[str, Any], error: str) -> ChartResult:
    """Build a ChartResult that carries an error (empty data)."""
    return ChartResult(
        chart_id=str(spec.get("chart_id") or "chart"),
        title=str(spec.get("title") or "Chart"),
        description=str(spec.get("description") or ""),
        chart_type="bar", data=[], series=[], x_label="", y_label="", error=error,
    )


def fix_chart_code(query: str, articles: list[dict[str, Any]], spec: dict[str, Any], error_message: str) -> str:
    """Ask the LLM to regenerate one chart's python_code, fixing a sandbox error.
    Returns the corrected code (empty string if the model produced none)."""
    sample = articles[:_MAX_CODEGEN_SAMPLE]
    user_msg = (
        f"User query: {query}\n\n"
        f"Chart: {spec.get('chart_id')} — {spec.get('title')}\n"
        f"{spec.get('description') or ''}\n\n"
        f"The previous python_code FAILED in the sandbox with this error:\n{error_message}\n\n"
        f"Previous python_code:\n```python\n{spec.get('python_code', '')}\n```\n\n"
        f"Dataset sample ({len(sample)} of {len(articles)} rows):\n"
        f"{json.dumps(sample, default=str, indent=2)}\n\n"
        "Return the CORRECTED python_code that fixes the error."
    )
    try:
        result = llm_client.complete_json(_CHART_FIX_PROMPT, user_msg, max_tokens=8192)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"fix_chart_code failed to produce valid JSON: {exc}")
        return ""
    if isinstance(result, dict):
        return str(result.get("python_code") or "")
    return str(result or "")


def run_chart_spec(spec: dict[str, Any], articles: list[dict[str, Any]]) -> ChartResult:
    """Execute one chart spec in the sandbox once and coerce to a ChartResult.
    (Single attempt — the router drives the fix-and-retry loop so it can stream
    each attempt.)"""
    code = spec.get("python_code") or ""
    if not code.strip():
        return failed_chart_result(spec, "The model did not return any python_code for this chart.")
    result = run_chart_code(code, articles)
    if not result.ok or not result.data:
        return failed_chart_result(spec, result.error or "Sandbox returned no chart data.")
    return chart_result_from_data(spec, result.data)


# ---------------------------------------------------------------------------
# Sub-agent 3: question answering
# ---------------------------------------------------------------------------

def _build_article_digest(articles: list[dict[str, Any]]) -> str:
    """Compact one-line-per-article view so the LLM can ground answers in actual
    headlines, capped at _MAX_QA_ARTICLES."""
    if not articles:
        return "ARTICLE DIGEST: (no tagged articles available)"

    sample = articles[:_MAX_QA_ARTICLES]
    overflow = len(articles) - len(sample)
    lines = [
        f"TOTAL TAGGED ARTICLES: {len(articles)}",
        "",
        "ARTICLE DIGEST (id | date | sentiment | theme | domain | title | snippet):",
    ]
    for a in sample:
        aid = a.get("id") or ""
        date = str(a.get("date") or "")[:10]
        sentiment = a.get("sentiment") or "-"
        theme = a.get("theme") or "-"
        domain = a.get("domain") or a.get("source") or "-"
        title = str(a.get("title") or "").strip().replace("\n", " ")
        body = str(a.get("content") or a.get("article_text") or "").strip().replace("\n", " ")
        lines.append(f"[{aid}] {date} | {sentiment} | {theme} | {domain} | {title} | {body[:_QA_SNIPPET_CHARS]}")
    if overflow > 0:
        lines.append("")
        lines.append(f"(+ {overflow} additional articles omitted from digest)")
    return "\n".join(lines)


def answer_question(query: str, articles: list[dict[str, Any]], charts_data: Any | None) -> str:
    """Answer a free-form question from the tagged articles + precomputed charts."""
    digest = _build_article_digest(articles)
    if charts_data:
        charts_blob = json.dumps(charts_data, default=str)
    else:
        charts_blob = "(no precomputed dashboard charts available for this session)"
    user_msg = (
        f"User question: {query}\n\n"
        f"PRECOMPUTED DASHBOARD CHARTS (JSON):\n{charts_blob}\n\n"
        f"{digest}"
    )
    return llm_client.complete(_QA_PROMPT, user_msg, max_tokens=2000)
