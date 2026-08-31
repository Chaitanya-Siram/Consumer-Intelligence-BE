import asyncio
import re
import time
from typing import Any
from fastapi import APIRouter, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy.orm import Session
from ai_helpers.llm_service import tag_articles, tag_articles_streaming
from ai_helpers.tagging_common import merge_tagged_with_articles, merge_tagged_with_syndication
from ai_helpers.article_linker import link_articles
from configs import logger
from data_source_helpers.fetching_service_v2 import fetching_service
from data_source_helpers.newspaper_helper import article_content_fetch
from db_helpers.repository.auth_repository.dependencies import get_connection_org_id
from db_helpers.repository.data_provider_keys_db import get_org_active_data_providers_key
from db_helpers.repository.sessions_db import (
    get_session,
    invalidate_session_charts,
    update_session_status,
    update_session_tagged_file,
)
from db_helpers.repository.raw_articles_db import get_raw_articles, get_untagged_raw_articles
from db_helpers.repository.tagged_articles_db import (
    add_tagged_articles as save_tagged_articles,
    count_tagged_articles,
    delete_tagged_article,
    get_tagged_articles,
    next_article_ref_number,
    replace_tagged_articles,
    upsert_tagged_article,
)
from db_helpers.repository.projects_db import get_project
from db_helpers.schemas.tagging_schema import ApproveRequest, FetchArticleRequest, NewTaggedArticle, TaggedArticleUpdate
from file_helpers.cleaing_data import clean_articles, reorder_by_confidence
from file_helpers.s3_file import s3_file
from file_helpers.similare_web_reach import get_reach
from db_helpers.database import get_db


router = APIRouter(tags=["tagging"])

# Per-field confidences, edited as 0–100 percents and stored as 0–1 floats.
_CONFIDENCE_FIELDS = ("sentiment_confidence", "theme_confidence", "section_category_confidence", "relevancy_confidence")

# Edits to these fields on a main article cascade to its syndicated copies
# (same story across domains → these classifications must stay in sync).
_SYNDICATION_CASCADE_FIELDS = ("section", "sentiment", "theme")


def _reindex_for_chat(session_id: int, project_id: int | None, articles: list[dict[str, Any]]) -> None:
    """Purge + re-embed a session's articles into the RAG vector store so chat can
    search them. Best-effort: a failure here (e.g. RAG deps not installed, model
    load error) must never fail the tagging pipeline itself."""
    try:
        from rag_helpers.ingestion import reingest_session

        count = reingest_session(articles, session_id, project_id)
        logger.info(f"Reindexed {count} article(s) for chat (session_id={session_id})")
    except Exception:  # noqa: BLE001
        logger.warning(
            f"RAG reindex failed for session_id={session_id}; chat search may be "
            "stale until the next successful tagging run.",
            exc_info=True,
        )


@router.post("/tagging/{session_id}")
def tagging(session_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)) -> Any:
    try:
        logger.info(f"Tagging process started for session_id={session_id}")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")

        # Workflow Data node may request Google News RSS (source google_news +
        # queries). Fetch + merge into the uploaded file, or create a new source
        # file when none was uploaded. No-op when no RSS request is configured.
        record, is_fetched = fetching_service.fetch_and_merge_articles(record, db)
        if is_fetched is False:
            raise HTTPException(status_code=404, detail="No articles found for the queries")

        if not record.source_file:
            raise HTTPException(status_code=400, detail="Workflow has no source_file set.")

        update_session_status(db, record, "Tagging")

        # Only tag what hasn't been tagged yet; earlier tagged rows are kept as-is.
        raw = get_untagged_raw_articles(db, session_id)
        if not raw:
            if count_tagged_articles(db, session_id):
                raise HTTPException(
                    status_code=400, detail="All articles in this session are already tagged."
                )
            raise HTTPException(status_code=404, detail="No data found in Source File")

        brand_keywords = record.brand_keywords or []
        competitor_keywords = record.competitor_keywords or []
        if brand_keywords:
            logger.info(f"Aspect-based sentiment for brand keywords: {brand_keywords}")

        project = get_project(db, record.project_id)
        sections_prompt = project.monitoring_sections_prompt if project else None

        articles = clean_articles(raw, start_id=next_article_ref_number(db, session_id))

        # Enrich each article with reach (S3 lookup + SimilarWeb fallback). Newly
        # fetched reaches are written back to the S3 reach file in the background.
        articles = get_reach(articles)

        # Link related articles BEFORE tagging so we can skip syndicated copies —
        # they inherit their main article's tags, so tagging them just wastes
        # tokens. Tag only mains + similar (everything that isn't a syndicated copy).
        articles = link_articles(articles)
        to_tag = [a for a in articles if not a.get("syndication_of")]
        copies = [a for a in articles if a.get("syndication_of")]
        copy_to_main = {a["id"]: a.get("syndication_of", "") for a in copies}
        logger.info(f"Tagging {len(to_tag)} articles; {len(copies)} syndicated copies inherit their main's tags")

        logger.info(f"Running AI tagging")
        started = time.time()
        tagged = tag_articles(articles, brand_keywords, competitor_keywords, sections_prompt=sections_prompt)
        logger.info(f"Tagging completed in {time.time() - started:.1f}s")
        
        # Merge tags onto the tagged articles, then attach the syndicated copies
        # with their main article's tags copied over.
        tagged_full = merge_tagged_with_syndication(to_tag, copies, copy_to_main, tagged)

        # Reorder by Confidence and Reassign Id
        # final_articles = reorder_by_confidence(tagged_full)
        final_articles = tagged_full

        # Append to the session's tagged articles; previously tagged rows stay.
        save_tagged_articles(db, session_id, final_articles)
        # Embed for chat in the background so the response isn't blocked on it.
        background_tasks.add_task(_reindex_for_chat, session_id, record.project_id, final_articles)
        if record.charts_data_file:
            s3_file.delete_file(record.charts_data_file)
        update_session_tagged_file(db, record, None)

        return final_articles
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Tagging process failed for session_id={session_id}")
        update_session_status(db, record, "Failed")
        raise HTTPException(status_code=500, detail=f"Tagging process failed: {exc}") from exc


@router.get("/tagging/{session_id}")
def get_tagged_articles_api(session_id: int, db: Session = Depends(get_db)) -> Any:
    try:
        logger.info("")
        logger.info(f"Fetching tagged articles for session_id={session_id}")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        # if not record.tagged_file:
        #     raise HTTPException(status_code=404, detail="No tagged file found for this session.")

        return get_tagged_articles(db, session_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Fetching tagged articles failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Fetching tagged articles failed: {exc}") from exc


@router.websocket("/ws/tagging")
async def tagging_stream(
    websocket: WebSocket,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_connection_org_id),
) -> None:
    """Stream tagging progress over a WebSocket.

    Mirrors the POST /tagging flow but emits a message per completed batch so the
    client sees how many article batches have finished in real time. Message
    types: "start", "batch", "complete", "error".
    """
    await websocket.accept()
    record = None
    try:
        init = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError) as exc:
        logger.warning(f"Websocket init failed: {exc}")
        await websocket.close()
        return
    try:
        session_id = init.get("session_id") if isinstance(init, dict) else None
        logger.info(f"Tagging stream started for session_id={session_id}")
        if session_id is None or not isinstance(session_id, int):
            await websocket.send_json({"type": "error", "detail": "Missing or invalid 'workflow_id'."})
            await websocket.close()
            return

        record = get_session(db, session_id)
        if record is None:
            await websocket.send_json({"type": "error", "detail": "Workflow not found."})
            return

        # Data Providers API Key, scoped to the org on the connection's token.
        data_providers_key = get_org_active_data_providers_key(db, org_id=org_id)

        loop = asyncio.get_running_loop()
        fetch_queue: asyncio.Queue = asyncio.Queue()
        FETCH_DONE = object()
        def on_fetch_progress(fetched: int) -> None:
            loop.call_soon_threadsafe(fetch_queue.put_nowait, fetched)
        
        # if record.session_type == SessionType.QUERY:
        await websocket.send_json({"type": "progress", "message": "Fetched articles......"})
        fetch_task = asyncio.create_task(
            asyncio.to_thread(
                fetching_service.fetch_and_merge_articles,
                record,
                db,
                data_providers_key,
                on_fetch_progress,
            )
        )
        fetch_task.add_done_callback(lambda _: fetch_queue.put_nowait(FETCH_DONE))

        fetched_total = 0
        while True:
            item = await fetch_queue.get()
            if item is FETCH_DONE:
                break
            if isinstance(item, int) and item != fetched_total:
                fetched_total = item
                await websocket.send_json({"type": "fetch_progress", "fetched": fetched_total})

        try:
            await fetch_task  # re-raise any exception from the fetch thread
        except ValueError as exc:
            # "Nothing found" / "no queries" — the pool may still hold untagged
            # articles from an earlier pass, so carry on and tag those.
            logger.warning(f"Pool fetch found nothing for session_id={session_id}: {exc}")
        await websocket.send_json({
            "type": "progress",
            "message": f"Fetched {fetched_total} articles — preparing to tag…" if fetched_total else "Fetched articles — preparing to tag…",
        })

        # if not record.source_file and record.session_type == SessionType.UPLOAD:
        #     await websocket.send_json({"type": "error", "detail": "Workflow has no source_file set."})
        #     return

        update_session_status(db, record, "Tagging")

        # Only tag what hasn't been tagged yet; earlier tagged rows are kept as-is.
        raw = get_untagged_raw_articles(db, session_id)
        if not raw:
            already_tagged = count_tagged_articles(db, session_id)
            if not already_tagged:
                await websocket.send_json(
                    {"type": "error", "detail": "No data found in Source File"}
                )
                update_session_status(db, record, "Failed")
                return

            # Nothing new to tag — report the session as already complete, using the
            # same fields the client reads off a normal "complete".
            logger.info(f"Nothing new to tag for session_id={session_id}; {already_tagged} already tagged")
            await websocket.send_json({"type": "start", "total_articles": 0})
            await websocket.send_json(
                {
                    "type": "complete",
                    "total_tagged": 0,
                    "elapsed_seconds": 0,
                    "detail": "All articles in this session are already tagged.",
                }
            )
            update_session_status(db, record, "Tagged")
            return

        articles = clean_articles(raw, start_id=next_article_ref_number(db, session_id))

        # Enrich with reach off the event loop (S3 lookup + SimilarWeb fallback);
        # the S3 reach-file write-back happens in the background inside get_reach.
        await websocket.send_json({"type": "progress", "message": "Fetching reach…"})
        articles = await asyncio.to_thread(get_reach, articles)

        brand_keywords = record.brand_keywords or []
        competitor_keywords = record.competitor_keywords or []
        if brand_keywords:
            logger.info(f"Aspect-based sentiment for brand keywords: {brand_keywords}")

        project = get_project(db, record.project_id)
        sections_prompt = project.monitoring_sections_prompt if project else None

        await websocket.send_json({"type": "start", "total_articles": len(articles)})

        await websocket.send_json({"type": "progress", "message": "Linking related articles…"})
        articles = await asyncio.to_thread(link_articles, articles)
        to_tag = [a for a in articles if not a.get("syndication_of")]
        syndications = [a for a in articles if a.get("syndication_of")]
        syndications_to_main = {a["id"]: a.get("syndication_of", "") for a in syndications}
        logger.info(f"Tagging {len(to_tag)} articles; {len(syndications)} syndicated copies inherit their main's tags")

        # Bridge the synchronous on_batch_done callback (invoked from worker
        # threads) into this async handler via a queue. A sentinel marks the end.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        DONE = object()

        def on_batch_done(payload: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        logger.info("Running AI tagging (streaming)")
        started = time.time()
        task = asyncio.create_task(
            asyncio.to_thread(
                tag_articles_streaming,
                to_tag,
                brand_keywords,
                competitor_keywords,
                sections_prompt,
                on_batch_done,
            )
        )
        # Sentinel is enqueued only after the worker fully returns, so it always
        # trails every batch payload (call_soon_threadsafe preserves FIFO order).
        task.add_done_callback(lambda _: queue.put_nowait(DONE))

        while True:
            payload = await queue.get()
            if payload is DONE:
                break
            await websocket.send_json(payload)

        tagged = await task  # re-raise any exception from the tagging thread
        logger.info(f"Tagging completed in {time.time() - started:.1f}s")

        # Merge tags onto the tagged articles, then attach the syndicated copies
        # with their main article's tags copied over.
        tagged_full = merge_tagged_with_syndication(to_tag, syndications, syndications_to_main, tagged)

        final_articles = tagged_full

        # Append to the session's tagged articles; previously tagged rows stay.
        save_tagged_articles(db, session_id, final_articles)
        # Embed for chat in the background so the client isn't blocked on it; the
        # task is fire-and-forget (it swallows its own errors and touches no request
        # state), so tagging completes immediately.
        asyncio.create_task(
            asyncio.to_thread(_reindex_for_chat, session_id, record.project_id, final_articles)
        )
        
        await websocket.send_json(
            {
                "type": "complete",
                "total_tagged": len(final_articles),
                "elapsed_seconds": round(time.time() - started, 1),
            }
        )
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session_id={session_id}")
    except Exception as exc:
        logger.exception(f"Tagging stream failed for session_id={session_id}")
        if record is not None:
            update_session_status(db, record, "Failed")
        try:
            await websocket.send_json({"type": "error", "detail": f"Tagging process failed: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.put("/tagging/{session_id}")
def update_tagged_articles(
    session_id: int,
    updates: list[TaggedArticleUpdate],
    db: Session = Depends(get_db),
) -> Any:
    """Patch tagged fields on one or more articles in the session's tagged file.

    Body: a JSON array of `{id, <tagged fields to change>}`. Only the provided
    tagged fields are applied (matched by `id`); the original article body stays
    untouched. Editing tags invalidates any cached dashboards for the session.
    """
    try:
        if not updates:
            raise HTTPException(status_code=400, detail="No articles provided to update.")

        logger.info(f"Updating {len(updates)} tagged articles for session_id={session_id}")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        if not record.tagged_file:
            raise HTTPException(status_code=404, detail="No tagged file found for this session.")

        articles = get_tagged_articles(db, session_id)

        by_id = {a.get("id"): a for a in articles if isinstance(a, dict)}
        # Map each main article id -> its syndicated copies (same story, other domains).
        synd_children: dict[str, list[dict]] = {}
        for a in articles:
            if isinstance(a, dict) and a.get("syndication_of"):
                synd_children.setdefault(a["syndication_of"], []).append(a)

        updated_ids: list[str] = []
        not_found_ids: list[str] = []
        cascaded_ids: list[str] = []
        for upd in updates:
            target = by_id.get(upd.id)
            if target is None:
                not_found_ids.append(upd.id)
                continue
            # Only the explicitly-sent tagged fields; never the id or body fields.
            patch = upd.model_dump(exclude_unset=True, exclude={"id"})
            # Confidences arrive as 0–100 percents; persist them as 0–1 floats.
            for cf in _CONFIDENCE_FIELDS:
                if patch.get(cf) is not None:
                    patch[cf] = patch[cf] / 100.0
            target.update(patch)
            updated_ids.append(upd.id)

            # Cascade section / sentiment / theme from a main article to its
            # syndicated copies (they're the same story, so these must match).
            cascade = {f: patch[f] for f in _SYNDICATION_CASCADE_FIELDS if f in patch}
            if cascade:
                for child in synd_children.get(upd.id, []):
                    child.update(cascade)
                    cascaded_ids.append(child.get("id"))

        if not updated_ids:
            raise HTTPException(
                status_code=404,
                detail=f"None of the provided ids exist in the tagged file: {not_found_ids}",
            )

        # Persist only the changed rows (edited targets + cascaded syndication copies).
        for changed_id in set(updated_ids) | {c for c in cascaded_ids if c}:
            article = by_id.get(changed_id)
            if article is not None:
                upsert_tagged_article(db, session_id, article)

        # The tags changed, so any dashboards built from the old tags are stale.
        if record.charts_data_file:
            try:
                s3_file.delete_file(record.charts_data_file)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to delete stale charts file; continuing.")
        invalidate_session_charts(db, record)

        logger.info(
            f"Updated {len(updated_ids)} tagged articles for session_id={session_id}"
            f" ({len(cascaded_ids)} syndicated cascades)"
        )
        return {
            "updated_count": len(updated_ids),
            "updated_ids": updated_ids,
            "cascaded_ids": cascaded_ids,
            "not_found_ids": not_found_ids,
            "tagged_file": "DB",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Updating tagged articles failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Updating tagged articles failed: {exc}") from exc


_LIST_FIELDS = ("brand_of_interest", "competitors", "other_competitors",
                "peoples", "countries", "organizations")


def _highest_article_num(articles: list[dict]) -> int:
    """Highest existing numeric suffix among A{n} ids (0 if none)."""
    highest = 0
    for a in articles:
        if not isinstance(a, dict):
            continue
        m = re.fullmatch(r"A(\d+)", str(a.get("id") or ""))
        if m:
            highest = max(highest, int(m.group(1)))
    return highest


@router.post("/tagging/{session_id}/fetch-article")
def fetch_and_tag_article(
    session_id: int,
    payload: FetchArticleRequest,
    db: Session = Depends(get_db),
) -> Any:
    """Fetch a single article by URL, AI-tag it, and return a *preview* (NOT saved).

    The article body is downloaded with newspaper; if it can't be retrieved (e.g.
    a paywalled / subscription-only page), a 422 'Subscription required' is raised.
    Otherwise the article is cleaned, enriched with reach, and tagged exactly like
    the pipeline. Confidences come back as 0–100 percents to match the review form.
    No id is assigned and nothing is persisted — the client reviews/edits the fields
    and then POSTs to /tagging/{session_id}/articles to save."""
    try:
        url = (payload.url or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="A URL is required.")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found.")

        logger.info(f"Fetching article by URL for session_id={session_id}: {url}")
        fetched = article_content_fetch({"url": url, "title": "", "published": ""})
        content = (fetched.get("content") if isinstance(fetched, dict) else "") or ""
        # article_content_fetch returns content "Subscription" when the download fails.
        if content.strip() in ("", "Subscription"):
            raise HTTPException(
                status_code=422,
                detail="Subscription required — couldn't fetch this article's content.",
            )

        articles = clean_articles([fetched])
        if not articles:
            raise HTTPException(status_code=422, detail="Couldn't parse the fetched article.")

        # Enrich with reach, then tag with the session's brand/competitor/section context.
        articles = get_reach(articles)
        brand_keywords = record.brand_keywords or []
        competitor_keywords = record.competitor_keywords or []
        project = get_project(db, record.project_id)
        sections_prompt = project.monitoring_sections_prompt if project else None

        tagged = tag_articles(articles, brand_keywords, competitor_keywords, sections_prompt=sections_prompt)
        preview = merge_tagged_with_articles(articles, tagged)[0]

        # Confidences are stored 0–1 but the review form edits them as 0–100 percents.
        for cf in _CONFIDENCE_FIELDS:
            v = preview.get(cf)
            if isinstance(v, (int, float)):
                preview[cf] = round(v * 100)
        # No id yet — a fresh A{n} is assigned only when the user saves the article.
        preview.pop("id", None)

        logger.info(f"Fetched and tagged article for session_id={session_id}: {url}")
        return preview
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Fetch+tag by URL failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Fetching article failed: {exc}") from exc


@router.post("/tagging/{session_id}/articles")
def add_tagged_articles(
    session_id: int,
    articles_in: list[NewTaggedArticle],
    db: Session = Depends(get_db),
) -> Any:
    """Append one or more new articles (body + tags) to the session's tagged file,
    each assigned a fresh sequential id. Editing the tagged set invalidates any
    cached dashboards. Returns the list of created articles."""
    try:
        if not articles_in:
            raise HTTPException(status_code=400, detail="No articles provided to add.")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        if not record.tagged_file:
            raise HTTPException(status_code=404, detail="No tagged file found for this session.")

        articles = get_tagged_articles(db, session_id)

        highest = _highest_article_num(articles)
        created: list[dict] = []
        for art in articles_in:
            if not (art.title or "").strip() and not (art.content or "").strip():
                raise HTTPException(status_code=400, detail="Each article needs a title or content.")
            new_article = art.model_dump()
            for field_name in _LIST_FIELDS:
                if new_article.get(field_name) is None:
                    new_article[field_name] = []
            # Confidences arrive as 0–100 percents; persist them as 0–1 floats.
            for cf in _CONFIDENCE_FIELDS:
                if new_article.get(cf) is not None:
                    new_article[cf] = round(new_article[cf] / 100, 2)
            # Normalize body fields (whitespace/HTML/date/domain) like the pipeline.
            # clean_articles assigns its own id, so re-apply our sequential id after.
            new_article = clean_articles([new_article])[0]
            highest += 1
            new_article["id"] = f"A{highest}"
            new_article["added_type"] = "Manual"  # mark user-added articles
            articles.append(new_article)
            created.append(new_article)

        # Insert only the newly-added rows.
        for new_article in created:
            upsert_tagged_article(db, session_id, new_article)

        # Tags changed → cached dashboards are stale.
        if record.charts_data_file:
            try:
                s3_file.delete_file(record.charts_data_file)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to delete stale charts file; continuing.")
        invalidate_session_charts(db, record)

        logger.info(f"Added {len(created)} article(s) to session_id={session_id}")
        return created
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Adding articles failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Adding articles failed: {exc}") from exc


@router.delete("/tagging/{session_id}/articles/{article_id}")
def delete_tagged_article(
    session_id: int,
    article_id: str,
    db: Session = Depends(get_db),
) -> Any:
    """Delete a manually-added article (added_type == 'Manual') from the session's
    tagged file. Only manual articles can be removed. Invalidates cached dashboards."""
    try:
        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        if not record.tagged_file:
            raise HTTPException(status_code=404, detail="No tagged file found for this session.")

        articles = get_tagged_articles(db, session_id)

        target = next((a for a in articles if isinstance(a, dict) and a.get("id") == article_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"Article {article_id} not found.")
        if (target.get("added_type") or "") != "Manual":
            raise HTTPException(status_code=400, detail="Only manually-added articles can be deleted.")

        delete_tagged_article(db, session_id, article_id)

        # Tags changed → cached dashboards are stale.
        if record.charts_data_file:
            try:
                s3_file.delete_file(record.charts_data_file)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to delete stale charts file; continuing.")
        invalidate_session_charts(db, record)

        logger.info(f"Deleted manual article {article_id} from session_id={session_id}")
        return {"deleted_id": article_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Deleting article failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Deleting article failed: {exc}") from exc


@router.post("/tagging/{session_id}/approve")
def approve_tagged_articles(
    session_id: int,
    payload: ApproveRequest,
    db: Session = Depends(get_db),
) -> Any:
    """Set the `is_approved` flag on the given articles in the session's tagged file.
    Approval is review metadata only — it does not affect dashboards."""
    try:
        if not payload.ids:
            raise HTTPException(status_code=400, detail="No article ids provided.")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        # if not record.tagged_file:
        #     raise HTTPException(status_code=404, detail="No tagged file found for this session.")

        articles = get_tagged_articles(db, session_id)

        # Media Monitoring approves into a separate flag so the two review flows
        # don't clobber each other's approvals.
        field = "is_approved_for_monitoring" if payload.for_monitoring else "is_approved"

        id_set = set(payload.ids)
        approved: list[dict] = []
        for a in articles:
            if isinstance(a, dict) and a.get("id") in id_set:
                a[field] = payload.is_approved
                approved.append(a)
        approved_ids = [a.get("id") for a in approved]

        not_found_ids = [i for i in payload.ids if i not in set(approved_ids)]
        if not approved_ids:
            raise HTTPException(status_code=404, detail=f"None of the provided ids exist: {not_found_ids}")

        for a in approved:
            upsert_tagged_article(db, session_id, a)
        invalidate_session_charts(db, record)

        logger.info(f"Set {field}={payload.is_approved} on {len(approved_ids)} article(s) for session_id={session_id}")
        return {"approved_ids": approved_ids, "not_found_ids": not_found_ids, "is_approved": payload.is_approved, "field": field}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Approving articles failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Approving articles failed: {exc}") from exc
