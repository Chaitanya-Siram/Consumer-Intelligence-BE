from __future__ import annotations
import re
import threading
from typing import Any
from sqlalchemy.orm import Session
from configs import logger
from data_source_helpers.api_sources.phyllo_insight_helper import phyllo_insight_api_helper
from db_helpers.models.session_model import SessionModel
from db_helpers.repository.raw_articles_db import add_new_raw_articles, get_raw_articles
from data_source_helpers.api_sources.google_news_rss import google_news_rss_scraper
from data_source_helpers.api_sources.tavily_helper import tavily_api_helper
from data_source_helpers.api_sources.serp_api_helper import serp_api_helper
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin


GOOGLE_NEWS_SOURCE = "google_news"


def _credentials(data_providers_key: dict, provider: str) -> dict[str, str | None]:
    """One provider's credentials from the org's credential map.

    Args:
        data_providers_key: Provider name/label -> credentials dict.
        provider: Provider label to look up.

    Returns:
        Dict with api_key, username and password; empty when unconfigured.
    """
    credentials = data_providers_key.get(provider)
    return credentials if isinstance(credentials, dict) else {}


class FetchingService():
    def __init__(self):
        self.UTM_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gclid", "fbclid", "_hsenc", "_hsmi"}

    def extract_workflow_rss_queries(self, session: SessionModel) -> list[str]:
        """Pull the queries configured on the workflow's api Data nodes.

        Returns the (trimmed, de-duped) query strings across every data node with
        sourceType "api" and at least one data source; otherwise [].
        """
        workflow = session.workflow if isinstance(session.workflow, dict) else {}
        nodes = workflow.get("nodes")
        if not isinstance(nodes, list):
            return []

        seen: set[str] = set()
        result: list[str] = []
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") != "data":
                continue
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            if data.get("sourceType") != "api":
                continue
            sources = data.get("data_sources") if isinstance(data.get("data_sources"), list) else []
            queries = data.get("queries") if isinstance(data.get("queries"), list) else []
            if not sources:
                continue

            for q in queries:
                q = str(q or "").strip()
                if q and q.lower() not in seen:
                    seen.add(q.lower())
                    result.append(q)

        return result

    def to_source_record(self, article: dict[str, Any], data_source: str = "Google News") -> dict[str, Any]:
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
            "data_source": data_source
        }

    def normalize_url(self, url: str) -> str:
        try:
            u = urlparse(url)
            host = (u.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            query = urlencode([(k, v) for k, v in parse_qsl(u.query) if k not in self.UTM_KEYS])
            path = u.path
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            netloc = host + (f":{u.port}" if u.port else "")
            return urlunparse((u.scheme, netloc, path, "", query, ""))
        except Exception:  # noqa: BLE001
            return url.strip().lower()

    def dedupe_articles(self, articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse the same article seen via RSS + Google News + scrape into one row,
        matching on normalized URL (title as fallback). Prefers a copy with a snippet
        and preserves a non-empty group/keyword_matched from whichever copy had it."""
        by_url: dict[str, dict[str, Any]] = {}
        by_title: dict[str, dict[str, Any]] = {}

        def _normalize_title(title: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()

        for a in articles:
            link = (a.get("url") or "").strip()
            title = (a.get("title") or "").strip()
            if not link or not title:
                continue
            norm_url = self.normalize_url(link)
            norm_title = _normalize_title(title)
            existing = by_url.get(norm_url) or by_title.get(norm_title)

            if existing is None:
                by_url[norm_url] = a
                by_title[norm_title] = a
                continue

            winner = existing
            # Prefer the copy that has content/snippet.
            if not existing.get("content") and a.get("content"):
                winner = a
            loser = a if winner is existing else existing
            # Preserve provenance: keep a group / keyword_matched if either copy had it.
            if not winner.get("group") and loser.get("group"):
                winner["group"] = loser["group"]
            if not winner.get("keyword_matched") and loser.get("keyword_matched"):
                winner["keyword_matched"] = loser["keyword_matched"]

            by_url[norm_url] = winner
            by_title[norm_title] = winner

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for a in by_url.values():
            k = self.normalize_url(a.get("url") or "")
            if k in seen:
                continue
            seen.add(k)
            out.append(a)
        return out

    def fetch_and_merge_articles(
        self,
        session: SessionModel,
        db: Session,
        data_providers_key: dict[str, dict[str, str | None]],
        on_progress=None,
    ):
        queries = self.extract_workflow_rss_queries(session)
        if not queries:
            return session, False

        selected_data_providers = []
        for item in session.workflow.get("nodes"):
            data = item.get("data", {}) if item.get("type") == "data" else {}
            if data.get("sourceType") == "api":
                selected_data_providers.extend(data.get("data_sources") or [])

        
        pool: list[dict[str, Any]] = []

        # Running per-source counts feeding the live "N fetched" counter. Google News
        # updates its slot from its own merge thread while this thread updates the
        # rss/scrape slots, hence the lock.
        counts: dict[str, int] = {}
        counts_lock = threading.Lock()

        def _report(tag: str, n: int) -> None:
            if on_progress is None:
                return
            with counts_lock:
                counts[tag] = n
                total = sum(counts.values())
            try:
                on_progress(total)
            except Exception:
                logger.exception("on_progress callback failed")

        # Run the source families concurrently — one worker per source, since each
        # blocks for minutes (Phyllo polls a job until it finishes).
        with ThreadPoolExecutor(max_workers=8) as pool_exec:
            futures = []
            # =====================================================
            # Google News Rss
            if "google_news" in selected_data_providers:
                gn_kwargs: dict[str, Any] = {
                    "language": "en",
                    "country": "us",
                    "on_progress": lambda n: _report("google_news", n),
                    "skip_url": None,
                }
                
                gn_rss_future = pool_exec.submit(
                    google_news_rss_scraper.fetch_google_news_feedparser_boolean_query,
                    queries,
                    **gn_kwargs,
                )

                futures = [("google_news_rss", gn_rss_future)]

            # =====================================================
            # Taviliy API Fetcher
            tavily_credentials = _credentials(data_providers_key, "tavily")
            if tavily_credentials.get("api_key") and "tavily" in selected_data_providers:
                tavily_future = pool_exec.submit(
                    tavily_api_helper.fetch_tavily_articles,
                    tavily_credentials["api_key"],
                    queries,
                    on_progress=lambda n: _report("tavily", n)
                )
                futures.append(("tavily", tavily_future))

            # =====================================================
            # Serp API Google Search Fetcher
            serp_search_credentials = _credentials(data_providers_key, "serp_google_search")
            if serp_search_credentials.get("api_key") and "serp_google_search" in selected_data_providers:
                serp_search_future = pool_exec.submit(
                    serp_api_helper.fetch_google_search_articles,
                    serp_search_credentials["api_key"],
                    queries,
                    on_progress=lambda n: _report("serp_google_search", n)
                )
                futures.append(("serp_google_search", serp_search_future))

            # =====================================================
            # Serp API Google News Fetcher
            serp_news_credentials = _credentials(data_providers_key, "serp_google_news")
            if serp_news_credentials.get("api_key") and "serp_google_news" in selected_data_providers:
                serp_news_future = pool_exec.submit(
                    serp_api_helper.fetch_google_news_articles,
                    serp_news_credentials["api_key"],
                    queries,
                    on_progress=lambda n: _report("serp_google_news", n)
                )
                futures.append(("serp_google_news", serp_news_future))

            # =====================================================
            # Phyllo Twitter Fetcher
            # Phyllo authenticates with Basic auth: client id / client secret.
            phyllo_credentials = _credentials(data_providers_key, "phyllo_twitter")
            if phyllo_credentials.get("username") and phyllo_credentials.get("password") and "phyllo_twitter" in selected_data_providers:
                phyllo_twitter_future = pool_exec.submit(
                    phyllo_insight_api_helper.fetch_phyllo_twitter_data,
                    phyllo_credentials["username"],
                    phyllo_credentials["password"],
                    queries,
                    on_progress=lambda n: _report("phyllo_twitter", n)
                )
                futures.append(("phyllo_twitter", phyllo_twitter_future))

            # =====================================================
            # Phyllo Instagram Fetcher
            phyllo_ig_credentials = _credentials(data_providers_key, "phyllo_instagram")
            if phyllo_ig_credentials.get("username") and phyllo_ig_credentials.get("password") and "phyllo_instagram" in selected_data_providers:
                phyllo_instagram_future = pool_exec.submit(
                    phyllo_insight_api_helper.fetch_phyllo_instagram_data,
                    phyllo_ig_credentials["username"],
                    phyllo_ig_credentials["password"],
                    queries,
                    on_progress=lambda n: _report("phyllo_instagram", n)
                )
                futures.append(("phyllo_instagram", phyllo_instagram_future))

            # =====================================================
            # Phyllo Reddit Fetcher
            phyllo_reddit_credentials = _credentials(data_providers_key, "phyllo_reddit")
            if phyllo_reddit_credentials.get("username") and phyllo_reddit_credentials.get("password") and "phyllo_reddit" in selected_data_providers:
                phyllo_reddit_future = pool_exec.submit(
                    phyllo_insight_api_helper.fetch_phyllo_reddit_data,
                    phyllo_reddit_credentials["username"],
                    phyllo_reddit_credentials["password"],
                    queries,
                    on_progress=lambda n: _report("phyllo_reddit", n)
                )
                futures.append(("phyllo_reddit", phyllo_reddit_future))

            # =====================================================
            # Phyllo YouTube Fetcher
            phyllo_youtube_credentials = _credentials(data_providers_key, "phyllo_youtube")
            if phyllo_youtube_credentials.get("username") and phyllo_youtube_credentials.get("password") and "phyllo_youtube" in selected_data_providers:
                phyllo_youtube_future = pool_exec.submit(
                    phyllo_insight_api_helper.fetch_phyllo_youtube_data,
                    phyllo_youtube_credentials["username"],
                    phyllo_youtube_credentials["password"],
                    queries,
                    on_progress=lambda n: _report("phyllo_youtube", n)
                )
                futures.append(("phyllo_youtube", phyllo_youtube_future))

            for tag, fut in futures:
                try:
                    articles = fut.result()
                    pool.extend(articles)
                    # if tag != "google_news_rss":  # google_news already reported per query
                    #     _report(tag, len(articles))
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"source {tag} failed: {exc}")

        deduped = self.dedupe_articles(pool)
        logger.info(f"Gathered {len(pool)} article(s) -> {len(deduped)} after dedupe")
        # Keep previously fetched articles; only articles this session hasn't seen
        # before (by hashed article_id) are added.
        added = add_new_raw_articles(db, session.project_id, session.id, deduped)
        logger.info(
            f"Added {len(added)} new article(s) for session id={session.id}; "
            f"{len(deduped) - len(added)} already stored"
        )
        return get_raw_articles(db, session.id)


fetching_service = FetchingService()