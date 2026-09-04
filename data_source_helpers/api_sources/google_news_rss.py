from typing import Any

import feedparser
from urllib.parse import quote
from configs import logger, envs
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as FuturesTimeoutError,
)
from data_source_helpers.newspaper_helper import article_content_fetch
from data_source_helpers.scrapper_utils import (
    filter_recent_articles,
    find_matched_keywords,
)
from data_source_helpers.url_decoder import decode_google_news_url, request_with_retry

# Every wait in this module is bounded, because this is the scheduler's fetch path: a
# single un-timed socket here used to hang the worker thread forever, which left the
# query's id in the scheduler's overlap guard and silently skipped every later hourly
# slot for it. Worst case per stage is (workers, items) dependent; these deadlines are
# set well above a healthy run and well below the scheduler's own run deadline
# (envs.SCHEDULER_RUN_TIMEOUT_SECONDS).
_FEED_TIMEOUT = (5, 15)        # (connect, read) for the RSS document itself
_DECODE_TIMEOUT = 240          # whole url-decode stage for one query
_CONTENT_TIMEOUT = 600         # whole body-download stage for one query
_QUERY_TIMEOUT = 1200          # all queries of one fetch


def _parse_feed(url: str):
    """Fetch an RSS document with a deadline, then hand the bytes to feedparser.

    Mirrors :func:`data_source_helpers.beone_fetcher._parse_feed`. Deliberately no
    ``feedparser.parse(url)`` fallback: letting feedparser fetch means urllib with the
    socket default timeout, which is no timeout at all.
    """
    res = request_with_retry(
        "get",
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AI-Solutions news fetcher)"},
        timeout=_FEED_TIMEOUT,
        allow_redirects=True,
    )
    return feedparser.parse(res.content)


def _collect(fn, items, *, max_workers, timeout, stage):
    """Map `fn` over `items` in a thread pool, returning whatever finished in `timeout`.

    Not ``with ThreadPoolExecutor(...)``: its ``__exit__`` calls ``shutdown(wait=True)``,
    so one worker stuck on a socket blocked the entire fetch — the exact failure this
    module now guards against. Stragglers are abandoned (their own request timeout ends
    them) and counted in the log, never dropped silently.
    """
    if not items:
        return []
    pool = ThreadPoolExecutor(max_workers=min(max_workers, len(items)))
    try:
        futures = [pool.submit(fn, item) for item in items]
        out = []
        try:
            for fut in as_completed(futures, timeout=timeout):
                try:
                    out.append(fut.result())
                except Exception:
                    logger.exception(f"GoogleNewsRSS: {stage} worker failed")
        except FuturesTimeoutError:
            logger.warning(
                f"GoogleNewsRSS: {stage} hit its {timeout}s deadline; continuing with "
                f"{len(out)} of {len(items)} result(s)"
            )
        return out
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


class GoogleNewsRssScraper:
    def __init__(self):
        self.rss_url = "https://news.google.com/rss/search"

    def safe_url_decode(self, entry):
        """Decode one entry, returning a result dict instead of raising.

        On failure the entry is KEPT with its undecoded news.google.com link, and
        `error` records why — see the aggregate log and `decode_error` in
        :meth:`call_feedparser_google_news_rss_query`.
        """
        try:
            return {"title": entry.title, "published": entry.published, "url": decode_google_news_url(entry.link)}
        except Exception as e:
            logger.debug(f"GoogleNewsRSS: url decode failed for {entry.link[:80]}: {e}")
            return {"title": entry.title, "published": entry.published, "url": entry.link, "error": str(e)}

    def to_source_record(self, article: dict[str, Any]) -> dict[str, Any]:
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
            "data_source": "Google News",
            "media_type": "News"
        }

    def call_feedparser_google_news_rss_query(
        self,
        full_query: str,
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
        skip_url=None,
    ):
        """Fetch and parse Google News RSS results for a full (boolean) query string,
        returning a list of article dicts. Returns [] on failure.

        `recency_hours` scopes both the Google News `when:` filter and the
        post-parse recency check, so an hourly top-up asks for an hour rather than
        re-walking days of results.

        `skip_url(url) -> bool` (optional) drops an entry after its real URL is
        decoded but *before* its body is downloaded. Downloading the body is by far
        the expensive part, so this is what makes re-fetching the same query every
        hour cheap: an article the caller already stores costs one decode, not a
        scrape.
        """
        try:
            final_query = f"when:{recency_hours}h {full_query}"
            url = f"{self.rss_url}?q={quote(final_query)}&hl=en-US&gl=US&ceid=US:en"
            feed = _parse_feed(url)

            results = _collect(
                self.safe_url_decode,
                list(feed.entries),
                max_workers=10,
                timeout=_DECODE_TIMEOUT,
                stage=f"url decode for {full_query!r}",
            )

            # Count Undecoded entries carry a news.google.com url
            failed = [r for r in results if r.get("error")]
            if failed:
                logger.warning(
                    f"GoogleNewsRSS: {len(failed)} of {len(results)} url decode(s) failed for "
                    f"{full_query!r}; keeping the undecoded news.google.com link. "
                    f"First error: {failed[0]['error']}"
                )

            if skip_url is not None:
                keep = []
                for entry in results:
                    try:
                        if skip_url(entry.get("url")):
                            continue
                    except Exception:
                        logger.exception("skip_url predicate failed; keeping the article")
                    keep.append(entry)
                if len(keep) != len(results):
                    logger.info(
                        f"GoogleNewsRSS: skipping {len(results) - len(keep)} already-stored "
                        f"article(s) before content fetch for {full_query!r}"
                    )
                results = keep

            # article_content_fetch rebuilds the dict from a fixed key set, so the
            # decode failure has to be re-attached or the reason never reaches the DB
            # (which is why undecoded rows there had no diagnosable cause).
            def fetch_content(entry):
                article = article_content_fetch(entry)
                if entry.get("error"):
                    article["decode_error"] = entry["error"]
                return article

            articles = _collect(
                fetch_content,
                results,
                max_workers=10,
                timeout=_CONTENT_TIMEOUT,
                stage=f"content fetch for {full_query!r}",
            )
            return filter_recent_articles(articles, recency_hours)
        except Exception as e:
            logger.exception(f"Feedparser fetch failed for query {full_query!r}: {e}")
            return []

    def fetch_google_news_feedparser_boolean_query(
        self,
        queries: list[dict[str, str]] | list[str],
        language: str = "en",
        country: str = "us",
        on_progress=None,
        recency_hours: int = envs.DEFAULT_RSS_RECENCY_HOURS,
        skip_url=None,
    ):
        """
        Fetch the articles from Google News RSS using Feedparser, given a boolean query.

        1. Call the feedparser for each boolean query
        2. return final list (deduped by url), tagged with query/group

        `on_progress(count)` (optional) is called with the cumulative deduped article
        count after each query's results are merged, so callers can stream a live
        "N fetched" counter. Called from the merge loop (single thread) — safe.

        `recency_hours` and `skip_url` are passed through per query — see
        :meth:`call_feedparser_google_news_rss_query`. `skip_url` is called from
        worker threads, so it must be safe to call concurrently.
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

            # 2. Fetch articles for each unique boolean query in parallel, then mergethe results in this thread
            # (dedup by url stays single-threaded & safe).
            if query_meta:
                max_workers = min(10, len(query_meta))
                pool = ThreadPoolExecutor(max_workers=max_workers)
                try:
                    futures = {
                        pool.submit(
                            self.call_feedparser_google_news_rss_query, q, recency_hours, skip_url
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
                                logger.exception(f"GoogleNewsRSS: fetch failed for {query}: {e}")
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
                            logger.info(f"GoogleNewsRSS: articles fetched for {query}")
                            if on_progress is not None:
                                try:
                                    on_progress(len(articles))
                                except Exception:
                                    logger.exception("GoogleNewsRSS: on_progress callback failed")
                    except FuturesTimeoutError:
                        logger.warning(
                            f"GoogleNewsRSS: query fan-out hit its {_QUERY_TIMEOUT}s deadline; "
                            f"continuing with {done} of {len(query_meta)} query/queries"
                        )
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.exception(f"GoogleNewsRSS fetch_google_news_feedparser_boolean_query failed: {e}")

        mapped_articles = [self.to_source_record(a) for a in articles]
        return mapped_articles


google_news_rss_scraper = GoogleNewsRssScraper()