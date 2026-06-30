"""Synthesises the top narrative threads across an already-tagged article corpus.

Runs ONCE per branch after per-article tagging is complete. Reads the tagged
articles (id / title / theme / sentiment / snippet) and asks the LLM, via a
forced tool call, to return the top 3 narratives as structured JSON.

Output is a list of {tags, title, summary, coverage}. Callers store it
alongside the tagged article list in S3 as
    { "top_narratives": [...], "tagged_articles": [...] }.
"""

import json
from pathlib import Path
from typing import Any
from configs import envs, logger


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_TEMPLATE = (_PROMPTS_DIR / "narrative_synthesizer.txt").read_text(encoding="utf-8")

# Cap the digest so we don't blow the context window on huge corpora. Articles
# beyond this limit are summarised by count only — the LLM still computes
# coverage % against the full total we tell it.
_MAX_ARTICLES_IN_DIGEST = 400
_SNIPPET_CHARS = 240

_CLAUDE_ALIASES = {"claude", "anthropic"}
_AZURE_ALIASES = {"azure_openai", "azure-openai", "azure", "openai", "gpt", "gpt-azure"}

_NARRATIVE_TOOL_NAME = "record_top_narratives"
_NARRATIVE_TOOL_DESCRIPTION = (
    "Record the top narrative threads found across the article corpus, ranked by coverage."
)

_NARRATIVE_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narratives": {
            "type": "array",
            "description": "Top narrative threads ranked by coverage descending. At most 3.",
            "items": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "description": "1-2 short uppercase labels characterising the narrative (e.g. ['DOMINANT', 'AGCM TRIGGER']).",
                        "items": {"type": "string"},
                    },
                    "title": {
                        "type": "string",
                        "description": "Punchy 5-10 word headline. No markdown. No article ids.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Two-line summary citing concrete facts. Wrap the most important phrases/numbers/quotes in **double asterisks**. Do NOT mention article ids.",
                    },
                    "coverage": {
                        "type": "number",
                        "description": "Coverage percent (0-100), rounded to the nearest whole number.",
                    },
                },
                "required": ["tags", "title", "summary", "coverage"],
            },
        }
    },
    "required": ["narratives"],
}


def _render_system_prompt(brand_keywords: list[str] | None) -> str:
    brands = [b.strip() for b in (brand_keywords or []) if b and b.strip()]
    brand_str = ", ".join(brands) if brands else "(not specified)"
    return _PROMPT_TEMPLATE.replace("{{BRAND}}", brand_str)


def _build_digest(tagged_articles: list[dict[str, Any]]) -> str:
    """Compact per-article view: id | date | sentiment | theme | title | snippet.
    Ids are kept in the digest so the LLM can cluster — but the prompt forbids
    citing them in the output."""
    sample = tagged_articles[:_MAX_ARTICLES_IN_DIGEST]
    overflow = len(tagged_articles) - len(sample)

    lines: list[str] = [
        f"TOTAL ARTICLES IN DATASET: {len(tagged_articles)}",
        "",
        "ARTICLE DIGEST (one per line):",
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
        lines.append(
            f"(+ {overflow} additional articles omitted from digest — coverage % "
            f"must still be computed against the TOTAL of {len(tagged_articles)})"
        )
    return "\n".join(lines)


def _normalize(narratives: Any) -> list[dict[str, Any]]:
    if not isinstance(narratives, list):
        return []
    out: list[dict[str, Any]] = []
    for n in narratives:
        if not isinstance(n, dict):
            continue
        tags = n.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        try:
            coverage = round(float(n.get("coverage") or 0))
        except (TypeError, ValueError):
            coverage = 0
        out.append({
            "tags": [str(t) for t in tags if t],
            "title": str(n.get("title") or "").strip(),
            "summary": str(n.get("summary") or "").strip(),
            "coverage": coverage,
        })
    return out


def _call_claude(system_prompt: str, user_msg: str) -> list[dict[str, Any]]:
    from anthropic import Anthropic

    if not envs.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=envs.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=envs.CLAUDE_MODEL,
        max_tokens=2048,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        tools=[{
            "name": _NARRATIVE_TOOL_NAME,
            "description": _NARRATIVE_TOOL_DESCRIPTION,
            "input_schema": _NARRATIVE_TOOL_PARAMETERS,
        }],
        tool_choice={"type": "tool", "name": _NARRATIVE_TOOL_NAME},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _NARRATIVE_TOOL_NAME:
            return _normalize(dict(block.input).get("narratives"))
    raise ValueError(f"Claude did not return a {_NARRATIVE_TOOL_NAME} tool call")


def _call_azure(system_prompt: str, user_msg: str) -> list[dict[str, Any]]:
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
        max_tokens=2048,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": _NARRATIVE_TOOL_NAME,
                "description": _NARRATIVE_TOOL_DESCRIPTION,
                "parameters": _NARRATIVE_TOOL_PARAMETERS,
            },
        }],
        tool_choice={"type": "function", "function": {"name": _NARRATIVE_TOOL_NAME}},
    )
    if not completion.choices:
        raise ValueError("Azure OpenAI returned no choices for narrative synthesis")
    tool_calls = getattr(completion.choices[0].message, "tool_calls", None) or []
    for call in tool_calls:
        if call.function.name == _NARRATIVE_TOOL_NAME:
            payload = json.loads(call.function.arguments or "{}")
            return _normalize(payload.get("narratives"))
    raise ValueError(f"Azure OpenAI did not call {_NARRATIVE_TOOL_NAME}")


def synthesize_top_narratives(
    tagged_articles: list[dict[str, Any]],
    brand_keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Produce a structured list of the top 3 narratives in the corpus.

    Each item is {tags, title, summary, coverage}. Returns an empty list on
    failure so callers can still persist tagged articles.
    """
    if not tagged_articles:
        return []

    system_prompt = _render_system_prompt(brand_keywords)
    user_msg = _build_digest(tagged_articles)

    provider = envs.LLM_PROVIDER
    try:
        if provider in _AZURE_ALIASES:
            return _call_azure(system_prompt, user_msg)
        if provider in _CLAUDE_ALIASES:
            return _call_claude(system_prompt, user_msg)
        raise RuntimeError(f"Unknown LLM_PROVIDER='{provider}'")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Narrative synthesis failed: {exc}")
        return []
