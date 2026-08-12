"""LlamaIndex ingestion: tagged articles -> chunks -> embeddings -> pgvector.

Builds one Document per tagged article (title + body), chunks with SentenceSplitter
(kept under the BGE 512-token limit), embeds with the local BGE model, and inserts
into the hybrid PGVectorStore. Article metadata is attached for filtered retrieval
and citations, but excluded from the embedded text so it doesn't pollute the vector.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from configs import envs, logger
from db_helpers.database import DB_SCHEMA, engine
from rag_helpers.embeddings import get_embed_model
from rag_helpers.vector_store import get_vector_store

# Metadata kept for filtering/citation but NOT mixed into the embedded text or the
# LLM prompt (avoids polluting the semantic signal).
_EXCLUDED_KEYS = [
    "session_id", "project_id", "article_ref", "url", "published_date", "author",
    "domain", "publication",
]

# Some scraped articles carry the whole body in "title". Left alone, the metadata
# header alone exceeds CHUNK_SIZE and SentenceSplitter refuses the document.
_MAX_TITLE_CHARS = 300


def _article_text(article: dict[str, Any]) -> str:
    """The text to embed: title + primary body (content / article_text / summary)."""
    parts: list[str] = []
    title = (article.get("title") or "").strip()
    if title:
        parts.append(title)
    body = (
        article.get("content")
        or article.get("article_text")
        or article.get("summary")
        or ""
    ).strip()
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def build_document(article: dict[str, Any], session_id: int, project_id: int | None):
    """Build a LlamaIndex Document for one tagged article.

    ``doc_id`` is stable per (session, article) so re-ingesting replaces cleanly.
    """
    from llama_index.core import Document

    article_ref = str(article.get("id") or "")
    metadata = {
        "session_id": str(session_id),
        "project_id": str(project_id) if project_id is not None else "",
        "article_ref": article_ref,
        # Truncated for the metadata header only; the full title stays in the
        # document text via _article_text, so no content is lost.
        "title": (article.get("title") or "")[:_MAX_TITLE_CHARS],
        "author": article.get("author") or "",
        "url": article.get("url") or "",
        "domain": article.get("domain") or article.get("domain_name") or "",
        # Display name of the outlet ("The Washington Post"), used as citation
        # link text; falls back to the bare domain.
        "publication": article.get("domain_name") or article.get("domain") or "",
        "published_date": article.get("date") or "",
        "sentiment": article.get("sentiment") or "",
        "theme": article.get("theme") or "",
        "section": article.get("section") or "",
    }
    return Document(
        text=_article_text(article),
        metadata=metadata,
        doc_id=f"{session_id}:{article_ref}",
        excluded_embed_metadata_keys=_EXCLUDED_KEYS,
        excluded_llm_metadata_keys=_EXCLUDED_KEYS,
    )


def ingest_articles(
    articles: list[dict[str, Any]], session_id: int, project_id: int | None = None
) -> int:
    """Chunk + embed + insert a session's tagged articles into pgvector.

    Returns the number of documents ingested. Articles with no usable text are
    skipped. Synchronous (CPU-bound embedding + DB I/O) — call via
    ``asyncio.to_thread`` from async code so the event loop is not blocked.
    """
    from llama_index.core.ingestion import IngestionPipeline
    from llama_index.core.node_parser import SentenceSplitter

    documents = [
        build_document(a, session_id, project_id)
        for a in articles
        if isinstance(a, dict) and _article_text(a).strip()
    ]
    if not documents:
        logger.info(f"No embeddable article text for session_id={session_id}; skipping ingest.")
        return 0

    pipeline = IngestionPipeline(
        transformations=[
            SentenceSplitter(chunk_size=envs.CHUNK_SIZE, chunk_overlap=envs.CHUNK_OVERLAP),
            get_embed_model(),
        ],
        vector_store=get_vector_store(),
    )
    nodes = pipeline.run(documents=documents, show_progress=False)
    logger.info(
        f"Ingested {len(documents)} article(s) as {len(nodes)} chunk(s) for session_id={session_id}"
    )
    return len(documents)


def delete_session_chunks(session_id: int) -> None:
    """Purge all vector chunks for a session (call before re-ingesting on a re-tag).

    LlamaIndex owns the physical table ``data_<VECTOR_TABLE_NAME>`` with a JSONB
    ``metadata_`` column; we delete by the ``session_id`` stored there. Best-effort:
    the table may not exist yet on a first run.
    """
    physical_table = f"data_{envs.VECTOR_TABLE_NAME}"
    stmt = text(
        f"DELETE FROM {DB_SCHEMA}.{physical_table} "
        "WHERE metadata_->>'session_id' = :sid"
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt, {"sid": str(session_id)})
    except Exception:  # noqa: BLE001
        logger.warning(
            f"Could not purge vector chunks for session_id={session_id} "
            "(table may not exist yet).",
            exc_info=True,
        )


def reingest_session(
    articles: list[dict[str, Any]], session_id: int, project_id: int | None = None
) -> int:
    """Purge + re-ingest a session's chunks. Convenience for the tagging flow."""
    delete_session_chunks(session_id)
    return ingest_articles(articles, session_id, project_id)
