from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Optional
import feedparser
from urllib.parse import quote
from configs import logger
from data_source_helpers.newspaper_helper import article_content_fetch
from data_source_helpers.url_decoder import decode_google_news_url


def safe_decode(entry):
    """Decode one entry, returning a result dict instead of raising."""
    try:
        return {"title": entry.title, "published": entry.published, "url": decode_google_news_url(entry.link)}
    except Exception as e:
        return {"title": entry.title, "published": entry.published, "url": entry.link, "error": str(e)}


def parse_boolean_query(query: str) -> list[str]:
    terms = []
    
    # 1. Extract quoted phrases first (e.g., "Bcl-2 inhibitor")
    quoted = re.findall(r'"([^"]+)"', query)
    terms.extend(quoted)
    
    # 2. Remove quoted phrases from query, then extract remaining single terms
    stripped = re.sub(r'"[^"]+"', '', query)
    
    # Match words including hyphens (e.g., BCL-2), skip boolean operators
    single_terms = re.findall(r'\b(?!(?:AND|OR|NOT)\b)[A-Za-z0-9][A-Za-z0-9\-]*', stripped)
    terms.extend(single_terms)
    
    # 3. Deduplicate while preserving order
    seen = set()
    result = []
    for t in terms:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            result.append(t)
    
    return result


def call_feedparser_google_news_rss(query_keyword):
    """Fetch and parse Google News RSS results for a single keyword, returning a
    list of article dicts. Returns [] on failure."""
    try:
        final_query = f"{query_keyword} when:1d"   # last 24 hours
        url = f"https://news.google.com/rss/search?q={quote(final_query)}" #&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)

        results = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(safe_decode, e) for e in feed.entries]
            for fut in as_completed(futures):
                results.append(fut.result())

        articles = []
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(article_content_fetch, a) for a in results]
            for fut in as_completed(futures):
                articles.append(fut.result())
        return articles
    except Exception as e:
        logger.exception(f"Feedparser fetch failed for keyword {query_keyword!r}: {e}")
        return []


def fetch_google_news_feedparser(
        queries: list[dict[str, str]] | list[str],
        max_results_per_query: int = 100,
        language: str = "en",
        country: str = "us",
        when: Optional[str] = "1d",
    ):
    """
    Fetch the articles from Google News RSS using Feedparser
    1. Parse Boolean Query in terms
    2. Call the feedparser for each keyword
    3. return final list (deduped by url), tagged with query/group
    """
    articles = []
    seen_urls = set()
    try:
        # 1. Collect the parsed keywords from every query into a deduped set,
        #    remembering the query/group each keyword first came from.
        terms_keywords: set[str] = set()
        term_meta: dict[str, dict] = {}

        for entry in queries:
            if isinstance(entry, dict):
                query = str(entry.get("query") or "").strip()
                group = entry.get("group")
            else:
                query = str(entry or "").strip()
                group = None
            if not query:
                continue

            for term in parse_boolean_query(query):
                if term in terms_keywords:
                    continue
                terms_keywords.add(term)
                term_meta[term] = {"query": query, "group": group}

        # 2. Fetch articles for each unique keyword in parallel, then merge the
        #    results in this thread (dedup by url stays single-threaded & safe).
        if terms_keywords:
            max_workers = min(10, len(terms_keywords))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(call_feedparser_google_news_rss, kw): kw
                    for kw in terms_keywords
                }
                for fut in as_completed(futures):
                    query_keyword = futures[fut]
                    meta = term_meta[query_keyword]
                    try:
                        fetched = fut.result()
                    except Exception as e:
                        logger.exception(f"FeedParser: fetch failed for {query_keyword}: {e}")
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
                        articles.append(art)
                    logger.info(f"FeedParser: articles fetched for {query_keyword}")
    except Exception as e:
        logger.exception(f"fetch_google_news_feedparser failed: {e}")
    return articles

