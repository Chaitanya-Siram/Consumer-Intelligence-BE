from datetime import datetime, timedelta
from typing import Any
from configs import logger, envs
from tavily import TavilyClient
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FuturesTimeoutError,
)
from data_source_helpers.scrapper_utils import (
    filter_recent_articles,
    find_matched_keywords,
)

_QUERY_TIMEOUT = 1200

class TavilyAPIHelper():
    def __init__(self):
        pass

    def to_source_record(self, article: dict[str, Any]) -> dict[str, Any]:
        """Shape a SerpAPI article so parse_upload keeps it: it needs a non-empty
        canonical `date`, plus title/content/url. Carry the group through for slicing."""
        return {
            "title": article.get("title", ""),
            "content": article.get("content", ""),
            "url": article.get("url", ""),
            "source": article.get("source", ""),
            "domain": article.get("domain", ""),
            "date": article.get("published_date") or "",
            "group": article.get("group", ""),
            "query": article.get("query", ""),
            "author": article.get("author", ""),
            "data_source": "Tavily",
            "media_type": "News"
        }

    def get_tavily_articles(self, api_key: str, query: str):
        try:
            start_date = datetime.now() - timedelta(hours=24)
            end_date = datetime.now()

            client = TavilyClient(api_key=api_key)
            response = client.search(
                query=query,
                topic="news",
                search_depth="basic",
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=end_date.strftime("%Y-%m-%d"),
                country="united states"
            )
            return response.get("results", [])
        except Exception as e:
            logger.error(f"Tavily Error: fetching articles from Tavily API: {e}")
            return []

    def fetch_tavily_articles(
        self,
        api_key: str,
        queries: list[dict[str, str]] | list[str],
        language: str = "en",
        country: str = "us",
        on_progress=None,
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
        skip_url=None,
    ):
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

            # 2. Fetch articles for each unique boolean query in parallel, then mergethe results in this thread
            # (dedup by url stays single-threaded & safe).
            if query_meta:
                max_workers = min(10, len(query_meta))
                pool = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    futures = {
                        pool.submit(
                            self.get_tavily_articles, api_key, q
                        ): q
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
                                logger.exception(f"TavilyAPI: fetch failed for {query}: {e}")
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
                            logger.info(f"TavilyAPI: articles fetched for {query}")
                            if on_progress is not None:
                                try:
                                    on_progress(len(articles))
                                except Exception:
                                    logger.exception("TavilyAPI: on_progress callback failed")
                    except FuturesTimeoutError:
                        logger.warning(
                            f"TavilyAPI: query fan-out hit its {_QUERY_TIMEOUT}s deadline; "
                            f"continuing with {done} of {len(query_meta)} query/queries"
                        )
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.exception(f"TavilyAPI fetch_tavily_articles_boolean_query failed: {e}")

        mapped_articles = [self.to_source_record(a) for a in articles]
        return mapped_articles


tavily_api_helper = TavilyAPIHelper()