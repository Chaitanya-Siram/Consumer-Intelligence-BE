from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from ai_helpers.llm_service import _AZURE_ALIASES
from configs import envs, logger


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt(filename: str) -> str:
    path = _PROMPTS_DIR / filename
    return path.read_text(encoding="utf-8")


TAGGING_DEFAULT_PROMPT = _load_prompt("tagging.txt")

TAG_TOOL_NAME = "tag_articles_batch"
TAG_TOOL_DESCRIPTION = (
    "Record sentiment, theme, per-field confidence scores, and explainable reason for each article in the batch."
)


def build_tag_tool_parameters(brand_keywords: list[str], sections_prompt: str | None):
    brand_name = brand_keywords[0] if brand_keywords else "Brand"

    if sections_prompt:
        section_field: dict[str, Any] = {
            "type": "string",
            "description": "The section this article belongs to. Use ONLY the section names listed in the system prompt — match the exact spelling.",
        }
    else:
        section_field = {
            "type": "string",
            "enum": [f"{brand_name} News", "Competitors News", "Industry News"],
            "description": f"Which section the article belongs to relative to {brand_name}.",
        }

    return {
        "type": "object",
        "properties": {
            "taggings": {
                "type": "array",
                "description": "One entry per input article, in the same order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The original article id (e.g. A1)."},
                        "sentiment": {
                            "type": "string",
                            "enum": ["POS", "NEG", "NEU"],
                            "description": f"Sentiment toward {brand_name} only.",
                        },
                        "theme": {"type": "string", "description": "Concise 2-5 word theme label."},
                        "summary": {
                            "type": "string",
                            "description": "A concise, neutral 2-3 sentence plain summary of the article's own key points. Summarize ONLY what the article reports. Never use the words 'the article' or 'this article' anywhere. Do NOT add meta-commentary about the presence or absence of any brand/company (e.g. 'No specific company or brand is cited') — simply omit what isn't there; never point out its absence.",
                        },
                        "sentiment_confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "Confidence (0-1) in the sentiment classification."},
                        "theme_confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "Confidence (0-1) in the theme classification."},
                        "section_category_confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "Confidence (0-1) in the section category assignment."},
                        "relevancy_confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": f"Relevancy score (0-1) — how relevant the article is to {brand_name}, its competitors, or their industry."},
                        "relevancy_reason": {
                            "type": "string",
                            "description": f"1-2 short sentences explaining the relevancy score — what makes the article relevant (or not) to {brand_name}, its competitors, or their industry.",
                        },
                        "xai_theme_reason": {
                            "type": "string",
                            "description": "Concise explainable AI rationale for theme classification."
                        },
                        "xai_sentiment_reason": {
                            "type": "string",
                            "description": f"Concise explainable AI rationale for sentiment classification, citing how {brand_name} is portrayed.",
                        },
                        "brand_of_interest": {
                            "type": "array",
                            "description": "List of brand keywords from the provided <brands> list that are mentioned in the article — either directly or via a synonym, abbreviation, ticker, handle, parent-brand or product/sub-brand variant. ALWAYS return the EXACT original keyword as given in <brands>, NOT the variant found in the article. Example: if <brands> is ['American Airlines'] and the article says 'AmericanAir', return ['American Airlines']. If <brands> is ['Tesla'] and the article says 'TSLA' or 'Cybertruck', return ['Tesla']. Empty list if none of the brand keywords or their variants are present.",
                            "items": {"type": "string"},
                        },
                        "competitors": {
                            "type": "array",
                            "description": "List of competitor brand keywords from the provided <competitors> list that are mentioned in the article — either directly or via synonyms, alternate spellings, abbreviations, tickers, handles, parent-brands, or product/sub-brand names. Example: if <competitors> is ['Delta Air Lines'] and the article says 'Delta', return ['Delta Air Lines']. If <competitors> is ['Ford'] and the article mentions 'Ford Motor Company', or 'Mustang', return ['Ford']. Empty list if no competitors are mentioned.",
                            "items": {"type": "string"},
                        },
                        "other_competitors": {
                            "type": "array",
                            "description": "List of any other competitor brands mentioned in the article that are NOT in the provided <competitors> list. Reveals competitive context beyond what was pre-identified. Only actual brand names mentioned in the text — no synonyms or related entities. Empty list if none.",
                            "items": {"type": "string"},
                        },
                        "priority_watch": {
                            "type": "boolean",
                            "description": "True if the article needs priority attention (negative sentiment, crisis, competitive threat, regulatory issue, viral content).",
                        },
                        "section": section_field,
                        "peoples": {
                            "type": "array",
                            "description": "List any people mentioned in the article who are relevant to the brand of interest (e.g. CEO, founder, key executives, public figures closely associated with the brand). Include full names and titles/roles if available. Empty list if none.",
                            "items": {"type": "string"},
                        },
                        "countries": {
                            "type": "array",
                            "description": "List any countries mentioned in the article that are relevant to the brand of interest (e.g. where the brand operates, is headquartered, or is involved in news). Empty list if none.",
                            "items": {"type": "string"},
                        },
                        "organizations": {
                            "type": "array",
                            "description": "List any organizations mentioned in the article that are relevant to the brand of interest (e.g. parent company, subsidiaries, partners, regulatory bodies). Empty list if none.",
                            "items": {"type": "string"},
                        }
                    },
                    "required": [
                        "id", "sentiment", "theme", "summary",
                        "sentiment_confidence", "theme_confidence", "section_category_confidence",
                        "relevancy_confidence", "relevancy_reason",
                        "xai_theme_reason", "xai_sentiment_reason",
                        "brand_of_interest", "competitors", "other_competitors", "priority_watch", "section",
                        "peoples", "countries", "organizations"
                    ],
                },
            }
        },
        "required": ["taggings"],
    }


def _default_sections_prompt(brand: str) -> str:
    """Fallback section list, mirroring the default enum in build_tag_tool_parameters."""
    return (
        f"- {brand} News: the article is primarily about {brand}.\n"
        f"- Competitors News: the article is primarily about a competitor of {brand}.\n"
        f"- Industry News: the article is about the broader industry, not {brand} or a specific competitor."
    )


def get_system_prompt(
    brand_keywords: list[str],
    competitor_keywords: list[str],
    sections_prompt: str | None = None,
) -> str:
    """ Replace the keywords in Prompt """
    brand = brand_keywords[0].strip() if brand_keywords else "Brand"
    competitors_str = ", ".join(competitor_keywords) if competitor_keywords else ""
    sections = sections_prompt.strip() if sections_prompt and sections_prompt.strip() else _default_sections_prompt(brand)
    return (
        TAGGING_DEFAULT_PROMPT
        .replace("{{BRAND}}", brand)
        .replace("{{Brand}}", brand)
        .replace("{{COMPETITORS}}", competitors_str)
        .replace("{{SECTIONS_PROMPT}}", sections)
    )


def build_batch_message(articles: list[dict[str, Any]]) -> str:
    """Format the batch of articles into the user message. Brand context is
    already in the system prompt via {{BRAND}} substitution, so it's not
    repeated here."""
    lines: list[str] = [
        f"Tag every one of the {len(articles)} articles below. "
        "Use the original id verbatim. Return exactly one entry per article."
    ]
    for a in articles:
        lines.append("")
        lines.append(f"--- {a['id']} ---")
        title = a.get("title") or ""
        if title:
            lines.append(f"Title: {title}")
        body = a.get("article_text") or a.get("content") or ""

        if title.lower().strip() != body.lower().strip():
            lines.append(f"Body: {body}")
    return "\n".join(lines)


@dataclass
class FieldConfigs:
    fields: list[str]
    list_fields: list[str] = field(default_factory=list)


FIELDS_CONFIG = FieldConfigs(
    fields=[
        "id", "sentiment", "theme", "summary",
        "sentiment_confidence", "theme_confidence", "section_category_confidence",
        "relevancy_confidence", "relevancy_reason",
        "xai_theme_reason", "xai_sentiment_reason", "priority_watch", "section",
    ],
    list_fields=["brand_of_interest", "competitors", "other_competitors", "peoples", "countries", "organizations"],
)


def blank_tagging(article_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"id": article_id}
    all_fields = set(FIELDS_CONFIG.fields + FIELDS_CONFIG.list_fields)
    list_fields = set(FIELDS_CONFIG.list_fields)
    for f in all_fields:
        if f == "id":
            continue
        out[f] = [] if f in list_fields else None
    return out


def align_taggings(
    article_ids: list[str],
    taggings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Align tool-call output to input ids; fill missing entries with defaults.
    The set of fields and their default-empty values are driven by the lens schema."""
    all_fields = set(FIELDS_CONFIG.fields + FIELDS_CONFIG.list_fields)
    list_fields = set(FIELDS_CONFIG.list_fields)
    by_id = {t.get("id"): t for t in taggings if isinstance(t, dict)}
    out: list[dict[str, Any]] = []
    for aid in article_ids:
        t = by_id.get(aid)
        if not t:
            out.append(blank_tagging(aid))
            continue
        row: dict[str, Any] = {"id": aid}
        for f in all_fields:
            if f == "id":
                continue
            if f in list_fields:
                row[f] = t.get(f) or []
            else:
                row[f] = t.get(f)
        out.append(row)
    return out


def project_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the fields needed for tagging — drops original metadata.

    The file parser normalizes the body column to `content` (see
    file_parser._STANDARD_FIELD_MAP), so fall back to `content` for the article
    body. Without this the body never reaches the model and tagging runs on the
    title alone."""
    return [
        {
            "id": a.get("id"),
            "title": a.get("title"),
            "article_text": a.get("article_text") or a.get("content"),
        }
        for a in articles
    ]


def _missing_ids(
    articles: list[dict[str, Any]], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {r.get("id"): r for r in results}
    return [
        a for a in articles
        if by_id.get(a["id"], {}).get("sentiment") is None
    ]


def _tag_with_split_retry(
    articles: list[dict[str, Any]],
    batch_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    depth: int = 0,
) -> list[dict[str, Any]]:
    """Run batch_fn; if some ids come back blank, retry the missing ones in halves."""
    results = batch_fn(articles)
    missing = _missing_ids(articles, results)
    if not missing or len(articles) <= 1:
        return results
    if len(missing) == len(articles):
        # whole batch failed (API error / no tool call) — don't recurse
        return results

    logger.warning(
        f"[depth={depth}] {len(missing)}/{len(articles)} ids missing — "
        f"retrying in halves"
    )
    by_id = {r["id"]: r for r in results}
    mid = max(1, len(missing) // 2)
    halves = [missing[:mid], missing[mid:]]
    for half in halves:
        if not half:
            continue
        sub = _tag_with_split_retry(half, batch_fn, depth + 1)
        for r in sub:
            if r.get("sentiment") is not None:
                by_id[r["id"]] = r
    return [by_id[a["id"]] for a in articles]


# How many times to re-attempt articles that came back completely untagged
# (e.g. a whole batch that hit an API error / returned no tool call).
MAX_UNTAGGED_RETRY_ROUNDS = 2


def _needs_retry(result: dict[str, Any]) -> bool:
    """An article is considered untagged when both sentiment and its confidence are
    null — i.e. it carries only default/blank values and should be retried."""
    return result.get("sentiment") is None and result.get("sentiment_confidence") is None


def _retry_untagged(
    projected: list[dict[str, Any]],
    results: list[dict[str, Any]],
    batch_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Final safety net: re-tag any article that came back untagged (null
    sentiment + confidence). Catches whole-batch failures that the per-batch
    split-retry intentionally skips. Re-runs up to MAX_UNTAGGED_RETRY_ROUNDS
    times, stopping early when nothing is left or no progress is made.
    `projected` is the id/title/article_text list the model was actually given.
    """
    by_id = {r.get("id"): r for r in results}
    proj_by_id = {a.get("id"): a for a in projected}

    for round_no in range(1, MAX_UNTAGGED_RETRY_ROUNDS + 1):
        pending_ids = [aid for aid, r in by_id.items() if _needs_retry(r)]
        retry_articles = [proj_by_id[aid] for aid in pending_ids if aid in proj_by_id]
        if not retry_articles:
            break

        logger.warning(
            f"Untagged retry round {round_no}/{MAX_UNTAGGED_RETRY_ROUNDS}: "
            f"re-tagging {len(retry_articles)} article(s) with null sentiment/confidence"
        )
        if emit is not None:
            try:
                emit({
                    "type": "progress",
                    "message": f"Retrying {len(retry_articles)} untagged article(s) (round {round_no})…",
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"retry emit callback raised: {exc}")

        retried = _tag_with_split_retry(retry_articles, batch_fn)
        progressed = False
        for r in retried:
            if not _needs_retry(r):
                by_id[r["id"]] = r
                progressed = True
        if not progressed:
            logger.warning(f"Untagged retry round {round_no} recovered nothing — giving up")
            break

    return [by_id[a["id"]] for a in projected]


def run_in_batches_streaming(
    articles: list[dict[str, Any]],
    batch_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    on_batch_done: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    """Like run_in_batches, but invokes on_batch_done(payload) as each chunk
    finishes (in completion order, not submission order). Used by the WebSocket
    streaming endpoint. Does not modify the non-streaming run_in_batches path.
    """
    if not articles:
        return []
    articles = project_articles(articles)
    batch_size = max(1, envs.LLM_BATCH_SIZE)
    chunks = [articles[i: i + batch_size]
              for i in range(0, len(articles), batch_size)]
    total_batches = len(chunks)
    results_by_index: dict[int, list[dict[str, Any]]] = {}

    def run_chunk(idx: int, chunk: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
        return idx, _tag_with_split_retry(chunk, batch_fn)

    max_workers = max(1, min(envs.LLM_CONCURRENCY, total_batches))
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(run_chunk, i, c) for i, c in enumerate(chunks)]
        for fut in as_completed(futures):
            try:
                idx, result = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Streaming batch failed: {exc}")
                continue
            results_by_index[idx] = result
            completed += 1
            try:
                on_batch_done(
                    {
                        "type": "batch",
                        "batch_index": idx,
                        "completed_batches": completed,
                        "total_batches": total_batches,
                        "tagged_count": len(result),
                        "articles": result,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"on_batch_done callback raised: {exc}")

    out: list[dict[str, Any]] = []
    for i in range(total_batches):
        if i in results_by_index:
            out.extend(results_by_index[i])

    # Re-attempt anything that came back untagged (null sentiment + confidence).
    out = _retry_untagged(articles, out, batch_fn, emit=on_batch_done)
    return out


def run_in_batches(
    articles: list[dict[str, Any]],
    batch_fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Chunk into LLM_BATCH_SIZE batches and run batch_fn concurrently.

    Each chunk auto-retries any ids the model skipped by recursively splitting
    the missing subset in half until everything is tagged or batch size hits 1.
    """
    if not articles:
        return []
    articles = project_articles(articles)
    batch_size = max(1, envs.LLM_BATCH_SIZE)
    chunks = [articles[i: i + batch_size]
              for i in range(0, len(articles), batch_size)]

    def run_chunk(chunk: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _tag_with_split_retry(chunk, batch_fn)

    if len(chunks) == 1 or envs.LLM_CONCURRENCY <= 1:
        results = [run_chunk(c) for c in chunks]
    else:
        with ThreadPoolExecutor(max_workers=min(envs.LLM_CONCURRENCY, len(chunks))) as pool:
            results = list(pool.map(run_chunk, chunks))
    out: list[dict[str, Any]] = []
    for r in results:
        out.extend(r)

    # Re-attempt anything that came back untagged (null sentiment + confidence).
    out = _retry_untagged(articles, out, batch_fn)
    return out


def merge_tagged_with_syndication(
    to_tag: list[dict[str, Any]],
    syndications: list[dict[str, Any]],
    syndications_to_main: dict[str, str],
    tagged: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge tags for the actually-tagged (non-copy) articles, then attach each
    syndicated copy with its MAIN article's tags copied over.

    Syndicated copies are never sent to the model (to save tokens); they share
    their main article's classification. `copy_to_main` maps each copy id to its
    main id (from article_linker.split_syndicated). Copies keep their own metadata
    (url, date, source, reach…) but take the main's tag fields."""
    merged = merge_tagged_with_articles(to_tag, tagged)

    tagged_by_id = {t["id"]: t for t in tagged if isinstance(t, dict) and t.get("id") is not None}
    # A per-copy tagging dict = the main's tags but keyed by the copy's own id, so
    # merge_tagged_with_articles attaches them to the right article.
    copy_taggings: list[dict[str, Any]] = []
    for c in syndications:
        main_tag = tagged_by_id.get(syndications_to_main.get(c.get("id"), ""))
        if main_tag:
            copy_taggings.append({**main_tag, "id": c.get("id")})
    merged += merge_tagged_with_articles(syndications, copy_taggings)
    return merged


def merge_tagged_with_articles(articles: list[dict[str, Any]], tagged: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge tagging output back onto the source articles, dropping article_text."""
    tagged_dict = {t["id"]: t for t in tagged if t.get("id") is not None}
    out: list[dict[str, Any]] = []
    for article in articles:
        aid = article.get("id")
        article_meta = {k: v for k, v in article.items() if k != "article_text"}
        if aid in tagged_dict:
            out.append({**article_meta, **tagged_dict[aid]})
        else:
            out.append({**article_meta, "summary": None, "sentiment": None, "theme": None, "reason": None,
                        "sentiment_confidence": None, "theme_confidence": None, "section_category_confidence": None,
                        "relevancy_confidence": None, "relevancy_reason": None})
    return out