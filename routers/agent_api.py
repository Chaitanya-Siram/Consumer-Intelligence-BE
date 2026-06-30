"""WebSocket endpoint for the interactive data agent.

Client flow:
  1. Connect to  ws://<host>/ws/agent
  2. Send a JSON init frame:  {"session_id": <int>, "query": "<user message>"}
  3. Receive a stream of typed messages until "complete" (or "error").

Message types emitted by the server:
  - {"type": "start",   "session_id": int}
  - {"type": "intent",  "intent": "chart" | "question"}
  - {"type": "status",  "message": str}
  - {"type": "plan",    "count": int, "charts": [{chart_id, title, description}]}   (chart intent)
  - {"type": "code",    "chart_id": str, "python_code": str, "attempt": int}        (chart intent, per attempt)
  - {"type": "retry",   "chart_id": str, "attempt": int, "max_retries": int, "error": str}  (chart intent, on code error)
  - {"type": "chart",   "chart": ChartResult}                                       (chart intent, one per chart)
  - {"type": "answer",  "answer": str}                                              (question intent)
  - {"type": "complete","intent": str, ...}
  - {"type": "error",   "detail": str}
"""
import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from agents.chart_generator.chart_agent import (
    answer_question,
    chart_result_from_data,
    classify_intent,
    failed_chart_result,
    fix_chart_code,
    generate_chart_specs,
)
from agents.chart_generator.sandbox import run_chart_code
from configs import logger
from db_helpers.database import get_db
from db_helpers.repository.sessions_db import get_session
from file_helpers.s3_file import s3_file

router = APIRouter(tags=["agent"])

# On a (code) sandbox failure, regenerate the chart code with the error fed back
# and re-run — up to this many times before giving up on that chart.
MAX_CODE_RETRIES = 3


def _load_json(file_key: str) -> Any:
    raw = s3_file.download_file(file_key)
    return json.loads(raw)


def _load_json_safe(file_key: str) -> Any | None:
    try:
        return _load_json(file_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Failed to load '{file_key}' from S3: {exc}")
        return None


@router.websocket("/ws/agent")
async def agent_stream(websocket: WebSocket, db: Session = Depends(get_db)) -> None:
    await websocket.accept()
    try:
        init = await websocket.receive_json()
    except (WebSocketDisconnect, ValueError) as exc:
        logger.warning(f"Agent websocket init failed: {exc}")
        await websocket.close()
        return

    try:
        session_id = init.get("session_id") if isinstance(init, dict) else None
        query = (init.get("query") or "").strip() if isinstance(init, dict) else ""

        if not isinstance(session_id, int):
            await websocket.send_json({"type": "error", "detail": "Missing or invalid 'session_id'."})
            return
        if not query:
            await websocket.send_json({"type": "error", "detail": "Missing 'query'."})
            return

        logger.info(f"Agent started for session_id={session_id}: {query!r}")

        record = get_session(db, session_id)
        if record is None:
            await websocket.send_json({"type": "error", "detail": "Session not found."})
            return
        if not record.tagged_file:
            await websocket.send_json({"type": "error", "detail": "No tagged file for this session; run the tagging agent first."})
            return

        await websocket.send_json({"type": "start", "session_id": session_id})

        # Load tagged articles off the event loop (S3 download + json parse).
        articles = await asyncio.to_thread(_load_json, record.tagged_file)
        if not isinstance(articles, list):
            await websocket.send_json({"type": "error", "detail": "Tagged file is not a list of articles."})
            return

        intent = await asyncio.to_thread(classify_intent, query)
        await websocket.send_json({"type": "intent", "intent": intent})

        if intent == "chart":
            await _handle_chart(websocket, query, articles)
        else:
            await _handle_question(websocket, query, articles, record.charts_data_file)

    except WebSocketDisconnect:
        logger.info("Agent websocket disconnected by client")
    except Exception as exc:
        logger.exception("Agent websocket failed")
        try:
            await websocket.send_json({"type": "error", "detail": f"Agent failed: {exc}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _handle_chart(websocket: WebSocket, query: str, articles: list[dict[str, Any]]) -> None:
    await websocket.send_json({"type": "status", "message": "Generating chart code..."})
    specs = await asyncio.to_thread(generate_chart_specs, query, articles)
    if not specs:
        await websocket.send_json({"type": "error", "detail": "Could not generate a chart for this request."})
        return

    await websocket.send_json({
        "type": "plan",
        "count": len(specs),
        "charts": [
            {"chart_id": s.get("chart_id"), "title": s.get("title"), "description": s.get("description")}
            for s in specs
        ],
    })

    charts_out: list[dict[str, Any]] = []
    for spec in specs:
        chart = await _execute_chart_with_retry(websocket, query, spec, articles)
        payload = jsonable_encoder(chart)
        charts_out.append(payload)
        await websocket.send_json({"type": "chart", "chart": payload})

    await websocket.send_json({"type": "complete", "intent": "chart", "charts": charts_out})


async def _execute_chart_with_retry(
    websocket: WebSocket, query: str, spec: dict[str, Any], articles: list[dict[str, Any]]
):
    """Run one chart's code in the sandbox; on a code error, regenerate the code
    with the error fed back and retry, up to MAX_CODE_RETRIES times. Streams each
    attempt (code -> status -> retry). Returns a ChartResult (with .error set if
    every attempt failed)."""
    chart_id = spec.get("chart_id", "chart")
    attempt = 0
    while True:
        await websocket.send_json({
            "type": "code", "chart_id": chart_id,
            "python_code": spec.get("python_code", ""), "attempt": attempt + 1,
        })

        await websocket.send_json({
            "type": "status",
            "message": f"Executing '{chart_id}' (attempt {attempt + 1}/{MAX_CODE_RETRIES + 1})...",
        })

        result = await asyncio.to_thread(run_chart_code, spec.get("python_code", ""), articles)
        if result.ok and result.data:
            return chart_result_from_data(spec, result.data)

        error = result.error or "Sandbox returned no chart data."

        # Don't burn retries on infra errors (missing key/package, network) — the
        # code is fine; regenerating it won't help.
        if not result.retryable or attempt >= MAX_CODE_RETRIES:
            return failed_chart_result(spec, error)

        attempt += 1
        await websocket.send_json({
            "type": "retry", "chart_id": chart_id, "attempt": attempt,
            "max_retries": MAX_CODE_RETRIES, "error": error,
        })
        await websocket.send_json({
            "type": "status",
            "message": f"'{chart_id}' failed — regenerating code (retry {attempt}/{MAX_CODE_RETRIES}).",
        })

        fixed_code = await asyncio.to_thread(fix_chart_code, query, articles, spec, error)
        if not fixed_code.strip():
            return failed_chart_result(spec, f"{error} (auto-fix produced no code)")
        spec["python_code"] = fixed_code


async def _handle_question(
    websocket: WebSocket, query: str, articles: list[dict[str, Any]], charts_data_file: str | None
) -> None:
    await websocket.send_json({"type": "status", "message": "Reading tagged articles and dashboard charts..."})
    charts_data = await asyncio.to_thread(_load_json_safe, charts_data_file) if charts_data_file else None
    answer = await asyncio.to_thread(answer_question, query, articles, charts_data)
    await websocket.send_json({"type": "answer", "answer": answer})
    await websocket.send_json({"type": "complete", "intent": "question"})
