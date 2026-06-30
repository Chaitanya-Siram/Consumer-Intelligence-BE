"""Background scheduler for recurring daily generated-query runs.

A lightweight asyncio loop (started on app startup) ticks once a minute, finds
generated queries whose daily schedule is due in their timezone, and runs the
fetch -> tag pipeline for each — once per local day. No external dependency.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from configs import logger
from db_helpers.database import SessionLocal
from db_helpers.models.session_model import SessionType
from db_helpers.repository.generated_query_db import (
    get_generated_query,
    list_scheduled_generated_queries,
    mark_generated_query_run,
)
from db_helpers.repository.sessions_db import create_session
from data_source_helpers.fetching_service import fetch_articles_and_save

TICK_SECONDS = 60
_running: set[int] = set()  # ids currently executing — guards against overlap


def _is_due(gq, now_utc: datetime) -> bool:
    """Due when, in its timezone, 'now' is at/after today's scheduled time and it
    hasn't already run today. Robust to a missed tick (runs at the next tick)."""
    if not gq.schedule_time or not gq.schedule_timezone:
        return False
    try:
        tz = ZoneInfo(gq.schedule_timezone)
        parts = str(gq.schedule_time).strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
    except Exception:
        return False

    local_now = now_utc.astimezone(tz)
    scheduled_today = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local_now < scheduled_today:
        return False

    last = gq.last_run_at
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if last.astimezone(tz).date() == local_now.date():
            return False  # already ran today
    return True


def run_generated_query(gq_id: int) -> None:
    """Headless fetch -> tag run for one generated query. Creates a fresh QUERY
    session (the day's dataset), fetches articles, and tags them. Each step opens
    its own work against a dedicated DB session; failures are logged, not raised."""
    # Import here to avoid an import cycle at module load (routers import heavy deps).
    from routers.tagging_api import tagging

    db = SessionLocal()
    try:
        gq = get_generated_query(db, gq_id)
        if gq is None:
            return
        logger.info(f"[scheduler] Running generated query id={gq_id} ({gq.name})")

        session = create_session(
            db,
            project_id=gq.project_id,
            brand_keywords=gq.brand_keywords or [],
            competitor_keywords=gq.competitor_keywords or [],
            message_keywords=gq.message_keywords or [],
            session_type=SessionType.QUERY,
            queries=gq.queries,
        )
        # Fetch articles into a source file, then run the full tagging pipeline.
        fetch_articles_and_save(session, db)
        tagging(session.id, db)

        mark_generated_query_run(db, gq, datetime.now(timezone.utc))
        logger.info(f"[scheduler] Completed generated query id={gq_id} -> session id={session.id}")
    except Exception:
        logger.exception(f"[scheduler] Run failed for generated query id={gq_id}")
    finally:
        db.close()


async def _run_and_release(gq_id: int) -> None:
    try:
        await asyncio.to_thread(run_generated_query, gq_id)
    finally:
        _running.discard(gq_id)


async def _tick() -> None:
    db = SessionLocal()
    try:
        scheduled = list_scheduled_generated_queries(db)
    finally:
        db.close()

    now_utc = datetime.now(timezone.utc)
    for gq in scheduled:
        if gq.id in _running:
            continue
        if _is_due(gq, now_utc):
            _running.add(gq.id)
            asyncio.create_task(_run_and_release(gq.id))


async def scheduler_loop() -> None:
    """Run forever, ticking every TICK_SECONDS. Started from the app's startup hook."""
    logger.info("[scheduler] started")
    while True:
        try:
            await _tick()
        except Exception:
            logger.exception("[scheduler] tick error")
        await asyncio.sleep(TICK_SECONDS)
