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

    async def complete_json(self, messages: list[dict], *, temperature: float = 0.3) -> dict:
        """`temperature` 0 for classification calls (theme taxonomy) so the
        grouping, and every index computed on it, stays stable between runs."""
        if envs.LLM_PROVIDER == "claude":
            return await self._call_claude(messages, temperature=temperature)
        return await self._call_azure(messages, temperature=temperature)

    async def _call_azure(self, messages: list[dict], *, temperature: float = 0.3) -> dict:
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
            "temperature": temperature,
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

    async def _call_claude(self, messages: list[dict], *, temperature: float = 0.3) -> dict:
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
            temperature=temperature,
            system=system_text + "\n\nReturn JSON only. No markdown fences.",
            messages=user_messages,
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return json.loads(text)


_BRAND_CHECK_PROMPT = (
    "Does this image clearly show the brand '{brand}' — its logo, packaging, "
    "product, storefront, or another unmistakable brand mark? Reply with "
    "exactly one word: YES or NO."
)


class CIVisionClient:
    """A single yes/no vision call, reusing the same provider switch and
    credentials as CINarrativeClient. Kept separate because none of the prose
    call sites need image content blocks."""

    async def image_shows_brand(self, image_bytes: bytes, media_type: str, brand: str) -> bool | None:
        """True/False on a confident answer; None when the call itself failed
        (missing key, network, rate limit) — callers should fail open rather
        than discard a real image over an inconclusive classifier."""
        import base64

        image_b64 = base64.b64encode(image_bytes).decode()
        try:
            if envs.LLM_PROVIDER == "claude":
                text = await self._call_claude(image_b64, media_type, brand)
            else:
                text = await self._call_azure(image_b64, media_type, brand)
        except Exception as exc:
            logger.info(f"Hero image brand check failed, keeping the image: {exc}")
            return None
        verdict = (text or "").strip().upper()
        if verdict.startswith("Y"):
            return True
        if verdict.startswith("N"):
            return False
        return None

    async def _call_claude(self, image_b64: str, media_type: str, brand: str) -> str:
        from anthropic import AsyncAnthropic

        if not envs.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        client = AsyncAnthropic(api_key=envs.ANTHROPIC_API_KEY)
        response = await client.messages.create(
            model=envs.CLAUDE_MODEL,
            max_tokens=6,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": _BRAND_CHECK_PROMPT.format(brand=brand)},
                ],
            }],
        )
        return response.content[0].text

    async def _call_azure(self, image_b64: str, media_type: str, brand: str) -> str:
        import httpx

        if not envs.AZURE_OPENAI_API_KEY or not envs.AZURE_OPENAI_ENDPOINT:
            raise RuntimeError("Azure OpenAI is not configured.")
        api_version = getattr(envs, "AZURE_OPENAI_API_VERSION", "2024-02-01")
        url = (
            f"{envs.AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
            f"{envs.AZURE_OPENAI_MODEL}/chat/completions?api-version={api_version}"
        )
        body = {
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": _BRAND_CHECK_PROMPT.format(brand=brand)},
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
                ],
            }],
            "max_tokens": 6,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json", "api-key": envs.AZURE_OPENAI_API_KEY},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"]


_vision_client: CIVisionClient | None = None


def get_vision_client() -> CIVisionClient:
    global _vision_client
    if _vision_client is None:
        _vision_client = CIVisionClient()
    return _vision_client


_client: CINarrativeClient | None = None


def get_narrative_client() -> CINarrativeClient:
    global _client
    if _client is None:
        _client = CINarrativeClient()
    return _client
