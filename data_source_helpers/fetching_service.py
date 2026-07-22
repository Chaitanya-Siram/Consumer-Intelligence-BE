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
from data_source_helpers.feedparser_helper import fetch_google_news_feedparser_boolean_query
from data_source_helpers.serp_api_helper import fetch_google_news_for_queries
from db_helpers.models.session_model import SessionModel
from db_helpers.repository.sessions_db import update_session_source_file
from file_helpers.file_parser import parse_upload
from file_helpers.s3_file import s3_file

# Google News is the only RSS source the workflow data node currently offers.
GOOGLE_NEWS_SOURCE = "google_news"
# Infix used in the key of source files we materialize from a workflow RSS fetch.
# Presence of this marker means the fetch already ran for this session, so a
# re-tag reads the merged file instead of fetching + merging again.
_RSS_MERGED_INFIX = "rss_merged"


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
    when: str = "1d",
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
    articles = fetch_google_news_feedparser_boolean_query(
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


def _extract_workflow_rss_queries(session: SessionModel) -> list[str]:
    """Pull the Google News RSS queries configured on the workflow's Data node.

    Returns the (trimmed, de-duped) query strings only when the Data node has
    `google_news` among its `api.sources` and at least one query; otherwise [].
    """
    workflow = session.workflow if isinstance(session.workflow, dict) else {}
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return []

    for node in nodes:
        if not isinstance(node, dict) or node.get("type") != "data":
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        api = data.get("api") if isinstance(data.get("api"), dict) else {}
        sources = api.get("sources") if isinstance(api.get("sources"), list) else []
        queries = api.get("queries") if isinstance(api.get("queries"), list) else []
        if GOOGLE_NEWS_SOURCE not in sources:
            return []

        seen: set[str] = set()
        result: list[str] = []
        for q in queries:
            q = str(q or "").strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                result.append(q)
        return result

    return []


def _rss_already_materialized(session: SessionModel) -> bool:
    """True if this session's source_file is one we previously built from an RSS
    fetch — so a re-tag reads it as-is instead of fetching + merging again."""
    key = session.source_file or ""
    return _RSS_MERGED_INFIX in key.rsplit("/", 1)[-1]


def fetch_and_merge_workflow_rss(
    session: SessionModel,
    db: Session,
) -> SessionModel:
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
    queries = _extract_workflow_rss_queries(session)
    if not queries:
        return session

    # if _rss_already_materialized(session):
    #     logger.info(
    #         f"Session id={session.id} already has an RSS-merged source file; skipping re-fetch."
    #     )
    #     return session

    logger.info(f"Workflow RSS fetch for session id={session.id}: {len(queries)} query/queries")
    rss_articles = fetch_google_news_feedparser_boolean_query(queries)
    if not rss_articles:
        logger.warning(
            f"No RSS articles fetched for session id={session.id}; leaving source file unchanged."
        )
        return session
    rss_records = [_to_source_record(a) for a in rss_articles]

    # Merge into the uploaded file's records when one is present; otherwise the
    # RSS records stand alone as a brand-new source file.
    base_records: list[dict[str, Any]] = []
    if session.source_file:
        try:
            existing = parse_upload(session.source_file, s3_file.download_file(session.source_file))
            if isinstance(existing, list):
                base_records = existing
        except Exception as e:
            logger.exception(
                f"Could not read existing source file {session.source_file!r} for session "
                f"id={session.id}; writing an RSS-only file instead: {e}"
            )

    merged_records = base_records + rss_records
    body = json.dumps(merged_records, ensure_ascii=False, indent=2, default=str).encode("utf-8")

    current_date = datetime.now().strftime("%Y-%m-%d")
    file_key = (
        f"session_files/session_{session.id}/{current_date}/"
        f"raw_{_RSS_MERGED_INFIX}_{int(time.time())}.json"
    )
    s3_file.upload_file(file_key, body)

    updated = update_session_source_file(db, session, file_key)
    logger.info(
        f"Saved {len(merged_records)} record(s) "
        f"({len(base_records)} file + {len(rss_records)} RSS) to key='{file_key}' "
        f"for session id={session.id}"
    )
    return updated
