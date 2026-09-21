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

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from configs import logger
from db_helpers.database import get_db
from db_helpers.repository.sessions_db import get_session
from db_helpers.repository.tagged_articles_db import get_tagged_articles
from file_helpers.s3_file import s3_file

from .builder import build_ci_charts, expand_lenses
from .tier_registry import CI_LENS_KEYS, COMING_SOON_TIER1, TIER1_TO_LENS_KEYS, resolve_ci_lenses
from . import qa_agent
from .profile_images import PROFILE_PREFIX
from .verbatim_capture import SCREENSHOT_PREFIX

router = APIRouter(tags=["consumer-intelligence"])


def _session_category(record) -> str:
    """The session's topic as configured (message_keywords, e.g. "car care");
    used as the product category in the LLM audit prompts. Empty when unset."""
    kws = getattr(record, "message_keywords", None) or []
    return str(kws[0]).strip() if isinstance(kws, list) and kws else ""


def _required_lenses(nodes: list[dict], requested: list[str] | None) -> list[str]:
    """Every CI lens key this session should carry, bundles expanded."""
    if requested:
        return expand_lenses([k for k in requested if k in CI_LENS_KEYS])
    resolved, _ = resolve_ci_lenses(nodes)
    return expand_lenses(resolved)


def _cached_lenses(cached: dict | None) -> set[str]:
    """Lens keys already present in a cached payload with a real storyboard
    (not a failed/coming-soon stub)."""
    if not isinstance(cached, dict):
        return set()
    return {
        k for k, v in cached.items()
        if k in CI_LENS_KEYS and isinstance(v, dict) and v.get("status") not in {"failed", "coming_soon"} and (v.get("meta") or v.get("tabs") or v.get("loyalty"))
    }


def _plan(session_id: int, nodes: list[dict], requested: list[str] | None, refresh: bool) -> tuple[dict | None, list[str], set[str] | None, list[str]]:
    """(cached_payload, required_lenses, lenses_to_skip, lenses_to_build).

    Without refresh: build only required lenses absent from the cache.
    With refresh: rebuild every required lens, keep cached lenses that were not
    requested (so `?lenses=x&refresh=true` refreshes x without dropping the rest).
    """
    cached = _load_cache(session_id)
    required = _required_lenses(nodes, requested)
    have = _cached_lenses(cached)
    if refresh:
        skip = have - set(required)
        missing = list(required)
    else:
        skip = set(have)
        missing = [k for k in required if k not in have]
    return cached, required, (skip if cached else None), missing


def _merge_payload(cached: dict | None, fresh: dict) -> dict:
    """Fresh lenses win; cached lenses not rebuilt are kept; meta reflects both.

    Lets a session that already has Brand Intelligence cached pick up a newly
    shipped lens (or one newly added to its workflow) without rebuilding the
    lenses it already has and without the FE having to pass refresh=true."""
    if not isinstance(cached, dict):
        return fresh
    out = dict(cached)
    for k, v in fresh.items():
        if k in ("meta", "coming_soon"):
            continue
        out[k] = v
    cached_meta = cached.get("meta") if isinstance(cached.get("meta"), dict) else {}
    fresh_meta = fresh.get("meta") if isinstance(fresh.get("meta"), dict) else {}
    lenses = list(dict.fromkeys([*(cached_meta.get("lenses") or []), *(fresh_meta.get("lenses") or [])]))
    out["meta"] = {**cached_meta, **fresh_meta, "lenses": lenses, "incremental": sorted(fresh_meta.get("built") or [])}
    still_coming = set((fresh.get("coming_soon") or {}).keys())
    out["coming_soon"] = {k: {"status": "coming_soon"} for k in sorted(still_coming)}
    for key in list(out.keys()):
        v = out[key]
        if isinstance(v, dict) and v.get("status") == "coming_soon" and key not in still_coming:
            del out[key]   # a Tier 1 that was coming soon when cached and has since shipped
    return out


import os

# Optional namespace for the S3 cache. Local dev shares the bucket with Render
# but has its own database, so session ids collide; set CI_CACHE_SCOPE=local in
# a dev .env to keep caches apart. Empty (the Render default) keeps today's keys.
_CACHE_SCOPE = os.getenv("CI_CACHE_SCOPE", "").strip().strip("/")


def _cache_prefix(session_id: int) -> str:
    scope = f"{_CACHE_SCOPE}/" if _CACHE_SCOPE else ""
    return f"session_files/{scope}session_{session_id}/ci_charts_data"


def _cache_key(session_id: int) -> str:
    return f"{_cache_prefix(session_id)}/latest.json"


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
            f"{_cache_prefix(session_id)}/ci_charts_data_{int(time.time())}.json",
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


@router.get("/consumer-intelligence/verbatim-image")
def verbatim_image(key: str) -> Response:
    """Streams a server-captured "Supporting Verbatims" screenshot from S3.
    `key` is restricted to the verbatim_capture cache prefix so this can't be
    used to read arbitrary objects out of the bucket."""
    if not key.startswith(SCREENSHOT_PREFIX) or ".." in key:
        raise HTTPException(status_code=400, detail="Invalid image key.")
    try:
        content = s3_file.download_file(key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Image not found.") from exc
    return Response(content=content, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


def _image_type(data: bytes) -> str:
    """Media type from the file's own signature; stored profile pictures are png, jpeg, gif or webp."""
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


@router.get("/consumer-intelligence/profile-image")
def profile_image(key: str) -> Response:
    """Streams a cached poster profile picture from S3. `key` is restricted to the
    profile_images prefix so this can't be used to read arbitrary objects."""
    if not key.startswith(PROFILE_PREFIX) or ".." in key:
        raise HTTPException(status_code=400, detail="Invalid image key.")
    try:
        content = s3_file.download_file(key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Image not found.") from exc
    return Response(content=content, media_type=_image_type(content), headers={"Cache-Control": "public, max-age=86400"})


@router.post("/consumer-intelligence/qa")
async def ci_qa(session_id: int, iterations: int | None = None, db: Session = Depends(get_db)) -> Any:
    """Run the verification-and-repair agent over a session's already-generated
    dashboard JSON, save the corrected payload, and return the agent's report."""
    if get_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    payload = _load_cache(session_id)
    if not payload:
        raise HTTPException(status_code=404, detail="No generated dashboard for this session yet.")
    articles = _articles_for_charts(db, session_id)
    report = await qa_agent.run(payload, articles, max_iterations=iterations)
    await asyncio.to_thread(_save_cache, session_id, payload)
    return report


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

    nodes = _workflow_nodes(record)
    if not lenses:
        resolved, coming = resolve_ci_lenses(nodes)
        if not resolved and not coming:
            raise HTTPException(
                status_code=400,
                detail="No Consumer Intelligence lens selected in this session's workflow. Pass ?lenses= explicitly.",
            )

    # Incremental cache: return the cached payload only when it already holds
    # every lens this session needs; otherwise build just the missing ones.
    # `refresh` rebuilds the required lenses but keeps other cached lenses.
    cached, required, skip, missing = _plan(session_id, nodes, lenses or None, refresh)
    if cached and not missing:
        return cached

    tagged_articles = _articles_for_charts(db, session_id)
    if not tagged_articles:
        if cached:
            return cached
        raise HTTPException(status_code=404, detail="No relevant tagged articles for this session.")

    # The FE can fire two GETs for the same session on first open (page load
    # plus a route-level fetch). Share one build per (session, lens set) so the
    # second caller awaits the first instead of running the LLM work again.
    key = (session_id, tuple(missing))
    task = _inflight.get(key)
    if task is None:
        task = asyncio.create_task(_build_and_cache(session_id, nodes, lenses or None, tagged_articles, record, with_media, skip, cached, missing, refresh=refresh))
        _inflight[key] = task
        task.add_done_callback(lambda _t, _k=key: _inflight.pop(_k, None))
    else:
        logger.info("CI charts build already in flight for session_id=%s; joining", session_id)
    try:
        return await asyncio.shield(task)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to generate CI charts for session_id=%s", session_id)
        raise HTTPException(status_code=500, detail=f"Failed to generate consumer intelligence charts: {exc}") from exc


_inflight: dict[tuple, asyncio.Task] = {}


async def _build_and_cache(session_id, nodes, lenses, tagged_articles, record, with_media, skip, cached, missing, refresh: bool = False) -> dict:
    started = time.time()
    payload = await build_ci_charts(
        workflow_nodes=nodes,
        requested_lenses=lenses,
        tagged_articles=tagged_articles,
        brand_keywords=record.brand_keywords,
        competitor_keywords=record.competitor_keywords,
        with_media=with_media,
        skip_lenses=skip,
        session_id=session_id,
        refresh=refresh,
        category=_session_category(record),
    )
    response = jsonable_encoder(_merge_payload(cached, payload))
    _save_cache(session_id, response)
    logger.info("CI charts generated for session_id=%s (%s) in %.1fs", session_id, ",".join(missing), time.time() - started)
    return response


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

        nodes = _workflow_nodes(record)
        cached, required, skip, missing = _plan(session_id, nodes, lenses or None, refresh)
        if cached and not missing:
            await websocket.send_json({"type": "complete", "cached": True, "charts_data": cached})
            return

        tagged_articles = _articles_for_charts(db, session_id)
        if not tagged_articles:
            if cached:
                await websocket.send_json({"type": "complete", "cached": True, "charts_data": cached})
            else:
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
            skip_lenses=skip,
            session_id=session_id,
            refresh=bool(refresh),
            category=_session_category(record),
        )
        response = jsonable_encoder(_merge_payload(cached, payload))
        _save_cache(session_id, response)
        await emit(
            {
                "type": "complete",
                "cached": False,
                "incremental": bool(cached),
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
