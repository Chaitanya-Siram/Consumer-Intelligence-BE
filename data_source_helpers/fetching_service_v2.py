from __future__ import annotations
import re
import threading
from typing import Any
from sqlalchemy.orm import Session
from configs import logger
from data_source_helpers.feedparser_helper import fetch_google_news_feedparser_boolean_query
from db_helpers.models.data_providers_model import DataProvidersAPIKeyModel
from db_helpers.models.session_model import SessionModel
from db_helpers.repository.sessions_db import update_session_source_file
from db_helpers.repository.raw_articles_db import add_new_raw_articles, get_raw_articles
from data_source_helpers.google_news_rss import google_news_rss_scraper
from data_source_helpers.tavily_helper import tavily_api_helper
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, urljoin


GOOGLE_NEWS_SOURCE = "google_news"


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

    def fetch_and_merge_workflow_rss(self, session: SessionModel, db: Session, data_providers_key: dict) -> SessionModel:
        """Materialize the workflow Data node's Google News RSS request into the
        session's source file.

        When the Data node asks for `google_news` + queries: fetch and clean those
        articles, then either MERGE them into the records of the already-uploaded
        source file, or — when no file was uploaded — save them as a NEW source file.
        Either way the session's `source_file` ends up pointing at a single JSON file
        that the tagging pipeline reads unchanged.

        No-ops (returns the session untouched) when the Data node has no RSS request,
        when nothing could be fetched, or when a prior run already materialized it.
        """
        queries = self.extract_workflow_rss_queries(session)
        if not queries:
            return session, False

        logger.info(f"Workflow RSS fetch for session id={session.id}: {len(queries)} query/queries")
        rss_articles = google_news_rss_scraper.fetch_google_news_feedparser_boolean_query(queries)
        if not rss_articles:
            logger.warning(
                f"No RSS articles fetched for session id={session.id}; leaving source file unchanged."
            )
            return session, False
        rss_records = [self.to_source_record(a, "Google News") for a in rss_articles]

        # Taviliy Fetcher
        if data_providers_key.get("tavily"):
            tavily_api = data_providers_key.get("tavily")
            tavily_articles = tavily_api_helper.fetch_tavily_articles(tavily_api, queries)

        # Merge into the uploaded file's records when one is present; otherwise the
        # RSS records stand alone as a brand-new source file.
        base_records: list[dict[str, Any]] = []

        merged_records = base_records + rss_records

        # Store the fetched/merged records in the database (raw_articles) instead of a
        # JSON source file on S3. A fresh source invalidates any prior tagged rows.
        # replace_raw_articles(db, session.id, merged_records)
        # delete_tagged_articles(db, session.id)
        updated = update_session_source_file(db, session, None)
        logger.info(
            f"Saved {len(merged_records)} record(s) "
            f"({len(base_records)} file + {len(rss_records)} RSS) "
            f"for session id={session.id}"
        )
        return updated, True

    def fetch_and_merge_articles(
        self,
        session: SessionModel,
        db: Session,
        data_providers_key: dict,
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

        # Run the three source families concurrently.
        with ThreadPoolExecutor(max_workers=3) as pool_exec:
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

            # Taviliy Fetcher
            if data_providers_key.get("tavily") and "tavily" in selected_data_providers:
                tavily_api_key = data_providers_key.get("tavily")
                tavily_future = pool_exec.submit(
                    tavily_api_helper.fetch_tavily_articles,
                    tavily_api_key,
                    queries,
                    on_progress=lambda n: _report("tavily", n)
                )
                futures.append(("tavily", tavily_future))

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
        update_session_source_file(db, session, None)
        return get_raw_articles(db, session.id)


fetching_service = FetchingService()