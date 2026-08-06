"""LLM provider factory — Azure OpenAI or Anthropic Claude, chosen by env.

Reuses the app's existing ``LLM_PROVIDER`` switch (``gpt``/``azure`` → Azure
OpenAI, ``claude``/``anthropic`` → Claude) and the existing Azure/Anthropic
config. Returns a LlamaIndex ``LLM`` so the retrieval agent + chat generation stay
provider-agnostic.
"""

from __future__ import annotations

from functools import lru_cache

from configs import envs

# Generous default; generation answers are short relative to this ceiling.
_MAX_TOKENS = 4096


@lru_cache
def get_llm():
    provider = (envs.LLM_PROVIDER or "").strip().lower()

    if provider in ("claude", "anthropic"):
        from llama_index.llms.anthropic import Anthropic

        return Anthropic(
            model=envs.CLAUDE_MODEL,
            api_key=envs.ANTHROPIC_API_KEY,
            max_tokens=_MAX_TOKENS,
        )

    if provider in ("gpt", "azure", "azure_openai"):
        from llama_index.llms.azure_openai import AzureOpenAI

        return AzureOpenAI(
            engine=envs.AZURE_OPENAI_MODEL,  # Azure deployment name
            api_key=envs.AZURE_OPENAI_API_KEY,
            azure_endpoint=envs.AZURE_OPENAI_ENDPOINT,
            api_version=envs.AZURE_OPENAI_API_VERSION,
            max_tokens=_MAX_TOKENS,
        )

    raise ValueError(f"Unknown LLM_PROVIDER: {envs.LLM_PROVIDER!r}")
