"""Fetch articles for a query-defined session from SerpAPI and store them as a
single source file on S3 — so a query session feeds the same tagging → charts
pipeline as an uploaded file.

The session's `queries` column holds the query-builder's query groups
([{"label": ..., "queries": [...]}]). We flatten those, fetch Google News for every
query, merge + de-dup, then write ONE JSON file whose records are shaped so
file_parser.parse_upload keeps them (notably a canonical `date` field).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from sqlalchemy.orm import Session
from configs import logger
from data_source_helpers.feedparser_helper import fetch_google_news_feedparser
from data_source_helpers.serp_api_helper import fetch_google_news_for_queries
from db_helpers.models.session_model import SessionModel
from db_helpers.repository.sessions_db import update_session_source_file
from file_helpers.s3_file import s3_file


def _flatten_session_queries(queries: Any) -> list[dict[str, str]]:
    """Flatten the session's stored query groups into [{group, query}, ...].

    Tolerates the query-builder shape ([{"label", "queries": [...]}]) as well as a
    bare list of query strings.
    """
    flat: list[dict[str, str]] = []
    if not isinstance(queries, list):
        return flat
    for entry in queries:
        if isinstance(entry, dict) and isinstance(entry.get("queries"), list):
            label = str(entry.get("label") or "Queries")
            for q in entry["queries"]:
                q = str(q).strip()
                if q:
                    flat.append({"group": label, "query": q})
        elif isinstance(entry, str) and entry.strip():
            flat.append({"group": "Queries", "query": entry.strip()})
    return flat


def _to_source_record(article: dict[str, Any]) -> dict[str, Any]:
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
        "author": article.get("author", "")
    }


def fetch_articles_and_save(
    session: SessionModel,
    db: Session,
    max_results_per_query: int = 50,
    language: str = "en",
    country: str = "us",
    when: str = "7d",
) -> SessionModel:
    """Fetch articles from SerpAPI for all of the session's queries and save them as
    a single JSON source file on S3, then point the session's source_file at it.

    Returns the updated session. Raises ValueError if the session has no queries or
    no articles could be fetched.
    """
    flat = _flatten_session_queries(session.queries)
    if not flat:
        raise ValueError("Session has no queries to fetch articles for.")

    logger.info(f"Fetching SerpAPI articles for session id={session.id}: {len(flat)} query/queries")
    articles = fetch_google_news_feedparser(
        flat,
        max_results_per_query=max_results_per_query,
        language=language,
        country=country,
        when=when,
    )
    if not articles:
        raise ValueError("No articles were fetched from SerpAPI for the session's queries.")

    records = [_to_source_record(a) for a in articles]
    body = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")

    current_date = datetime.now().strftime("%Y-%m-%d")
    file_key = f"session_files/session_{session.id}/{current_date}/raw_fetched_articles_{int(time.time())}.json"
    s3_file.upload_file(file_key, body)

    updated = update_session_source_file(db, session, file_key)
    logger.info(f"Saved {len(records)} fetched article(s) to key='{file_key}' for session id={session.id}")
    return updated
