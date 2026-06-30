from typing import Any
from configs import envs, logger

_CLAUDE_ALIASES = {"claude", "anthropic"}
_AZURE_ALIASES = {"azure_openai", "azure-openai", "azure", "openai", "gpt", "gpt-azure"}


def tag_articles(
    articles: list[dict[str, Any]],
    brand_keywords: list[str],
    competitor_keywords: list[str],
    sections_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Dispatch tagging to the configured LLM provider.

    Args:
        articles: records with id/title/article_text.
        brand_keywords: synonymous names of the brand to score sentiment against
            (e.g. ['Acme', 'Acme Inc']). When provided, sentiment is aspect-based
            toward that brand only.
        lens_label: workflow lens (e.g. "Media Monitoring") that selects the
            prompt + tool schema. None or any other value uses the default lens.
        skill_prompt: optional per-branch override. For Media Monitoring this
            replaces {{SECTIONS_PROMPT}} in the dynamic-section template; for
            other lenses it is ignored.
    """
    provider = envs.LLM_PROVIDER
    if provider in _AZURE_ALIASES:
        from . import openai_service
        return openai_service.tag_articles(articles, brand_keywords, competitor_keywords, sections_prompt)
    if provider in _CLAUDE_ALIASES:
        from . import claude_service
        return claude_service.tag_articles(articles, brand_keywords, competitor_keywords, sections_prompt)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER='{provider}'. Use 'claude' or 'azure_openai'."
    )


def tag_articles_streaming(
    articles: list[dict[str, Any]],
    brand_keywords: list[str],
    competitor_keywords: list[str],
    sections_prompt: str | None = None,
    on_batch_done: Any = None,
) -> list[dict[str, Any]]:
    """Streaming dispatcher — emits on_batch_done(payload) per completed batch."""
    provider = envs.LLM_PROVIDER
    if provider in _AZURE_ALIASES:
        from . import openai_service
        return openai_service.tag_articles_streaming(articles, brand_keywords, competitor_keywords, sections_prompt, on_batch_done)
    if provider in _CLAUDE_ALIASES:
        from . import claude_service
        return claude_service.tag_articles_streaming(articles, brand_keywords, competitor_keywords, sections_prompt, on_batch_done)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER='{provider}'. Use 'claude' or 'azure_openai'."
    )
