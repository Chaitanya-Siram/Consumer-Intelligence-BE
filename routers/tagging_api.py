import asyncio
import json
import os
import re
import time
from typing import Any
from fastapi import APIRouter, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy.orm import Session
from ai_helpers.llm_service import tag_articles, tag_articles_streaming
from ai_helpers.tagging_common import merge_tagged_with_articles
from ai_helpers.article_linker import link_articles
from configs import logger
from data_source_helpers.fetching_service import fetch_articles_and_save
from data_source_helpers.newspaper_helper import article_content_fetch
from db_helpers.models.session_model import SessionType
from db_helpers.repository.sessions_db import (
    get_session,
    invalidate_session_charts,
    update_session_status,
    update_session_tagged_file,
)
from db_helpers.repository.projects_db import get_project
from db_helpers.schemas.tagging_schema import ApproveRequest, FetchArticleRequest, NewTaggedArticle, TaggedArticleUpdate
from file_helpers.cleaing_data import clean_articles, reorder_by_confidence
from file_helpers.file_parser import parse_upload
from file_helpers.s3_file import s3_file
from file_helpers.similare_web_reach import get_reach
from db_helpers.database import get_db


router = APIRouter(tags=["tagging"])

# Per-field confidences, edited as 0–100 percents and stored as 0–1 floats.
_CONFIDENCE_FIELDS = ("sentiment_confidence", "theme_confidence", "section_category_confidence", "relevancy_confidence")

# Edits to these fields on a main article cascade to its syndicated copies
# (same story across domains → these classifications must stay in sync).
_SYNDICATION_CASCADE_FIELDS = ("section", "sentiment", "theme")


@router.post("/tagging/{session_id}")
def tagging(session_id: int, db: Session = Depends(get_db)) -> Any:
    try:
        logger.info(f"Tagging process started for session_id={session_id}")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        if not record.source_file:
            raise HTTPException(status_code=400, detail="Workflow has no source_file set.")

        update_session_status(db, record, "Tagging")

        source_file = record.source_file

        file_data = s3_file.download_file(source_file)
        raw = parse_upload(source_file, file_data)
        if raw is None:
            raise HTTPException(status_code=404, detail="No data found in Source File")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="Stored payload is not a list of records.")

        brand_keywords = record.brand_keywords or []
        competitor_keywords = record.competitor_keywords or []
        if brand_keywords:
            logger.info(f"Aspect-based sentiment for brand keywords: {brand_keywords}")

        project = get_project(db, record.project_id)
        sections_prompt = project.monitoring_sections_prompt if project else None

        articles = clean_articles(raw)

        # Enrich each article with reach (S3 lookup + SimilarWeb fallback). Newly
        # fetched reaches are written back to the S3 reach file in the background.
        articles = get_reach(articles)

        logger.info(f"Running AI tagging")
        started = time.time()
        tagged = tag_articles(articles, brand_keywords, competitor_keywords, sections_prompt=sections_prompt)
        logger.info(f"Tagging completed in {time.time() - started:.1f}s")
        
        # Merging the tagged fields with original fields
        tagged_full = merge_tagged_with_articles(articles, tagged)

        # Reorder by Confidence and Reassign Id
        final_articles = reorder_by_confidence(tagged_full)

        # Link syndicated copies and same-story articles (uses the final ids).
        final_articles = link_articles(final_articles)

        # Uploading tagged articles to S3 JSON file
        name, _ext = os.path.splitext(source_file)
        tagged_file_name = f"{name.replace('raw', 'tagged')}.json"
        s3_file.upload_file(tagged_file_name, json.dumps(final_articles, default=str).encode("utf-8"))
        if record.charts_data_file:
            s3_file.delete_file(record.charts_data_file)
        update_session_tagged_file(db, record, tagged_file_name)
        
        return final_articles
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Tagging process failed for session_id={session_id}")
        update_session_status(db, record, "Failed")
        raise HTTPException(status_code=500, detail=f"Tagging process failed: {exc}") from exc


@router.get("/tagging/{session_id}")
def get_tagged_articles(session_id: int, db: Session = Depends(get_db)) -> Any:
    try:
        logger.info("")
        logger.info(f"Fetching tagged articles for session_id={session_id}")

        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Workflow not found.")
        if not record.tagged_file:
            raise HTTPException(status_code=404, detail="No tagged file found for this session.")

        file_data = s3_file.download_file(record.tagged_file)
        json_data = json.loads(file_data)
        return json_data
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Fetching tagged articles failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Fetching tagged articles failed: {exc}") from exc


@router.websocket("/ws/tagging")
async def tagging_stream(websocket: WebSocket, db: Session = Depends(get_db)) -> None:
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
        
        if record.session_type == SessionType.QUERY:
            await websocket.send_json({"type": "progress", "message": "Fetching articles from the internet…"})
            # SerpAPI calls are blocking — run off the event loop so the socket stays responsive.
            record = await asyncio.to_thread(fetch_articles_and_save, record, db)
            await websocket.send_json({"type": "progress", "message": "Fetched articles — preparing to tag…"})

        if not record.source_file:
            await websocket.send_json({"type": "error", "detail": "Workflow has no source_file set."})
            return

        update_session_status(db, record, "Tagging")
        source_file = record.source_file

        file_data = s3_file.download_file(source_file)
        raw = parse_upload(source_file, file_data)
        if raw is None:
            await websocket.send_json({"type": "error", "detail": "No data found in Source File"})
            update_session_status(db, record, "Failed")
            return
        if not isinstance(raw, list):
            await websocket.send_json({"type": "error", "detail": "Stored payload is not a list of records."})
            update_session_status(db, record, "Failed")
            return

        articles = clean_articles(raw)

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
                articles,
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

        # Merging the tagged fields with original fields
        tagged_full = merge_tagged_with_articles(articles, tagged)

        # Reorder by Confidence and Reassign Id
        final_articles = reorder_by_confidence(tagged_full)

        # Link syndicated copies and same-story articles (uses the final ids).
        await websocket.send_json({"type": "progress", "message": "Linking related articles…"})
        final_articles = await asyncio.to_thread(link_articles, final_articles)

        # Uploading tagged articles to S3 JSON file
        name, _ext = os.path.splitext(source_file)
        tagged_file_name = f"{name.replace('raw', 'tagged')}.json"
        s3_file.upload_file(tagged_file_name, json.dumps(final_articles, default=str).encode("utf-8"))
        update_session_tagged_file(db, record, tagged_file_name)

        await websocket.send_json(
            {
                "type": "complete",
                "tagged_file": tagged_file_name,
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

        file_data = s3_file.download_file(record.tagged_file)
        articles = json.loads(file_data)
        if not isinstance(articles, list):
            raise HTTPException(status_code=400, detail="Stored tagged file is not a list of articles.")

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

        # Persist the edited file back to the same S3 key.
        s3_file.upload_file(record.tagged_file, json.dumps(articles, default=str).encode("utf-8"))

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
            "tagged_file": record.tagged_file,
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

        file_data = s3_file.download_file(record.tagged_file)
        articles = json.loads(file_data)
        if not isinstance(articles, list):
            raise HTTPException(status_code=400, detail="Stored tagged file is not a list of articles.")

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

        s3_file.upload_file(record.tagged_file, json.dumps(articles, default=str).encode("utf-8"))

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

        file_data = s3_file.download_file(record.tagged_file)
        articles = json.loads(file_data)
        if not isinstance(articles, list):
            raise HTTPException(status_code=400, detail="Stored tagged file is not a list of articles.")

        target = next((a for a in articles if isinstance(a, dict) and a.get("id") == article_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"Article {article_id} not found.")
        if (target.get("added_type") or "") != "Manual":
            raise HTTPException(status_code=400, detail="Only manually-added articles can be deleted.")

        articles = [a for a in articles if not (isinstance(a, dict) and a.get("id") == article_id)]
        s3_file.upload_file(record.tagged_file, json.dumps(articles, default=str).encode("utf-8"))

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
        if not record.tagged_file:
            raise HTTPException(status_code=404, detail="No tagged file found for this session.")

        file_data = s3_file.download_file(record.tagged_file)
        articles = json.loads(file_data)
        if not isinstance(articles, list):
            raise HTTPException(status_code=400, detail="Stored tagged file is not a list of articles.")

        # Media Monitoring approves into a separate flag so the two review flows
        # don't clobber each other's approvals.
        field = "is_approved_for_monitoring" if payload.for_monitoring else "is_approved"

        id_set = set(payload.ids)
        approved_ids: list[str] = []
        for a in articles:
            if isinstance(a, dict) and a.get("id") in id_set:
                a[field] = payload.is_approved
                approved_ids.append(a.get("id"))

        not_found_ids = [i for i in payload.ids if i not in set(approved_ids)]
        if not approved_ids:
            raise HTTPException(status_code=404, detail=f"None of the provided ids exist: {not_found_ids}")

        s3_file.upload_file(record.tagged_file, json.dumps(articles, default=str).encode("utf-8"))
        invalidate_session_charts(db, record)

        logger.info(f"Set {field}={payload.is_approved} on {len(approved_ids)} article(s) for session_id={session_id}")
        return {"approved_ids": approved_ids, "not_found_ids": not_found_ids, "is_approved": payload.is_approved, "field": field}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Approving articles failed for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Approving articles failed: {exc}") from exc
