"""Pull the include / exclude domain lists out of a project's relevancy prompt.

The relevancy criteria are free text, and the domains they name are written the
way a person writes them — bulleted, prefixed with ``www.``, carrying a stray
comma for a dot, sometimes as a full URL, sometimes as a publication name beside
the host. This asks the LLM to read the criteria once, when the prompt is saved,
and return the two lists as structured JSON. The result is stored on
``projects.relevancy_domains`` so the fetch path never re-pays for the parse.

Shape returned (and stored):
    {"include": ["reuters.com", ...], "exclude": ["yahoo.com", ...]}

Every host is normalized through the relevancy agent's own ``_normalize_domain``,
so what lands in the column matches what the gate matches against at fetch time.

Fails SOFT: on any LLM or config error the extraction returns empty lists and
logs. A prompt must always be saveable — the domain lists are an optimization
over criteria the gate can still read as text, never a gate on saving.
"""
from __future__ import annotations

import json
from typing import Any

from configs import envs, logger

from .relevancy_agent import (
    _AZURE_ALIASES,
    _CLAUDE_ALIASES,
    _normalize_domain,
)

_TOOL_NAME = "record_domains"
_TOOL_DESCRIPTION = "Record the publication domains the criteria include and exclude."
_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include": {
            "type": "array",
            "description": (
                "Hostnames the criteria explicitly say to INCLUDE / allow / restrict coverage to. "
                "Empty list when the criteria name none."
            ),
            "items": {"type": "string"},
        },
        "exclude": {
            "type": "array",
            "description": (
                "Hostnames the criteria explicitly say to EXCLUDE / drop / ignore. "
                "Empty list when the criteria name none."
            ),
            "items": {"type": "string"},
        },
    },
    "required": ["include", "exclude"],
}

_SYSTEM_PROMPT = """You extract publication domains from a media-monitoring relevancy brief.

The brief is free text written by an analyst. Somewhere in it there may be lists of publications or websites to EXCLUDE (drop, ignore, never report) and/or to INCLUDE (allow, restrict coverage to, only report from). Your only job is to find those and
return them as two lists of hostnames.

Rules:
- Return a bare hostname for each entry: no scheme, no `www.`, no path, no query. `https://www.finance.yahoo.com/news` becomes `finance.yahoo.com`.
- When an entry names a publication and its site together ("Yahoo Finance (finance.yahoo.com)"), return only the hostname.
- When an entry names a publication with NO domain anywhere in the brief ("exclude tabloids"), skip it — you must not guess or invent a domain. Only return hosts the brief actually spells out.
- Fix obvious typos in a host: `indiasnews,net` is `indiasnews.net`. Do not otherwise alter it.
- A domain listed under an exclusion belongs ONLY in `exclude`; one listed under an inclusion belongs ONLY in `include`. If the brief genuinely lists the same host in both, put it in `exclude` — exclusions win.
- Deduplicate. Preserve the order the brief lists them in.
- Most briefs name no domains at all, or only exclusions. Returning two empty lists, or an empty `include`, is a correct and common answer. Never pad a list to look complete.

Respond by calling the record_domains tool exactly once."""


def _call_claude(user_msg: str) -> dict[str, Any]:
    from anthropic import Anthropic

    if not envs.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=envs.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=envs.CLAUDE_MODEL,
        max_tokens=envs.MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": _SYSTEM_PROMPT}],
        tools=[{
            "name": _TOOL_NAME,
            "description": _TOOL_DESCRIPTION,
            "input_schema": _TOOL_PARAMETERS,
        }],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        messages=[{"role": "user", "content": user_msg}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == _TOOL_NAME:
            return dict(block.input)
    raise ValueError(f"Claude did not return a {_TOOL_NAME} tool call")


def _call_azure(user_msg: str) -> dict[str, Any]:
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
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": _TOOL_NAME,
                "description": _TOOL_DESCRIPTION,
                "parameters": _TOOL_PARAMETERS,
            },
        }],
        tool_choice={"type": "function", "function": {"name": _TOOL_NAME}},
    )
    if not completion.choices:
        raise ValueError("Azure OpenAI returned no choices for domain extraction")
    for call in getattr(completion.choices[0].message, "tool_calls", None) or []:
        if call.function.name == _TOOL_NAME:
            return json.loads(call.function.arguments or "{}")
    raise ValueError(f"Azure OpenAI did not call {_TOOL_NAME}")


def _clean_list(raw: Any) -> list[str]:
    """Normalize one returned list to deduplicated bare hostnames, in order.

    Args:
        raw: The `include` or `exclude` value from the tool call.

    Returns:
        Hostnames that survive normalization; anything else is dropped.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        host = _normalize_domain(entry)
        if host and host not in seen:
            seen.add(host)
            out.append(host)
    return out


def extract_relevancy_domains(relevancy_prompt: str | None) -> dict[str, list[str]]:
    """Read the include / exclude publication domains out of a relevancy prompt.

    Args:
        relevancy_prompt: The project's free-text relevancy criteria.

    Returns:
        ``{"include": [...], "exclude": [...]}`` of bare hostnames. Both lists are
        empty when the prompt is blank or the LLM call fails.
    """
    empty: dict[str, list[str]] = {"include": [], "exclude": []}
    criteria = (relevancy_prompt or "").strip()
    if not criteria:
        return empty

    provider = envs.LLM_PROVIDER
    try:
        user_msg = f"Extract the include / exclude publication domains from this brief:\n\n{criteria}"
        if provider in _AZURE_ALIASES:
            payload = _call_azure(user_msg)
        elif provider in _CLAUDE_ALIASES:
            payload = _call_claude(user_msg)
        else:
            raise RuntimeError(f"Unknown LLM_PROVIDER='{provider}'. Use 'claude' or 'azure_openai'.")
    except Exception as exc:  # noqa: BLE001 — saving the prompt must never fail on this
        logger.exception(f"Domain extraction failed; storing empty domain lists: {exc}")
        return empty

    include = _clean_list(payload.get("include"))
    exclude = _clean_list(payload.get("exclude"))
    # An exclusion always wins, so a host in both lists is only an exclusion.
    include = [h for h in include if h not in set(exclude)]

    logger.info(f"Extracted relevancy domains: {len(include)} include, {len(exclude)} exclude.")
    return {"include": include, "exclude": exclude}
