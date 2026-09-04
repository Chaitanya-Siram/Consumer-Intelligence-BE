from __future__ import annotations
import re
from datetime import datetime, timedelta, timezone
from typing import Any
import serpapi
from configs import logger
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FuturesTimeoutError,
)
from file_helpers.cleaing_data import _to_iso_date
from data_source_helpers.newspaper_helper import article_content_fetch
from data_source_helpers.scrapper_utils import find_matched_keywords

_QUERY_TIMEOUT = 1200
_CONTENT_TIMEOUT = 600         # whole body-download stage for one query
_MAX_RESULTS = 100
# Google Search returns ~10 organic results per page, so 100 results needs ~10 pages.
_PAGE_SIZE = 10
_MAX_PAGES = 10

# SerpAPI often returns relative dates ("11 hours ago", "2 days ago").
_RELATIVE_DATE_RE = re.compile(
    r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s*ago", re.IGNORECASE
)
_RELATIVE_UNITS = {
    "second": "seconds",
    "minute": "minutes",
    "hour": "hours",
    "day": "days",
    "week": "weeks",
    "month": "days",
    "year": "days",
}
_RELATIVE_SCALE = {"month": 30, "year": 365}


def _resolve_date(raw: Any) -> str:
    """Normalise a date, converting relative forms like "11 hours ago" to absolute.

    Args:
        raw: Date string from SerpAPI or a scraped article.

    Returns:
        ISO 8601 date string, or "" when there is nothing usable.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    match = _RELATIVE_DATE_RE.search(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        delta = timedelta(**{_RELATIVE_UNITS[unit]: amount * _RELATIVE_SCALE.get(unit, 1)})
        return (datetime.now(timezone.utc) - delta).isoformat(timespec="milliseconds")
    return _to_iso_date(text)


class SerpAPIHelper:
    def __init__(self):
        pass

    def _search(self, api_key: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one SerpAPI search and return the raw payload.

        Args:
            api_key: SerpAPI key.
            params: Engine-specific search params.

        Returns:
            Response dict, empty on failure.
        """
        try:
            full = {**params, "api_key": api_key}
            # The modern SDK exposes Client; older google-search-results exposes GoogleSearch.
            if hasattr(serpapi, "Client"):
                return dict(serpapi.Client(api_key=api_key).search(full))
            if hasattr(serpapi, "GoogleSearch"):
                return serpapi.GoogleSearch(full).get_dict()
            raise RuntimeError("Unsupported 'serpapi' package — install with: pip install serpapi")
        except Exception as e:
            logger.error(f"SerpAPI Error: search failed for {params.get('engine')}: {e}")
            return {}

    def _domain(self, url: str) -> str:
        """Extract the bare domain from a url.

        Args:
            url: Article url.

        Returns:
            Domain string without scheme or www.
        """
        if not url:
            return ""
        return re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()

    def _flatten_source(self, source: Any) -> tuple[str, str]:
        """Pull publication name and authors out of a google_news `source` field.

        Args:
            source: Either a dict {name, authors, ...} or a string.

        Returns:
            Tuple of (source name, comma-joined authors).
        """
        if isinstance(source, dict):
            name = str(source.get("name") or source.get("title") or "").strip()
            authors = source.get("authors")
            author = ", ".join(authors) if isinstance(authors, list) else ""
            return name, author.strip()
        return str(source or "").strip(), ""

    def _expand_stories(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten a grouped google_news topic into individual article dicts.

        Args:
            result: One news_result entry.

        Returns:
            List of article-shaped dicts.
        """
        if isinstance(result.get("stories"), list):
            return [s for s in result["stories"] if isinstance(s, dict)]
        if isinstance(result.get("highlight"), dict):
            items = [result["highlight"]]
            items += [s for s in result.get("publications", []) if isinstance(s, dict)]
            return items
        return [result]

    def _enrich_article(self, article: dict[str, Any]) -> dict[str, Any]:
        """Scrape the article url for full content and a real publish date.

        Falls back to the SerpAPI snippet when no body is scraped, and to the
        SerpAPI date (relative forms resolved) when no date is scraped.

        Args:
            article: Normalised article dict carrying url/content/date.

        Returns:
            The article with content, date and author filled in where possible.
        """
        snippet = article.get("content") or ""
        serp_date = _resolve_date(article.get("date"))
        try:
            # article_content_fetch reads `published` and rebuilds from a fixed key set.
            fetched = article_content_fetch({**article, "published": article.get("date") or ""})
        except Exception:
            logger.exception(f"SerpAPI: content fetch failed for {article.get('url')}")
            fetched = {}

        content = str(fetched.get("content") or "").strip()
        # article_content_fetch returns "Subscription" as its failure placeholder.
        if not content or content == "Subscription":
            content = snippet
        article["content"] = content

        article["date"] = _resolve_date(fetched.get("date")) or serp_date
        if (not article.get("author") or article.get("author") == "") and fetched.get("author"):
            article["author"] = fetched["author"]
        return article

    def _enrich_articles(self, articles: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        """Scrape content/date for a query's articles in parallel, bounded by a deadline.

        Args:
            articles: Normalised article dicts for one query.
            query: The query they came from, for logging.

        Returns:
            Whatever finished inside the deadline; stragglers are dropped.
        """
        if not articles:
            return []
        pool = ThreadPoolExecutor(max_workers=min(10, len(articles)))
        try:
            futures = [pool.submit(self._enrich_article, a) for a in articles]
            out = []
            try:
                for fut in as_completed(futures, timeout=_CONTENT_TIMEOUT):
                    try:
                        out.append(fut.result())
                    except Exception:
                        logger.exception(f"SerpAPI: enrich worker failed for {query!r}")
            except FuturesTimeoutError:
                logger.warning(
                    f"SerpAPI: content fetch for {query!r} hit its {_CONTENT_TIMEOUT}s "
                    f"deadline; continuing with {len(out)} of {len(articles)} article(s)"
                )
            return out
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def to_source_record(self, article: dict[str, Any], data_source: str) -> dict[str, Any]:
        """Shape a SerpAPI article so parse_upload keeps it: it needs a non-empty
        canonical `date`, plus title/content/url. Carry the group through for slicing."""
        return {
            "title": article.get("title", ""),
            "content": article.get("content", ""),
            "url": article.get("url", ""),
            "source": article.get("source", ""),
            "domain": article.get("domain", ""),
            "date": article.get("date") or "",
            "group": article.get("group", ""),
            "query": article.get("query", ""),
            "author": article.get("author", ""),
            "keyword_matched": article.get("keyword_matched", []),
            "data_source": data_source,
            "media_type": "News"
        }

    def get_google_news_articles(
        self,
        api_key: str,
        query: str,
        language: str = "en",
        country: str = "us",
        when: str = "1d",
        max_results: int = _MAX_RESULTS,
    ) -> list[dict[str, Any]]:
        """Fetch Google News results for one query via SerpAPI.

        Args:
            api_key: SerpAPI key.
            query: Search query (boolean operators supported).
            language: SerpAPI `hl` code.
            country: SerpAPI `gl` code.
            when: Recency window (e.g. "1d", "7d").
            max_results: Stop after this many articles.

        Returns:
            List of normalised article dicts.
        """
        params: dict[str, Any] = {
            "engine": "google_news",
            "q": query,
            "hl": language,
            "gl": country,
        }
        if when:
            params["when"] = when

        payload = self._search(api_key, params)
        if payload.get("error"):
            logger.warning(f"SerpAPI google_news error for query={query!r}: {payload['error']}")
            return []

        articles = []
        for result in payload.get("news_results") or []:
            if not isinstance(result, dict):
                continue
            for item in self._expand_stories(result):
                link = str(item.get("link") or "").strip()
                title = str(item.get("title") or "").strip()
                if not link or not title:
                    continue
                source, author = self._flatten_source(item.get("source"))
                articles.append({
                    "title": title,
                    # google_news has no full body — the snippet is the best available text.
                    "content": str(item.get("snippet") or "").strip(),
                    "url": link,
                    "domain": self._domain(link),
                    "source": source,
                    "date": item.get("date"),
                    "author": author,
                })
                if len(articles) >= max_results:
                    return self._enrich_articles(articles, query)
        return self._enrich_articles(articles, query)

    def get_google_search_articles(
        self,
        api_key: str,
        query: str,
        language: str = "en",
        country: str = "us",
        when: str = "d1",
        max_results: int = _MAX_RESULTS,
    ) -> list[dict[str, Any]]:
        """Fetch Google Search organic results for one query via SerpAPI.

        Args:
            api_key: SerpAPI key.
            query: Search query (boolean operators supported).
            language: SerpAPI `hl` code.
            country: SerpAPI `gl` code.
            when: Google `tbs=qdr:` recency value (e.g. "d1", "w1").
            max_results: Stop after this many articles.

        Returns:
            List of normalised article dicts.
        """
        base_params: dict[str, Any] = {
            "engine": "google",
            "q": query,
            "hl": language,
            "gl": country,
            "num": _PAGE_SIZE,
        }
        if when:
            base_params["tbs"] = f"qdr:{when}"

        articles = []
        seen = set()
        for page in range(1, _MAX_PAGES+1):
            params = {**base_params, "start": page * _PAGE_SIZE}
            payload = self._search(api_key, params)
            if payload.get("error"):
                logger.warning(f"SerpAPI google error for query={query!r}: {payload['error']}")
                break

            organic_results = payload.get("organic_results") or []
            if not organic_results:
                break

            for result in organic_results:
                if not isinstance(result, dict):
                    continue
                link = str(result.get("link") or "").strip()
                title = str(result.get("title") or "").strip()
                if not link or not title or link in seen:
                    continue
                seen.add(link)
                articles.append({
                    "title": title,
                    "content": str(result.get("snippet") or "").strip(),
                    "url": link,
                    "domain": self._domain(link),
                    "source": str(result.get("source") or result.get("displayed_link") or "").strip(),
                    "date": result.get("date"),
                    "author": "",
                })
                if len(articles) >= max_results:
                    return self._enrich_articles(articles, query)

            # Stop when Google says there is no next page.
            if not (payload.get("serpapi_pagination") or {}).get("next"):
                break
        return self._enrich_articles(articles, query)

    def _fetch_for_queries(
        self,
        fetch_fn,
        api_key: str,
        queries: list[dict[str, str]] | list[str],
        data_source: str,
        language: str = "en",
        country: str = "us",
        on_progress=None,
    ) -> list[dict[str, Any]]:
        """Fan `fetch_fn` out over the queries and merge the results, deduped by url.

        Args:
            fetch_fn: Per-query fetcher (api_key, query, language, country).
            api_key: SerpAPI key.
            queries: Query strings or {"group", "query"} dicts.
            data_source: Label stamped on each record.
            language: SerpAPI `hl` code.
            country: SerpAPI `gl` code.
            on_progress: Optional callback receiving the running article count.

        Returns:
            List of source records.
        """
        articles = []
        seen_urls = set()
        try:
            # 1. Normalise the incoming queries into (query, group) pairs, deduping identical query strings while remembering their group.
            query_meta: dict[str, dict] = {}
            for entry in queries:
                if isinstance(entry, dict):
                    query = str(entry.get("query") or "").strip()
                    group = entry.get("group")
                else:
                    query = str(entry or "").strip()
                    group = None
                if not query or query in query_meta:
                    continue
                query_meta[query] = {"query": query, "group": group}

            # 2. Fetch articles for each unique boolean query in parallel, then merge the results in this thread
            # (dedup by url stays single-threaded & safe).
            if query_meta:
                max_workers = min(10, len(query_meta))
                pool = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    futures = {
                        pool.submit(fetch_fn, api_key, q, language, country): q
                        for q in query_meta
                    }
                    done = 0
                    try:
                        for fut in as_completed(futures, timeout=_QUERY_TIMEOUT):
                            done += 1
                            query = futures[fut]
                            meta = query_meta[query]
                            try:
                                fetched = fut.result()
                            except Exception as e:
                                logger.exception(f"SerpAPI ({data_source}): fetch failed for {query}: {e}")
                                continue
                            for art in fetched:
                                if not isinstance(art, dict):
                                    continue
                                url = art.get("url")
                                if url and url in seen_urls:
                                    continue
                                if url:
                                    seen_urls.add(url)
                                art.setdefault("query", meta["query"])
                                if meta["group"] is not None:
                                    art.setdefault("group", meta["group"])
                                # Split the boolean query into terms and record which ones were found in the article's title/content.
                                art["keyword_matched"] = find_matched_keywords(meta["query"], art.get("title"), art.get("content"))
                                articles.append(art)
                            logger.info(f"SerpAPI ({data_source}): articles fetched for {query}")
                            if on_progress is not None:
                                try:
                                    on_progress(len(articles))
                                except Exception:
                                    logger.exception(f"SerpAPI ({data_source}): on_progress callback failed")
                    except FuturesTimeoutError:
                        logger.warning(
                            f"SerpAPI ({data_source}): query fan-out hit its {_QUERY_TIMEOUT}s deadline; "
                            f"continuing with {done} of {len(query_meta)} query/queries"
                        )
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.exception(f"SerpAPI ({data_source}) fetch failed: {e}")

        return [self.to_source_record(a, data_source) for a in articles]

    def fetch_google_news_articles(
        self,
        api_key: str,
        queries: list[dict[str, str]] | list[str],
        language: str = "en",
        country: str = "us",
        on_progress=None,
    ) -> list[dict[str, Any]]:
        """Fetch and merge SerpAPI Google News articles for many queries.

        Args:
            api_key: SerpAPI key.
            queries: Query strings or {"group", "query"} dicts.
            language: SerpAPI `hl` code.
            country: SerpAPI `gl` code.
            on_progress: Optional callback receiving the running article count.

        Returns:
            List of source records.
        """
        return self._fetch_for_queries(
            self.get_google_news_articles,
            api_key,
            queries,
            "SerpAPI Google News",
            language=language,
            country=country,
            on_progress=on_progress,
        )

    def fetch_google_search_articles(
        self,
        api_key: str,
        queries: list[dict[str, str]] | list[str],
        language: str = "en",
        country: str = "us",
        on_progress=None,
    ) -> list[dict[str, Any]]:
        """Fetch and merge SerpAPI Google Search articles for many queries.

        Args:
            api_key: SerpAPI key.
            queries: Query strings or {"group", "query"} dicts.
            language: SerpAPI `hl` code.
            country: SerpAPI `gl` code.
            on_progress: Optional callback receiving the running article count.

        Returns:
            List of source records.
        """
        return self._fetch_for_queries(
            self.get_google_search_articles,
            api_key,
            queries,
            "SerpAPI Google Search",
            language=language,
            country=country,
            on_progress=on_progress,
        )


serp_api_helper = SerpAPIHelper()
