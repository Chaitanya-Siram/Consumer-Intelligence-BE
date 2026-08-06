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
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from agents.chart_generator.chart_agent import (
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
from db_helpers.repository.tagged_articles_db import get_tagged_articles

router = APIRouter(tags=["agent"])

# On a (code) sandbox failure, regenerate the chart code with the error fed back
# and re-run — up to this many times before giving up on that chart.
MAX_CODE_RETRIES = 3


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

        # Load tagged articles from the database (tagged_articles).
        articles = await asyncio.to_thread(get_tagged_articles, db, session_id)
        if not isinstance(articles, list):
            await websocket.send_json({"type": "error", "detail": "Tagged file is not a list of articles."})
            return

        intent = await asyncio.to_thread(classify_intent, query)
        await websocket.send_json({"type": "intent", "intent": intent})

        if intent == "chart":
            await _handle_chart(websocket, query, articles)
        else:
            await _handle_question(websocket, session_id, record.project_id, query)

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


_CITATION_SNIPPET_CHARS = 300
_PROMPT_EXCERPT_CHARS = 1200


def _build_sources(nodes: list) -> list[dict[str, Any]]:
    """Short, citation-friendly source records for the client."""
    sources: list[dict[str, Any]] = []
    for i, nws in enumerate(nodes, start=1):
        meta = nws.node.metadata or {}
        sources.append({
            "n": i,
            "article_ref": meta.get("article_ref") or None,
            "title": meta.get("title") or None,
            "url": meta.get("url") or None,
            "published_date": meta.get("published_date") or None,
            "sentiment": meta.get("sentiment") or None,
            "section": meta.get("section") or None,
            "score": float(nws.score) if nws.score is not None else None,
            "snippet": nws.node.get_content()[:_CITATION_SNIPPET_CHARS],
        })
    return sources


def _build_prompt_sources(nodes: list) -> str:
    """Longer numbered excerpts for grounding the generation."""
    lines = []
    for i, nws in enumerate(nodes, start=1):
        meta = nws.node.metadata or {}
        title = meta.get("title") or "Untitled"
        date = meta.get("published_date") or "n.d."
        text = nws.node.get_content()[:_PROMPT_EXCERPT_CHARS]
        lines.append(f"[{i}] {title} ({date})\n{text}")
    return "\n\n".join(lines)


async def _handle_question(
    websocket: WebSocket, session_id: int, project_id: int | None, query: str
) -> None:
    """Answer a data question with RAG: retrieve (hybrid + rerank + CRAG grade) over
    the session's embedded articles, then generate a grounded, cited answer."""
    await websocket.send_json({"type": "status", "message": "Searching articles…"})

    from rag_helpers.agent.graph import run_retrieval
    from rag_helpers.agent.prompts import ABSTAIN_MESSAGE, GENERATION_SYSTEM, GENERATION_USER
    from rag_helpers.llm import get_llm

    state = await run_retrieval(session_id, query, project_id=project_id)
    nodes = state.get("nodes") or []
    answerable = bool(state.get("answerable")) and bool(nodes)

    if not answerable:
        await websocket.send_json({"type": "answer", "answer": ABSTAIN_MESSAGE})
        await websocket.send_json({"type": "complete", "intent": "question"})
        return

    from llama_index.core.llms import ChatMessage, MessageRole

    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=GENERATION_SYSTEM),
        ChatMessage(
            role=MessageRole.USER,
            content=GENERATION_USER.format(
                question=query, sources=_build_prompt_sources(nodes)
            ),
        ),
    ]
    resp = await get_llm().achat(messages)
    answer = (resp.message.content if resp and resp.message else "") or ""

    await websocket.send_json({"type": "sources", "sources": _build_sources(nodes)})
    await websocket.send_json({"type": "answer", "answer": answer})
    await websocket.send_json({"type": "complete", "intent": "question"})
