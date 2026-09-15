"""Async LLM client for Consumer Intelligence narrative generation.

Supports both LLM_PROVIDER options from configs.py:
- "gpt" → Azure OpenAI (httpx async)
- "claude" → Anthropic async client

Used only for prose generation, never for article tagging.
"""

import json
import logging

from configs import envs, logger


class CINarrativeClient:
    """Thin async JSON-mode chat client for CI narrative prose."""

    async def complete_json(self, messages: list[dict]) -> dict:
        if envs.LLM_PROVIDER == "claude":
            return await self._call_claude(messages)
        return await self._call_azure(messages)

    async def _call_azure(self, messages: list[dict]) -> dict:
        import httpx

        if not envs.AZURE_OPENAI_API_KEY or not envs.AZURE_OPENAI_ENDPOINT:
            raise RuntimeError("Azure OpenAI is not configured.")

        api_version = getattr(envs, "AZURE_OPENAI_API_VERSION", "2024-02-01")
        url = (
            f"{envs.AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
            f"{envs.AZURE_OPENAI_MODEL}/chat/completions"
            f"?api-version={api_version}"
        )
        body = {
            "messages": messages,
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "api-key": envs.AZURE_OPENAI_API_KEY,
                },
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        return json.loads(data["choices"][0]["message"]["content"])

    async def _call_claude(self, messages: list[dict]) -> dict:
        from anthropic import AsyncAnthropic

        if not envs.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")

        system_messages = [m for m in messages if m.get("role") == "system"]
        user_messages = [m for m in messages if m.get("role") != "system"]
        system_text = "\n\n".join(m["content"] for m in system_messages)

        client = AsyncAnthropic(api_key=envs.ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=envs.CLAUDE_MODEL,
            max_tokens=4096,
            system=system_text + "\n\nReturn JSON only. No markdown fences.",
            messages=user_messages,
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(text)


_client: CINarrativeClient | None = None


def get_narrative_client() -> CINarrativeClient:
    global _client
    if _client is None:
        _client = CINarrativeClient()
    return _client
