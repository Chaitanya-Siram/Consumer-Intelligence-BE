"""Fetch Google News articles via SerpAPI.

Wraps SerpAPI's `google_news` engine and normalises each result into the article
shape the rest of the pipeline expects (see file_helpers/cleaing_data.py):
    { "title", "content", "url", "source", "domain", "published_date", ... }

So results can flow straight into clean_articles → tagging → charts.

Env:
    SERP_API_KEY        — your SerpAPI key (required)

Requires the `serpapi` library:  pip install serpapi
"""
from __future__ import annotations

import re
from typing import Any, Optional
import serpapi
from configs import envs, logger
from file_helpers.cleaing_data import _to_iso_date

# SerpAPI google_news returns ~100 results per page; cap pages so a broad query
# can't spin forever.
_MAX_PAGES = 10


def _serp_search(params: dict[str, Any], api_key: str) -> dict[str, Any]:
    """Run one SerpAPI search via the `serpapi` library and return the result dict.

    Supports both the modern `serpapi.Client` SDK and the legacy `GoogleSearch`
    interface (both import as `serpapi`), so it works whichever is installed.
    """
    full = {**params, "api_key": api_key, "engine": "google_news"}
    if hasattr(serpapi, "Client"):                 # modern serpapi SDK
        return dict(serpapi.Client(api_key=api_key).search(full))
    if hasattr(serpapi, "GoogleSearch"):           # legacy google-search-results
        return serpapi.GoogleSearch(full).get_dict()
    raise RuntimeError("Unsupported 'serpapi' package — reinstall with: pip install serpapi")


def _domain(url: str) -> str:
    """example.com from https://www.example.com/path — mirrors cleaing_data.get_domain."""
    if not url:
        return ""
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()


def _flatten_source(source: Any) -> Any:
    """google_news `source` is usually a dict {name, icon, ...}; sometimes a string."""
    publication_source, author = "", ""
    if isinstance(source, dict):
        publication_source = str(source.get("name") or source.get("title") or "").strip()
        if "authors" in source and isinstance(source.get("authors"), list):
            author = ", ".join(source.get("authors", []))
    
    return str(publication_source or "").strip(), author.strip()


def _normalise(result: dict[str, Any], query: str) -> Optional[dict[str, Any]]:
    """Map one SerpAPI news_result onto the pipeline's article shape. Returns None
    for entries without a usable link/title."""
    link = str(result.get("link") or "").strip()
    title = str(result.get("title") or "").strip()
    if not link or not title:
        return None
    source, author = _flatten_source(result.get("source"))
    return {
        "title": title,
        # google_news has no full body — the snippet is the best available text.
        "content": str(result.get("snippet") or "").strip(),
        "url": link,
        "domain": _domain(link),
        "source": source,
        "date": _to_iso_date(result.get("date")),
        "thumbnail": result.get("thumbnail"),
        "author": author,
        "query": query,
    }


def _expand_stories(result: dict[str, Any]) -> list[dict[str, Any]]:
    """A news_result may be a single article or a grouped topic carrying a `stories`
    list (highlight / publications). Flatten either into individual article dicts."""
    if isinstance(result.get("stories"), list):
        return [s for s in result["stories"] if isinstance(s, dict)]
    if isinstance(result.get("highlight"), dict):
        items = [result["highlight"]]
        items += [s for s in result.get("publications", []) if isinstance(s, dict)]
        return items
    return [result]


def fetch_google_news(
    query: str,
    max_results: int = 100,
    language: str = "en",
    country: str = "us",
    when: Optional[str] = "7d",
    api_key: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch Google News articles for a single query.

    Args:
        query: the search query (boolean operators supported by Google News are fine).
        max_results: stop once this many articles are collected (paginates as needed).
        language / country: SerpAPI `hl` / `gl` codes (e.g. "en" / "us").
        when: optional recency window passed through as `qdr:` (e.g. "d" day, "w" week,
              "m" month, "y" year, or "7d").
        api_key: override the configured SERP_API_KEY.

    Returns:
        A list of normalised article dicts (deduped by URL). Empty on misconfig/error.
    """
    key = api_key or envs.SERP_API_KEY
    if not key:
        logger.warning("SERP_API_KEY is not set — cannot fetch Google News.")
        return []
    if not query or not query.strip():
        return []

    base_params: dict[str, Any] = {
        "engine": "google_news",
        "q": query.strip(),
        "hl": language,
        "gl": country,
    }
    if when:
        # SerpAPI accepts Google's `qdr:` recency operator via the `when` filter; pass
        # bare values like "7d" too.
        base_params["when"] = when

    articles: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 0

    while len(articles) < max_results and page < _MAX_PAGES:
        params = dict(base_params)
        if page:
            params["page"] = page  # google_news pagination is 0-based page index
        try:
            payload = _serp_search(params, key)
        except Exception as exc:  # noqa: BLE001 — library/network failure
            logger.warning(f"SerpAPI request failed for query={query!r}: {exc}")
            break
        if payload.get("error"):
            logger.warning(f"SerpAPI error for query={query!r}: {payload['error']}")
            break

        news_results = payload.get("news_results") or []
        if not news_results:
            break

        new_this_page = 0
        for result in news_results:
            for item in _expand_stories(result):
                article = _normalise(item, query)
                if article is None or article["url"] in seen:
                    continue
                seen.add(article["url"])
                articles.append(article)
                new_this_page += 1
                if len(articles) >= max_results:
                    break
            if len(articles) >= max_results:
                break

        # Stop if there's no next page or the page added nothing new.
        if not new_this_page or not payload.get("serpapi_pagination", {}).get("next"):
            break
        page += 1

    logger.info(f"SerpAPI fetched {len(articles)} Google News article(s) for query={query!r}")
    return articles[:max_results]


def fetch_google_news_for_queries(
    queries: list[dict[str, str]] | list[str],
    max_results_per_query: int = 100,
    language: str = "en",
    country: str = "us",
    when: Optional[str] = "7d",
) -> list[dict[str, Any]]:
    """Fetch and merge Google News articles for many queries, deduped across all of
    them by URL. Each article is tagged with its `query` (and `group` when the input
    items carry a "group" label).

    Args:
        queries: either a list of query strings, or a list of
                 {"group": <label>, "query": <query>} dicts.

    Returns:
        A combined, de-duplicated list of normalised article dicts.
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in queries:
        if isinstance(entry, dict):
            query = str(entry.get("query") or "").strip()
            group = entry.get("group")
        else:
            query = str(entry or "").strip()
            group = None
        if not query:
            continue

        for article in fetch_google_news(
            query,
            max_results=max_results_per_query,
            language=language,
            country=country,
            when=when,
        ):
            if article["url"] in seen:
                continue
            seen.add(article["url"])
            if group is not None:
                article["group"] = group
            merged.append(article)

    logger.info(f"SerpAPI fetched {len(merged)} unique article(s) across {len(queries)} query/queries")
    return merged
