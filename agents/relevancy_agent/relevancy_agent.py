"""Relevancy agent — the gate that runs BEFORE tagging.

Given freshly-parsed articles (from an uploaded CSV/Excel/JSON or a web fetch),
it asks the LLM to decide whether each article is relevant, using the project's
``relevancy_prompt`` as the criteria (configured per project in the DB / review
UI).

Each article is annotated in place with:
  * ``is_relevant``            — bool
  * ``relevancy_reason``       — short reason, set for relevant and irrelevant alike
  * ``relevancy_confidence``   — 0-1 score, or None when no LLM judged the article

The score and the flag are one judgment: a score below
``RELEVANCY_MIN_CONFIDENCE`` demotes the article to irrelevant here in code,
whatever the model answered for ``is_relevant``. This is the only place
relevancy is judged — the tagging agent no longer scores it, so the tagger runs
on articles that already passed this gate.

Relevant articles flow on to tagging; irrelevant ones are filtered out. The agent
fails OPEN: on any LLM error every article is kept (``is_relevant=True``) so
nothing is ever silently dropped.

A project with no ``relevancy_prompt`` is scored against ``_DEFAULT_CRITERIA``
rather than skipped, so every article gets a verdict and a score.

The system prompt lives in ``relevancy_prompt.txt`` beside this module; the
project's criteria are substituted into it last, so criteria text containing a
``{{...}}`` placeholder is never itself expanded.

One rule is NOT left to the model. The project's include / exclude publication
domains are matched in code before any LLM call — an exact list is not a
judgment call, and because the gate fails open a model error would otherwise let
a blocked domain through. Everything else stays with the LLM.

Those lists come from the project's ``relevancy_domains`` column, extracted by
``relevancy_domain_extractor`` when the relevancy prompt is saved, rather than
being re-parsed out of the criteria text on every run. A project whose prompt
predates that column has no lists and is judged by the LLM alone.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any

# from ai_helpers.memory_retriever import append_examples
from configs import envs, logger
from file_helpers.cleaing_data import get_domain

# if TYPE_CHECKING:
#     from ai_helpers.memory_retriever import ArticleMemory

_PROMPT_TEMPLATE = (Path(__file__).parent / "relevancy_prompt.txt").read_text(encoding="utf-8")

# Criteria used when a project has configured none of its own. This is the
# judgment the tagging agent used to make on its own, kept here so an
# unconfigured project still gets a relevancy score instead of no verdict.
# Deliberately generic: with no client rules to apply, the only thing that can be
# judged is proximity to the brand, its competitors and their shared industry.
_DEFAULT_CRITERIA = """No client-specific criteria have been configured for this project, so judge
relevancy on proximity to the brand of interest, its competitors, and the broader industry they operate in.

Include:
- Articles substantively about the brand of interest, its products, executives or partners.
- Articles substantively about a competitor listed above.
- Articles about the industry the brand and its competitors operate in — market trends, regulation,
  supply chain, or comparable developments at peer companies.

Exclude:
- Articles that mention the brand, a competitor or the industry only in passing, with no substantive
  connection — a ticker list, a sidebar, a related-links block, or boilerplate.
- Articles about an unrelated topic, or about a different company or person that merely shares a name
  with the brand or a competitor.
"""

_CLAUDE_ALIASES = {"claude", "anthropic"}
_AZURE_ALIASES = {"azure_openai", "azure-openai", "azure", "openai", "gpt", "gpt-azure"}

# Cap the body text we send per article so a large batch can't blow the context.
_MAX_BODY_CHARS = 2000

# A bare hostname: two or more dot-separated labels, nothing else.
_HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")

_TOOL_NAME = "record_relevancy"
_TOOL_DESCRIPTION = "Record the relevancy decision (is_relevant + reason) for each article in the batch."
_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "description": "One entry per input article, in the same order, using the exact id.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "The article id, copied exactly (e.g. A1).",
                    },
                    "is_relevant": {
                        "type": "boolean",
                        "description": "True if the article fits the relevancy criteria, else False.",
                    },
                    "relevancy_confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "How relevant the article is to the brand, its competitors or their "
                            "industry (1 = squarely about them, 0 = not at all). Must agree with "
                            "is_relevant: below the threshold when false, at or above it when true."
                        ),
                    },
                    "relevancy_reason": {
                        "type": "string",
                        "description": (
                            "1-2 short sentences explaining the score and the decision. Required for "
                            "every article: when not relevant, cite the exclusion or the lack of fit; "
                            "when relevant, say what connects it to the brand/competitors/industry."
                        ),
                    },
                },
                "required": ["id", "is_relevant", "relevancy_confidence", "relevancy_reason"],
            },
        }
    },
    "required": ["results"],
}


def _normalize_domain(raw: Any) -> str:
    """Reduce a blocklist entry or an article's source to a bare lowercase hostname.

    Args:
        raw: A domain, URL or blocklist entry.

    Returns:
        The hostname without scheme, `www.` or path, or "" if it isn't one.
    """
    s = str(raw or "").strip().strip("`'\"<>").lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    # A hostname can't contain a comma, so `indiasnews,net` is a typo for a dot.
    s = s.replace(",", ".").strip(".")
    if s.startswith("www."):
        s = s[4:]
    return s if _HOSTNAME.fullmatch(s) else ""


def _domain_set(relevancy_domains: dict[str, Any] | None, key: str) -> set[str]:
    """Read one normalized hostname set out of a project's ``relevancy_domains``.

    Args:
        relevancy_domains: The stored ``{"include": [...], "exclude": [...]}``.
        key: Which list to read — "include" or "exclude".

    Returns:
        Normalized hostnames. Empty when the column is unset or holds no usable
        entry, which disables that side of the filter.
    """
    if not isinstance(relevancy_domains, dict):
        return set()
    raw = relevancy_domains.get(key)
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {host for host in (_normalize_domain(entry) for entry in raw) if host}


def _matched_domain(article: dict[str, Any], hosts: set[str]) -> str:
    """The listed hostname an article's source matches, if any.

    Args:
        article: A cleaned article dict (``domain`` / ``source`` / ``url``).
        hosts: Normalized hostnames from :func:`_domain_set`.

    Returns:
        The matching entry, or "" when the article matches none of them.
    """
    for key in ("domain", "source", "url"):
        host = _normalize_domain(article.get(key))
        if not host:
            continue
        # A listed domain covers its subdomains: finance.yahoo.com → yahoo.com.
        # Stop before the last label so a bare TLD is never tested.
        labels = host.split(".")
        for i in range(len(labels) - 1):
            candidate = ".".join(labels[i:])
            if candidate in hosts:
                return candidate
    return ""


def _system_prompt(
    brand: str,
    brand_keywords: list[str],
    competitor_keywords: list[str],
    criteria: str,
) -> str:
    brands = ", ".join(b for b in (brand_keywords or []) if str(b).strip()) or brand
    comps = ", ".join(c for c in (competitor_keywords or []) if str(c).strip()) or "(none specified)"
    # .replace, not .format — the criteria are free text and routinely contain braces.
    return (
        _PROMPT_TEMPLATE
        .replace("{{BRAND}}", brand)
        .replace("{{BRAND_KEYWORDS}}", brands)
        .replace("{{COMPETITORS}}", comps)
        .replace("{{THRESHOLD}}", f"{envs.RELEVANCY_MIN_CONFIDENCE:g}")
        .replace("{{CRITERIA}}", criteria)
    )


def _build_message(articles: list[dict[str, Any]], examples: str = "") -> str:
    lines: list[str] = [
        f"Assess the relevancy of the following {len(articles)} article(s). "
        "Return exactly one entry per article, using the id verbatim."
    ]
    for a in articles:
        lines.append("")
        lines.append(f"--- {a.get('id')} ---")

        # Domain
        domain = str(a.get("domain") or a.get("source") or "").strip()
        if domain:
            lines.append(f"Domain: {domain}")
        else:
            url = str(a.get("url") or "").strip()
            domain = get_domain(url)
            lines.append(f"Domain: {domain}")

        # Publication
        publication = str(a.get("domain_name") or "").strip()
        if publication and publication.lower() != domain.lower():
            lines.append(f"Publication: {publication}")

        # Query for which the article was fetched, if any
        query = str(a.get("query") or "").strip()
        if query:
            lines.append(f"Fetched for query: {query}")

        title = str(a.get("title") or "").strip()
        if title:
            lines.append(f"Title: {title}")
        body = str(a.get("article_text") or a.get("content") or "").strip()
        body = body[:_MAX_BODY_CHARS]
        if body and body.lower() != title.lower():
            lines.append(f"Body: {body}")
    # append_examples(
    #     lines,
    #     examples,
    #     "articles this client accepted before; calibration only — the criteria decide",
    # )
    return "\n".join(lines)


def _call_claude(system_prompt: str, user_msg: str) -> list[dict[str, Any]]:
    from anthropic import Anthropic

    if not envs.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    client = Anthropic(api_key=envs.ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=envs.CLAUDE_MODEL,
        max_tokens=envs.MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
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
            return list(dict(block.input).get("results", []))
    raise ValueError(f"Claude did not return a {_TOOL_NAME} tool call")


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
        max_tokens=envs.MAX_OUTPUT_TOKENS,
        messages=[
            {"role": "system", "content": system_prompt},
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
        raise ValueError("Azure OpenAI returned no choices for relevancy")
    for call in getattr(completion.choices[0].message, "tool_calls", None) or []:
        if call.function.name == _TOOL_NAME:
            payload = json.loads(call.function.arguments or "{}")
            return list(payload.get("results", []))
    raise ValueError(f"Azure OpenAI did not call {_TOOL_NAME}")


def _coerce_confidence(raw: Any) -> float | None:
    """Read the model's relevancy score, tolerating a 0-100 answer.

    Args:
        raw: The `relevancy_confidence` value from the tool call.

    Returns:
        A 0-1 float, or None when the value isn't a usable number.
    """
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return None
    if score != score or score in (float("inf"), float("-inf")):
        return None
    # The prompt asks for 0-1, but models occasionally answer on a 0-100 scale.
    # Only rescale what actually looks like a percentage: 2.5 is out of range
    # either way, and dividing it would invent a confident-looking 0.025.
    if score > 1:
        score = score / 100 if score <= 100 else 1.0
    return min(1.0, max(0.0, score))


def _relevancy_batch(
    articles: list[dict[str, Any]],
    system_prompt: str,
    memory: None #"ArticleMemory | None" = None,
) -> dict[str, tuple[bool, str, float | None]]:
    """Classify one batch. Returns ``{id: (is_relevant, relevancy_reason, confidence)}``.

    Fails OPEN: on any error, or for ids the model omits, the article is kept
    (``is_relevant=True``) so nothing is dropped because of an LLM hiccup. A
    fail-open article carries no score — None, not 0, which would read as
    "judged irrelevant" downstream.
    """
    ids = [a["id"] for a in articles]
    provider = envs.LLM_PROVIDER
    try:
        # Retrieval sits inside the try, so a memory failure lands on the same
        # fail-open path as an LLM error rather than dropping the batch.
        # examples = memory.examples_for(articles) if memory else ""
        examples = ""
        user_msg = _build_message(articles, examples)
        if provider in _AZURE_ALIASES:
            results = _call_azure(system_prompt, user_msg)
        elif provider in _CLAUDE_ALIASES:
            results = _call_claude(system_prompt, user_msg)
        else:
            raise RuntimeError(f"Unknown LLM_PROVIDER='{provider}'. Use 'claude' or 'azure_openai'.")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Relevancy batch failed for {len(articles)} article(s) — keeping all as relevant: {exc}")
        return {aid: (True, "", None) for aid in ids}

    threshold = envs.RELEVANCY_MIN_CONFIDENCE
    by_id: dict[str, tuple[bool, str, float | None]] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("id") or "").strip()
        if not rid:
            continue
        is_rel = bool(r.get("is_relevant", True))
        reason = str(r.get("relevancy_reason") or "").strip()
        score = _coerce_confidence(r.get("relevancy_confidence"))
        # The threshold is enforced here, not left to the model: a low score
        # demotes the article however the model answered is_relevant. A missing
        # score can't demote — we don't know what it would have been.
        if is_rel and score is not None and score < threshold:
            is_rel = False
            reason = reason or f"Relevancy score {score:.2f} is below the {threshold:g} threshold."
        by_id[rid] = (is_rel, reason, score)

    # Any id the model skipped defaults to relevant (fail-open).
    return {aid: by_id.get(aid, (True, "", None)) for aid in ids}


def apply_relevancy(
    articles: list[dict[str, Any]],
    brand_keywords: list[str],
    competitor_keywords: list[str],
    relevancy_prompt: str | None = None,
    relevancy_domains: dict[str, Any] | None = None,
    memory: None = None #"ArticleMemory | None" = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Annotate every article with ``is_relevant`` + ``relevancy_reason`` +
    ``relevancy_confidence`` and split them into ``(relevant, irrelevant)``.

    Criteria come from the project's ``relevancy_prompt``. When it's empty the
    generic ``_DEFAULT_CRITERIA`` are used instead, so every article still gets a
    score and a reason — the judgment the tagging agent used to make.

    Args:
        articles: Cleaned article dicts, annotated in place.
        brand_keywords: The project's brand names; the first is the brand label.
        competitor_keywords: The project's competitor names.
        relevancy_prompt: Free-text criteria, or None for the defaults.
        relevancy_domains: The project's stored ``{"include": [...],
            "exclude": [...]}`` hostnames, extracted when the prompt was saved.
            Applied in code before the LLM. A missing/empty list disables that
            side of the filter.
        memory: Approved-article memory for few-shot examples, or None.

    Returns:
        ``(relevant, irrelevant)`` — the same dicts, partitioned.
    """
    if not articles:
        return [], []

    brand = (brand_keywords[0].strip() if brand_keywords else "") or "Brand"
    criteria = (relevancy_prompt or "").strip()

    if not criteria:
        criteria = _DEFAULT_CRITERIA
        logger.info(
            f"No relevancy prompt configured for '{brand}'; scoring "
            f"{len(articles)} article(s) against the default criteria."
        )

    system_prompt = _system_prompt(brand, brand_keywords or [], competitor_keywords or [], criteria)

    # Deterministic source filtering, ahead of everything else — including the
    # Subscription carve-out below, since a blocked domain is blocked whether or
    # not its body could be fetched. The lists come from the project's
    # relevancy_domains column, extracted once when the prompt was saved.
    blocked = _domain_set(relevancy_domains, "exclude")
    allowed = _domain_set(relevancy_domains, "include")
    prefiltered: dict[str, tuple[bool, str, float | None]] = {}
    remaining: list[dict[str, Any]] = []
    dropped_not_allowed = 0
    for a in articles:
        entry = _matched_domain(a, blocked) if blocked else ""
        if entry:
            # A blocklist hit is certain, not a judgment — score it 0.
            prefiltered[a["id"]] = (False, f"Source '{entry}' is on the criteria's excluded publications list.", 0.0)
            continue
        # An include list, when the criteria name one, is an allowlist: a source
        # that isn't on it is out. An empty list means "no restriction", never
        # "allow nothing" — that would drop everything the moment extraction
        # returned no includes.
        if allowed and not _matched_domain(a, allowed):
            prefiltered[a["id"]] = (
                False,
                "Source is not on the criteria's included publications list.",
                0.0,
            )
            dropped_not_allowed += 1
            continue
        remaining.append(a)
    if blocked or allowed:
        logger.info(
            f"Relevancy [{brand}]: {len(blocked)} excluded / {len(allowed)} included domain(s) "
            f"configured; {len(prefiltered) - dropped_not_allowed} article(s) dropped as excluded, "
            f"{dropped_not_allowed} as not-included, before the LLM."
        )

    # Paywalled stubs whose body couldn't be fetched never reach the LLM — there is
    # no body for it to judge. They are kept, except when the query that fetched them
    # matched nothing in the title (see below).
    def _is_subscription_stub(a: dict[str, Any]) -> bool:
        flag = a.get("is_subscription")
        if flag is not None:
            return bool(flag)
        body = str(a.get("article_text") or a.get("content") or "").strip()
        return body.lower() == "subscription"

    to_classify: list[dict[str, Any]] = []
    kept_stubs = 0
    for a in remaining:
        if not _is_subscription_stub(a):
            to_classify.append(a)
            continue
        # Checking for query keywords matched for Subscription needed articles
        if str(a.get("query") or "").strip() and not (a.get("keyword_matched") or []):
            prefiltered[a["id"]] = (
                False,
                "Paywalled article whose body could not be fetched, and no query keyword appears in its title.",
                0.0,
            )
        else:
            kept_stubs += 1
    dropped_stubs = len(remaining) - len(to_classify) - kept_stubs
    if kept_stubs or dropped_stubs:
        logger.info(
            f"Relevancy [{brand}]: {kept_stubs} 'Subscription' article(s) auto-marked relevant, "
            f"{dropped_stubs} dropped for matching no query keyword; skipping LLM for both."
        )

    batch_size = max(1, envs.LLM_BATCH_SIZE)
    chunks = [to_classify[i:i + batch_size] for i in range(0, len(to_classify), batch_size)]

    def run(chunk: list[dict[str, Any]]) -> dict[str, tuple[bool, str]]:
        return _relevancy_batch(chunk, system_prompt, memory)

    if len(chunks) <= 1 or envs.LLM_CONCURRENCY <= 1:
        maps = [run(c) for c in chunks]
    else:
        with ThreadPoolExecutor(max_workers=min(envs.LLM_CONCURRENCY, len(chunks))) as pool:
            maps = list(pool.map(run, chunks))

    verdict: dict[str, tuple[bool, str, float | None]] = dict(prefiltered)
    for m in maps:
        verdict.update(m)

    relevant: list[dict[str, Any]] = []
    irrelevant: list[dict[str, Any]] = []
    for a in articles:
        is_rel, reason, score = verdict.get(a["id"], (True, "", None))
        a["is_relevant"] = is_rel
        # Kept for relevant articles too — it now explains the score, not just a drop.
        a["relevancy_reason"] = reason
        a["relevancy_confidence"] = score
        (relevant if is_rel else irrelevant).append(a)

    logger.info(
        f"Relevancy [{brand}]: {len(relevant)} relevant, {len(irrelevant)} irrelevant "
        f"of {len(articles)} article(s)."
    )
    return relevant, irrelevant
