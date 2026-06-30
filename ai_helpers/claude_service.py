from typing import Any
from anthropic import Anthropic
from configs import envs, logger
from .tagging_common import (
    TAG_TOOL_DESCRIPTION,
    TAG_TOOL_NAME,
    align_taggings,
    blank_tagging,
    build_batch_message,
    get_system_prompt,
    build_tag_tool_parameters,
    run_in_batches,
    run_in_batches_streaming,
)

_client: Anthropic | None = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        if not envs.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = Anthropic(api_key=envs.ANTHROPIC_API_KEY)
    return _client


def _extract_taggings(response: Any) -> list[dict[str, Any]]:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == TAG_TOOL_NAME:
            return list(dict(block.input).get("taggings", []))
    raise ValueError(f"Claude did not return a {TAG_TOOL_NAME} tool call")


def _tag_batch(
    articles: list[dict[str, Any]],
    brand_keywords: list[str],
    competitor_keywords: list[str],
    sections_prompt: str | None = None,
) -> list[dict[str, Any]]:
    ids = [a["id"] for a in articles]
    client = _get_client()
    try:
        logger.info(f"Tagging batch of {len(articles)} articles with Claude...")
        with client.messages.stream(
            model=envs.CLAUDE_MODEL,
            max_tokens=envs.MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": get_system_prompt(brand_keywords, competitor_keywords, sections_prompt),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[
                {
                    "name": TAG_TOOL_NAME,
                    "description": TAG_TOOL_DESCRIPTION,
                    "input_schema": build_tag_tool_parameters(brand_keywords, sections_prompt),
                }
            ],
            tool_choice={"type": "tool", "name": TAG_TOOL_NAME},
            messages=[{"role": "user", "content": build_batch_message(articles)}],
        ) as stream:
            response = stream.get_final_message()
        taggings = _extract_taggings(response)
    except Exception as exc:
        logger.error(f"Claude batch tagging failed for {len(articles)} articles: {exc}")
        return [blank_tagging(aid) for aid in ids]

    return align_taggings(ids, taggings)


def tag_articles(
    articles: list[dict[str, Any]],
    brand_keywords: list[str],
    competitor_keywords: list[str],
    sections_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Tag with Claude. Schema depends on lens_label."""
    def batch_fn(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _tag_batch(batch, brand_keywords, competitor_keywords, sections_prompt)
    return run_in_batches(articles, batch_fn)


def tag_articles_streaming(
    articles: list[dict[str, Any]],
    brand_keywords: list[str],
    competitor_keywords: list[str],
    sections_prompt: str | None = None,
    on_batch_done: Any = None,
) -> list[dict[str, Any]]:
    """Streaming variant: emits on_batch_done(payload) as each chunk finishes."""
    def batch_fn(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _tag_batch(batch, brand_keywords, competitor_keywords, sections_prompt)
    cb = on_batch_done if callable(on_batch_done) else (lambda _: None)
    return run_in_batches_streaming(articles, batch_fn, cb)