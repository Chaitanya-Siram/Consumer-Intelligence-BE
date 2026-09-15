"""Consumer Intelligence endpoints.

GET  /consumer-intelligence/charts?session_id=&lenses=&refresh=
WS   /ws/consumer-intelligence/charts   init: {"session_id": int, "lenses": [..]?, "refresh": bool?}
GET  /consumer-intelligence/lenses      → registry (tier1 map, coming-soon list)

Separate from /charts so the Media Intelligence pipeline is untouched. The FE
already knows each analysis node's `lensType`; it calls this endpoint when a
Consumer Intelligence lens is selected and /charts otherwise.

Cache: payload is stored on S3 at a deterministic key per session
(`session_files/session_{id}/ci_charts_data/latest.json`) because the session
table has no CI column and existing models must not change.
"""

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from configs import logger
from db_helpers.database import get_db
from db_helpers.repository.sessions_db import get_session
from db_helpers.repository.tagged_articles_db import get_tagged_articles
from file_helpers.s3_file import s3_file

from .builder import build_ci_charts, expand_lenses
from .tier_registry import CI_LENS_KEYS, COMING_SOON_TIER1, TIER1_TO_LENS_KEYS, resolve_ci_lenses

router = APIRouter(tags=["consumer-intelligence"])


def _cache_key(session_id: int) -> str:
    return f"session_files/session_{session_id}/ci_charts_data/latest.json"


def _load_cache(session_id: int) -> dict | None:
    try:
        raw = s3_file.download_file(_cache_key(session_id))
        return json.loads(raw, parse_constant=lambda _: None) if raw else None
    except Exception:
        return None


def _save_cache(session_id: int, payload: dict) -> None:
    try:
        s3_file.upload_file(_cache_key(session_id), json.dumps(payload).encode("utf-8"))
        s3_file.upload_file(
            f"session_files/session_{session_id}/ci_charts_data/ci_charts_data_{int(time.time())}.json",
            json.dumps(payload).encode("utf-8"),
        )
    except Exception:
        logger.exception("Failed to cache CI charts for session_id=%s", session_id)


def _workflow_nodes(record) -> list[dict]:
    workflow = getattr(record, "workflow", None) or {}
    return list(workflow.get("nodes") or []) if isinstance(workflow, dict) else []


def _articles_for_charts(db: Session, session_id: int) -> list[dict]:
    """Same gate as the MI dispatcher: approved-for-dashboards rows when the
    analyst has approved any, otherwise every relevant row."""
    tagged = get_tagged_articles(db, session_id, is_relevant=True)
    approved = [a for a in tagged if a.get("is_approved_for_dashboards")]
    return approved or tagged


@router.get("/consumer-intelligence/lenses")
def ci_lenses() -> Any:
    return {
        "lens_keys": sorted(CI_LENS_KEYS),
        "tier1_to_lens_keys": TIER1_TO_LENS_KEYS,
        "coming_soon_tier1": sorted(COMING_SOON_TIER1),
        "bundles": {"brand_intelligence": ["brand_intelligence", "brand_health_storyboard", "brand_competitive_intel"]},
    }


@router.get("/consumer-intelligence/charts")
async def ci_charts(
    session_id: int,
    lenses: list[str] = Query(default_factory=list),
    refresh: bool = False,
    with_media: bool = True,
    db: Session = Depends(get_db),
) -> Any:
    """Build (or return cached) Consumer Intelligence storyboards for a session."""
    record = get_session(db, session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    if not refresh:
        cached = _load_cache(session_id)
        if cached:
            return cached

    nodes = _workflow_nodes(record)
    if not lenses:
        resolved, coming = resolve_ci_lenses(nodes)
        if not resolved and not coming:
            raise HTTPException(
                status_code=400,
                detail="No Consumer Intelligence lens selected in this session's workflow. Pass ?lenses= explicitly.",
            )

    tagged_articles = _articles_for_charts(db, session_id)
    if not tagged_articles:
        raise HTTPException(status_code=404, detail="No relevant tagged articles for this session.")

    try:
        started = time.time()
        payload = await build_ci_charts(
            workflow_nodes=nodes,
            requested_lenses=lenses or None,
            tagged_articles=tagged_articles,
            brand_keywords=record.brand_keywords,
            competitor_keywords=record.competitor_keywords,
            with_media=with_media,
        )
        response = jsonable_encoder(payload)
        _save_cache(session_id, response)
        logger.info("CI charts generated for session_id=%s in %.1fs", session_id, time.time() - started)
        return response
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to generate CI charts for session_id=%s", session_id)
        raise HTTPException(status_code=500, detail=f"Failed to generate consumer intelligence charts: {exc}") from exc


@router.websocket("/ws/consumer-intelligence/charts")
async def ci_charts_stream(websocket: WebSocket, db: Session = Depends(get_db)) -> None:
    """Stream CI storyboard generation.

    Message types: "start", "progress", "lens_complete", "lens_error", "complete", "error".
    Each `lens_complete` carries the finished storyboard so the FE can render
    lenses as they land instead of waiting for the whole batch.
    """
    await websocket.accept()
    session_id = None
    try:
        init = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError) as exc:
        logger.warning("CI charts websocket init failed: %s", exc)
        await websocket.close()
        return
    try:
        session_id = init.get("session_id") if isinstance(init, dict) else None
        if session_id is None or not isinstance(session_id, int):
            await websocket.send_json({"type": "error", "detail": "Missing or invalid 'session_id'."})
            return
        lenses = init.get("lenses") if isinstance(init, dict) else None
        refresh = bool(init.get("refresh")) if isinstance(init, dict) else False
        with_media = init.get("with_media", True) if isinstance(init, dict) else True

        record = get_session(db, session_id)
        if record is None:
            await websocket.send_json({"type": "error", "detail": "Session not found."})
            return

        if not refresh:
            cached = _load_cache(session_id)
            if cached:
                await websocket.send_json({"type": "complete", "cached": True, "charts_data": cached})
                return

        nodes = _workflow_nodes(record)
        tagged_articles = _articles_for_charts(db, session_id)
        if not tagged_articles:
            await websocket.send_json({"type": "error", "detail": "No relevant tagged articles for this session."})
            return

        send_lock = asyncio.Lock()

        async def emit(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(jsonable_encoder(payload))

        started = time.time()
        payload = await build_ci_charts(
            workflow_nodes=nodes,
            requested_lenses=lenses or None,
            tagged_articles=tagged_articles,
            brand_keywords=record.brand_keywords,
            competitor_keywords=record.competitor_keywords,
            on_event=emit,
            with_media=bool(with_media),
        )
        response = jsonable_encoder(payload)
        _save_cache(session_id, response)
        await emit(
            {
                "type": "complete",
                "cached": False,
                "charts_data": response,
                "elapsed_seconds": round(time.time() - started, 1),
            }
        )
    except WebSocketDisconnect:
        logger.info("CI charts WebSocket disconnected for session_id=%s", session_id)
    except Exception as exc:
        logger.exception("CI charts stream failed for session_id=%s", session_id)
        try:
            await websocket.send_json({"type": "error", "detail": f"Failed to generate consumer intelligence charts: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


__all__ = ["router", "expand_lenses"]
