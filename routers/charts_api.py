import asyncio
from collections import defaultdict
import json
import time
from typing import Any, Callable
from urllib.parse import quote
from fastapi import APIRouter, HTTPException, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ai_helpers.chart_insight_synthesizer import synthesize_chart_insights
from ai_helpers.narrative_synthesizer import synthesize_top_narratives
from ai_helpers.storyboard_synthesizer import synthesize_storyboard
from db_helpers.database import get_db
from db_helpers.schema import DASHBOARDS_ENUM
from db_helpers.repository.sessions_db import get_session, update_session_charts_data_file
from db_helpers.repository.projects_db import get_project
from db_helpers.repository.tagged_articles_db import get_tagged_articles, upsert_tagged_article
from file_helpers.s3_file import s3_file
from configs import logger
from charts_helpers.dashboards import get_dashboards_chart_data
from reports_helpers.beone_report import (
    build_media_monitoring_report,
    sections_from_charts_data,
)
from reports_helpers.trane_report import (
    build_media_monitoring_report as build_trane_report,
)
from reports_helpers.otsuka_report import (
    build_media_monitoring_report as build_otsuka_report,
)

router = APIRouter(tags=["charts"])


@router.get("/charts")
def charts(
    session_id: int,
    dashboards: list[str] = Query(default_factory=list),
    db: Session = Depends(get_db),
) -> Any:
    """Generate chart data from a tagged-articles JSON file stored in S3."""
    try:
        # Default to every dashboard when none are requested.
        dashboards = dashboards or [d.value for d in DASHBOARDS_ENUM]
        # Fetch workflow details from DB to confirm workflow_id exists and is valid
        record = get_session(db, session_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Session not found.")
        
        if record.charts_data_file:
            charts_data = s3_file.download_file(record.charts_data_file)
            return json.loads(charts_data, parse_constant=lambda _: None)
        
        # tagged_file = record.tagged_file
        # if tagged_file is None:
        #     raise HTTPException(status_code=404, detail=f"Not processed yet, run the tagging agent")
        
        if record.workflow:
            dashboards = []
            for node in record.workflow.get("nodes"):
                if node.get("type") == "analysis" and "lens" in node.get("data", {}):
                    dashboards.append(node["data"]["lens"])
            dashboards = list(set(dashboards))
        
        brand_keywords = record.brand_keywords
        competitor_keywords = record.competitor_keywords
        message_keywords = record.message_keywords
        project = get_project(db, record.project_id)
        sections_orders = project.sections_orders if project else None

        tagged_articles = get_tagged_articles(db, session_id)

        dashboards_chart_data, data_for_insight = get_dashboards_chart_data(dashboards, tagged_articles, brand_keywords, competitor_keywords, message_keywords, sections_orders)
        
        logger.info("Generating Charts Insight and Overall Summaries")
        started = time.time()
        insights = synthesize_chart_insights(
            dashboards,
            data_for_insight,
            brand_keywords=brand_keywords,
            tagged_articles=tagged_articles,
        )
        logger.info(f"Insights Completed in {time.time() - started:.1f}s")

        # Top Narratives
        top_narratives = []
        if DASHBOARDS_ENUM.narrative_intelligence in dashboards:
            logger.info("Generating Top Narratives")
            narrative_started = time.time()
            top_narratives = synthesize_top_narratives(tagged_articles, brand_keywords)
            logger.info(f"Top Narrative Completed in {time.time() - narrative_started:.1f}s ({len(top_narratives)} narratives)")

        storyboards = defaultdict(dict)
        logger.info("Generating Story Boarding")
        for d in dashboards:
            charts_data = dashboards_chart_data.get(d, {})
            if not charts_data or d == DASHBOARDS_ENUM.media_monitoring:
                continue
            storyboard_started = time.time()
            storyboard = synthesize_storyboard(charts_data, dashboard=d, brand_keywords=brand_keywords)
            logger.info(f"Storyboard synthesis completed for {d} in {time.time() - storyboard_started:.1f}s ({len(storyboard)} chapters)")
            storyboards[d] = storyboard


        # jsonable_encoder turns the ChartResult Pydantic models into plain dicts
        # (recursively), so the saved JSON is a list of dicts — not str(model) reprs.
        response = jsonable_encoder({
            **dashboards_chart_data,
            **insights,
            "top_narratives": top_narratives,
            "storyboards": storyboards
        })

        # Save the response to S3 (charts_data stays on S3) and log the key to the DB.
        charts_data_file_name = f"session_files/session_{record.id}/charts_data/charts_data_{int(time.time())}.json"
        s3_file.upload_file(charts_data_file_name, json.dumps(response).encode("utf-8"))
        update_session_charts_data_file(db, record, charts_data_file_name)
        logger.info("Charts Data uploaded successfully")

        return response
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Failed to generate charts for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Failed to generate charts: {exc}") from exc


@router.get("/media-monitoring/report")
def media_monitoring_report(
    session_id: int,
    days: list[str] = Query(default_factory=list),
    db: Session = Depends(get_db),
) -> Any:
    """Download the media-monitoring articles as a BeOne-style .docx report.

    Reads the session's cached charts data from S3, builds the report from the
    media-monitoring section table, and streams it back as a Word document.
    Pass `days` (repeated YYYY-MM-DD values) to limit the report to specific
    dates; omit it for the full feed.
    """
    record = get_session(db, session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not record.charts_data_file:
        raise HTTPException(status_code=404, detail="Charts not generated yet for this session.")

    raw = s3_file.download_file(record.charts_data_file)
    charts_data = json.loads(raw, parse_constant=lambda _: None)

    # Normalize the day filter (drop empties) so an empty list means "all dates".
    day_filter = [d for d in (days or []) if d]
    sections = sections_from_charts_data(charts_data, days=day_filter or None)
    if not sections:
        raise HTTPException(status_code=404, detail="No media-monitoring data available for this session.")

    brand_keywords = record.brand_keywords or []
    competitor_keywords = record.competitor_keywords or []
    brand = brand_keywords[0] if brand_keywords else "BeOne"

    # Each brand can use its own report layout; everyone else gets the BeOne style.
    brand_lower = brand.lower()
    try:
        if "trane" in brand_lower:
            doc_bytes = build_trane_report(sections, brand=brand)
        elif "otsuka" in brand_lower:
            doc_bytes = build_otsuka_report(
                sections,
                brand=brand,
                brand_keywords=brand_keywords,
                competitor_keywords=competitor_keywords,
            )
        else:
            doc_bytes = build_media_monitoring_report(sections, brand=brand)
    except Exception as exc:
        logger.exception(f"Failed to build media-monitoring report for session_id={session_id}")
        raise HTTPException(status_code=500, detail=f"Failed to build report: {exc}") from exc

    filename = "media_monitoring_report.docx"
    headers = {
        "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
    }
    return StreamingResponse(
        iter([doc_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


SECTION_ARTICLES_CHART_ID = "section_articles"


class MoveArticleRequest(BaseModel):
    session_id: int = Field(..., description="Session whose dashboard is being edited.")
    article_id: str = Field(..., description="Id of the article to move.")
    from_section: str = Field(..., description="Section the article is currently in.")
    to_section: str = Field(..., description="Section the article should move to.")


def _move_article_in_mm_charts(charts_data: dict, article_id: str, from_section: str, to_section: str) -> bool:
    """Move an article (by id) between media-monitoring sections in a charts payload.

    Mutates ``charts_data`` in place. The article is removed from whichever section
    currently holds it (preferring ``from_section``) and appended to ``to_section``.
    Returns True when the article was found and moved.
    """
    mm = charts_data.get("media_monitoring")
    if not isinstance(mm, list):
        return False
    chart = next(
        (c for c in mm if isinstance(c, dict) and c.get("chart_id") == SECTION_ARTICLES_CHART_ID),
        None,
    )
    if not chart or not isinstance(chart.get("data"), dict):
        return False

    data = chart["data"]
    target_id = str(article_id)

    # Remove from the named source first, then fall back to any other section.
    article = None
    search_order = [from_section] + [s for s in data if s != from_section]
    for section in search_order:
        items = data.get(section)
        if not isinstance(items, list):
            continue
        for i, a in enumerate(items):
            if isinstance(a, dict) and str(a.get("id")) == target_id:
                article = items.pop(i)
                break
        if article is not None:
            break

    if article is None:
        return False

    if not isinstance(data.get(to_section), list):
        data[to_section] = []
    data[to_section].append(article)
    return True


def _set_article_section_in_tagged(tagged_articles: list, article_id: str, to_section: str) -> bool:
    """Set the ``section`` of the matching tagged article(s). Returns True if changed."""
    target_id = str(article_id)
    changed = False
    for item in tagged_articles:
        if isinstance(item, dict) and str(item.get("id")) == target_id:
            item["section"] = to_section
            changed = True
    return changed


@router.put("/media-monitoring/move-article")
def move_media_monitoring_article(payload: MoveArticleRequest, db: Session = Depends(get_db)) -> Any:
    """Move a media-monitoring article from one section to another.

    Updates the session's cached charts file (so the dashboard reflects the move
    immediately on the next load) and the underlying tagged-articles file (so the
    move survives a charts regeneration). Both files live in S3.
    """
    if payload.from_section == payload.to_section:
        return {"status": "noop"}

    record = get_session(db, payload.session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    if not record.charts_data_file:
        raise HTTPException(status_code=404, detail="Charts not generated yet for this session.")

    # 1) Move the article inside the cached charts file.
    raw = s3_file.download_file(record.charts_data_file)
    charts_data = json.loads(raw, parse_constant=lambda _: None)
    if not _move_article_in_mm_charts(charts_data, payload.article_id, payload.from_section, payload.to_section):
        raise HTTPException(status_code=404, detail="Article not found in the media-monitoring sections.")
    s3_file.upload_file(record.charts_data_file, json.dumps(charts_data, default=str).encode("utf-8"))

    # 2) Re-tag the article in the tagged_articles table so a regeneration keeps the move.
    if record.tagged_file:
        try:
            tagged_articles = get_tagged_articles(db, payload.session_id)
            if _set_article_section_in_tagged(tagged_articles, payload.article_id, payload.to_section):
                for a in tagged_articles:
                    if isinstance(a, dict) and str(a.get("id")) == str(payload.article_id):
                        upsert_tagged_article(db, payload.session_id, a)
        except Exception:  # noqa: BLE001
            logger.exception(f"Failed to update tagged articles for session_id={payload.session_id}")

    logger.info(
        f"Moved article {payload.article_id} from '{payload.from_section}' to "
        f"'{payload.to_section}' for session_id={payload.session_id}"
    )
    return {"status": "ok"}


def _build_charts(
    dashboards: list[str],
    tagged_articles: list[dict],
    brand_keywords: Any,
    competitor_keywords: Any,
    message_keywords: Any,
    emit: Callable[[dict[str, Any]], None],
    sections_orders: Any = None,
) -> dict[str, Any]:
    """Build the full charts response, emitting a progress event per stage.

    Runs the same pipeline as the GET /charts handler. `emit` is a thread-safe
    callback that forwards a payload dict to the WebSocket. Pure compute — no DB
    access — so it is safe to run in a worker thread.
    """
    emit({"type": "progress", "stage": "chart_data", "message": "Building chart data…"})
    dashboards_chart_data, data_for_insight = get_dashboards_chart_data(
        dashboards, tagged_articles, brand_keywords, competitor_keywords, message_keywords, sections_orders
    )

    emit({"type": "progress", "stage": "insights", "message": "Generating chart insights and overall summaries…"})
    started = time.time()
    insights = synthesize_chart_insights(
        dashboards,
        data_for_insight,
        brand_keywords=brand_keywords,
        tagged_articles=tagged_articles,
    )
    logger.info(f"Insights Completed in {time.time() - started:.1f}s")

    top_narratives = []
    if DASHBOARDS_ENUM.narrative_intelligence in dashboards:
        emit({"type": "progress", "stage": "narratives", "message": "Generating top narratives…"})
        narrative_started = time.time()
        top_narratives = synthesize_top_narratives(tagged_articles, brand_keywords)
        logger.info(
            f"Top Narrative Completed in {time.time() - narrative_started:.1f}s ({len(top_narratives)} narratives)"
        )

    storyboards = defaultdict(dict)
    for d in dashboards:
        charts_data = dashboards_chart_data.get(d, {})
        if not charts_data or d == DASHBOARDS_ENUM.media_monitoring:
            continue
        emit({"type": "progress", "stage": "storyboard", "dashboard": str(d), "message": f"Building storyboard for {d}…"})
        storyboard_started = time.time()
        storyboard = synthesize_storyboard(charts_data, dashboard=d, brand_keywords=brand_keywords)
        logger.info(
            f"Storyboard synthesis completed for {d} in {time.time() - storyboard_started:.1f}s ({len(storyboard)} chapters)"
        )
        storyboards[d] = storyboard

    return jsonable_encoder({
        **dashboards_chart_data,
        **insights,
        "top_narratives": top_narratives,
        "storyboards": storyboards,
    })


@router.websocket("/ws/charts")
async def charts_stream(websocket: WebSocket, db: Session = Depends(get_db)) -> None:
    """Stream chart generation progress over a WebSocket.

    Mirrors the GET /charts flow but emits a message per pipeline stage (chart
    data, insights, narratives, storyboards) so the client sees progress in real
    time. Message types: "start", "progress", "complete", "error".

    The client sends an init message: {"session_id": int, "dashboards": [..]?}.
    """
    await websocket.accept()
    session_id = None
    try:
        init = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError) as exc:
        logger.warning(f"Charts websocket init failed: {exc}")
        await websocket.close()
        return
    try:
        session_id = init.get("session_id") if isinstance(init, dict) else None
        logger.info(f"Charts stream started for session_id={session_id}")
        if session_id is None or not isinstance(session_id, int):
            await websocket.send_json({"type": "error", "detail": "Missing or invalid 'session_id'."})
            await websocket.close()
            return

        dashboards = init.get("dashboards") if isinstance(init, dict) else None
        dashboards = dashboards or [d.value for d in DASHBOARDS_ENUM]

        record = get_session(db, session_id)
        if record is None:
            await websocket.send_json({"type": "error", "detail": "Session not found."})
            return

        # Cached charts: short-circuit and return the stored payload immediately.
        if record.charts_data_file:
            charts_data = s3_file.download_file(record.charts_data_file)
            await websocket.send_json(
                {
                    "type": "complete",
                    "cached": True,
                    "charts_data": json.loads(charts_data, parse_constant=lambda _: None),
                }
            )
            return

        # tagged_file = record.tagged_file
        # if tagged_file is None:
        #     await websocket.send_json({"type": "error", "detail": "Not processed yet, run the tagging agent"})
        #     return
        
        if record.workflow:
            dashboards = []
            for node in record.workflow.get("nodes"):
                if node.get("type") == "analysis" and node.get("data", {}).get("lens"):
                    dashboards.append(node["data"]["lens"])
            dashboards = list(set(dashboards))

        brand_keywords = record.brand_keywords
        competitor_keywords = record.competitor_keywords
        message_keywords = record.message_keywords
        project = get_project(db, record.project_id)
        sections_orders = project.sections_orders if project else None

        tagged_articles = get_tagged_articles(db, session_id)

        await websocket.send_json(
            {"type": "start", "dashboards": dashboards, "total_articles": len(tagged_articles)}
        )

        # Bridge the synchronous per-stage emit callback (invoked from the worker
        # thread) into this async handler via a queue. A sentinel marks the end.
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        DONE = object()

        def emit(payload: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        started = time.time()
        task = asyncio.create_task(
            asyncio.to_thread(
                _build_charts,
                dashboards,
                tagged_articles,
                brand_keywords,
                competitor_keywords,
                message_keywords,
                emit,
                sections_orders,
            )
        )
        task.add_done_callback(lambda _: queue.put_nowait(DONE))

        while True:
            payload = await queue.get()
            if payload is DONE:
                break
            await websocket.send_json(payload)

        response = await task  # re-raise any exception from the compute thread
        logger.info(f"Charts generated in {time.time() - started:.1f}s")

        # Persist to S3 + flip the session to "Completed", mirroring GET /charts so
        # the dashboard is cached and re-opening short-circuits above. charts_data
        # stays on S3; only its key is logged to the DB.
        charts_data_file_name = f"session_files/session_{record.id}/charts_data/charts_data_{int(time.time())}.json"
        s3_file.upload_file(charts_data_file_name, json.dumps(response).encode("utf-8"))
        update_session_charts_data_file(db, record, charts_data_file_name)
        logger.info("Charts Data uploaded successfully")

        await websocket.send_json(
            {
                "type": "complete",
                "cached": False,
                "charts_data": response,
                "elapsed_seconds": round(time.time() - started, 1),
            }
        )
    except WebSocketDisconnect:
        logger.info(f"Charts WebSocket disconnected for session_id={session_id}")
    except Exception as exc:
        logger.exception(f"Charts stream failed for session_id={session_id}")
        try:
            await websocket.send_json({"type": "error", "detail": f"Failed to generate charts: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
