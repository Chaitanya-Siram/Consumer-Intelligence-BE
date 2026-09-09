"""Provider-agnostic LLM text / JSON completion for the agents package.

Switches between Anthropic Claude and Azure OpenAI based on `envs.LLM_PROVIDER`,
mirroring the alias sets used in ai_helpers. The agent pipeline is a sequence of
single-turn completions (classify intent, generate code, answer question) rather
than a provider-specific tool-call loop, so a single text/JSON surface keeps both
providers behaving identically.
"""
from __future__ import annotations

import json
import re
from typing import Any

from configs import envs, logger

_CLAUDE_ALIASES = {"claude", "anthropic"}
_AZURE_ALIASES = {"azure_openai", "azure-openai", "azure", "openai", "gpt", "gpt-azure"}

_anthropic_client = None
_azure_client = None
_azure_responses_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic

        if not envs.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _anthropic_client = Anthropic(api_key=envs.ANTHROPIC_API_KEY)
    return _anthropic_client


def _get_azure_responses():
    """OpenAI client bound to the Azure v1 endpoint, which serves the Responses API.

    Azure exposes web search only through Responses, and that endpoint takes the plain
    OpenAI client against `<resource>/openai/v1/` — not AzureOpenAI with an api-version.

    Returns:
        An OpenAI client pointed at the Azure v1 endpoint.
    """
    global _azure_responses_client
    if _azure_responses_client is None:
        from openai import OpenAI

        if not envs.AZURE_OPENAI_ENDPOINT or not envs.AZURE_OPENAI_API_KEY:
            raise RuntimeError("Azure OpenAI is not configured for web search.")
        base_url = envs.AZURE_OPENAI_ENDPOINT.rstrip("/") + "/openai/v1/"
        _azure_responses_client = OpenAI(
            base_url=base_url, api_key=envs.AZURE_OPENAI_API_KEY, timeout=600.0
        )
    return _azure_responses_client


def _azure_web_search(system: str, user: str) -> str:
    """Grounded completion via the Azure Responses API's `web_search` tool.

    Args:
        system: System instructions.
        user: User message.

    Returns:
        The model's grounded text.
    """
    resp = _get_azure_responses().responses.create(
        model=envs.AZURE_OPENAI_WEB_SEARCH_MODEL,
        tools=[{"type": "web_search"}],
        input=f"{system}\n\n{user}",
    )
    # Absent a web_search_call the model answered from memory; useful to know in the logs.
    if not any(getattr(item, "type", "") == "web_search_call" for item in resp.output):
        logger.info("Azure web search: no web_search_call in response (answered from memory)")
    return (resp.output_text or "").strip()


def _get_azure():
    global _azure_client
    if _azure_client is None:
        from openai import AzureOpenAI

        missing = [
            name
            for name, val in (
                ("AZURE_OPENAI_API_KEY", envs.AZURE_OPENAI_API_KEY),
                ("AZURE_OPENAI_ENDPOINT", envs.AZURE_OPENAI_ENDPOINT),
                ("AZURE_OPENAI_MODEL", envs.AZURE_OPENAI_MODEL),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(f"Azure OpenAI is not configured. Missing: {', '.join(missing)}")
        _azure_client = AzureOpenAI(
            api_key=envs.AZURE_OPENAI_API_KEY,
            azure_endpoint=envs.AZURE_OPENAI_ENDPOINT,
            api_version=envs.AZURE_OPENAI_API_VERSION,
            timeout=600.0,
        )
    return _azure_client


def complete(system: str, user: str, max_tokens: int = 4096, temperature: float = 0.0) -> str:
    """Return the model's plain-text response to (system, user)."""
    provider = envs.LLM_PROVIDER
    if provider in _AZURE_ALIASES:
        client = _get_azure()
        completion = client.chat.completions.create(
            model=envs.AZURE_OPENAI_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        if not completion.choices:
            raise ValueError("Azure OpenAI returned no choices")
        return (completion.choices[0].message.content or "").strip()

    if provider in _CLAUDE_ALIASES:
        client = _get_anthropic()
        resp = client.messages.create(
            model=envs.CLAUDE_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        parts = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()

    raise RuntimeError(f"Unknown LLM_PROVIDER='{provider}'. Use 'claude' or 'azure_openai'.")


def complete_web(system: str, user: str, max_tokens: int = 4096, max_uses: int = 5) -> str:
    """Plain-text completion grounded in live web results.

    Prefers Claude's server-side `web_search` tool when an Anthropic key is set (even
    when LLM_PROVIDER is Azure), then Azure's Responses API `web_search` tool. With
    neither available — or if the grounded call fails, e.g. an admin has blocked the
    tool — it degrades to a normal knowledge-only completion rather than raising.

    Args:
        system: System instructions.
        user: User message.
        max_tokens: Output cap for the Claude path.
        max_uses: Max Claude web searches per request.

    Returns:
        The model's answer, grounded when web search was available.
    """
    if envs.ANTHROPIC_API_KEY:
        try:
            return _claude_web_search(system, user, max_tokens, max_uses)
        except Exception as exc:  # noqa: BLE001
            # A present-but-unusable key (expired, no credit) must not block the fallback.
            logger.warning(f"Claude web search unavailable ({exc}); trying next option")

    if envs.AZURE_OPENAI_ENDPOINT and envs.AZURE_OPENAI_API_KEY:
        try:
            return _azure_web_search(system, user)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Azure web search unavailable ({exc}); using knowledge-only")

    logger.info("complete_web: no web search available — knowledge-only completion")
    return complete(system, user, max_tokens=max_tokens, temperature=0.0)


def _claude_web_search(system: str, user: str, max_tokens: int, max_uses: int) -> str:
    """Grounded completion via Claude's server-side `web_search` tool.

    Args:
        system: System instructions.
        user: User message.
        max_tokens: Output cap.
        max_uses: Max searches per request.

    Returns:
        The model's grounded text.
    """
    resp = _get_anthropic().messages.create(
        model=envs.CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}],
    )
    # The model interleaves search/tool blocks with text; the answer is the text.
    parts = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"]
    return "".join(parts).strip()


def _drop_trailing_commas(text: str) -> str:
    """Remove commas before a closing brace/bracket — GPT-4.1 emits them intermittently."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _parse_json(raw: str, where: str) -> Any:
    """Parse a model response as JSON, salvaging fences, surrounding prose and
    trailing commas.

    Args:
        raw: The model's raw text.
        where: Caller name, for the log line.

    Returns:
        The parsed JSON value.
    """
    cleaned = _strip_code_fences(raw)
    candidates = [cleaned]
    match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
    if match:
        candidates.append(match.group(1))
    for candidate in list(candidates):
        candidates.append(_drop_trailing_commas(candidate))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    logger.warning(f"{where} could not parse model output: {cleaned[:200]}")
    raise json.JSONDecodeError("Could not parse model output as JSON", cleaned, 0)


def complete_json_web(system: str, user: str, max_tokens: int = 4096, max_uses: int = 5) -> Any:
    """Like complete_json, but with live web search (Claude). Falls back to
    knowledge-only JSON when no Anthropic key is configured."""
    raw = complete_web(system, user, max_tokens=max_tokens, max_uses=max_uses)
    return _parse_json(raw, "complete_json_web")


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def complete_json(system: str, user: str, max_tokens: int = 4096) -> Any:
    """Completion whose response is parsed as JSON. Tolerates markdown fences,
    leading/trailing prose and trailing commas."""
    raw = complete(system, user, max_tokens=max_tokens, temperature=0.0)
    return _parse_json(raw, "complete_json")
