"""WebSocket endpoint for the conversational workflow-builder agent.

Unlike /ws/agent and /ws/charts, this connection is long-lived: the whole multi-turn
conversation runs over one socket and the state lives for its duration.

Client flow:
  1. Connect to  ws://<host>/ws/workflow-agent?token=<jwt>
  2. Receive "ready" (the org's data providers) and the agent's opening turn.
  3. Send {"type": "message", "text": "..."} each turn.
     Also accepted: {"type": "reset"} to start over, {"type": "close"} to finish,
     and {"type": "attach_file", "name": "x.csv", "file_upload_id": "..."} to
     switch the pipeline to analysing an upload (send it with no id to switch
     back to fetching). Upload via POST /upload first to get the id.
  4. Receive typed frames until the socket closes.

Server frame types:
  - {"type": "ready",    "providers": {name: label}}
  - {"type": "agent",    "message": str, "options": [str, ...]}
  - {"type": "query",    "message": str, "queries": [str], "rationale": str, "options": [...]}
  - {"type": "workflow", "workflow": {nodes, edges}, "summary": str}
  - {"type": "state",    "state": {...}}        (last frame of every turn)
  - {"type": "error",    "detail": str, "fatal": bool}
"""
import asyncio
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from agents.workflow_agent.agent import begin_session, process_turn
from agents.workflow_agent.provider_tool import get_active_providers, provider_name
from agents.workflow_agent.state import WorkflowAgentState
from configs import logger
from db_helpers.database import get_db
from db_helpers.repository.auth_repository.dependencies import get_connection_org_id

router = APIRouter(tags=["workflow-agent"])

# Bound the in-memory transcript on a long-lived connection.
MAX_TURNS = 40
MAX_MESSAGE_CHARS = 4000


async def _emit(websocket: WebSocket, state: WorkflowAgentState,
                events: list[dict[str, Any]], providers: dict[str, str]) -> None:
    """Send a turn's events, then the state frame.

    Providers are reported by display name; the labels the fetcher needs stay inside
    the workflow graph.
    """
    for event in events:
        await websocket.send_json(event)
    snapshot = state.model_dump(exclude={"history", "proposed_queries"})
    snapshot["providers"] = [provider_name(p, providers) for p in state.providers]
    await websocket.send_json({"type": "state", "state": snapshot})


@router.websocket("/ws/workflow-agent")
async def workflow_agent_stream(
    websocket: WebSocket,
    db: Session = Depends(get_db),
    org_id: int = Depends(get_connection_org_id),
) -> None:
    await websocket.accept()
    state = WorkflowAgentState()
    turns = 0

    try:
        providers = await asyncio.to_thread(get_active_providers, db, org_id)
        logger.info(f"Workflow agent started for org_id={org_id}: {sorted(providers.values())}")
        await websocket.send_json({"type": "ready", "providers": providers})
        await _emit(websocket, state, begin_session(state), providers)

        while True:
            frame = await websocket.receive_json()
            frame_type = frame.get("type") if isinstance(frame, dict) else None

            if frame_type == "close":
                return
            if frame_type == "reset":
                state = WorkflowAgentState()
                turns = 0
                await _emit(websocket, state, begin_session(state), providers)
                continue
            if frame_type == "attach_file":
                # The client uploads first and sends the resulting id, so the graph
                # can carry a real file_upload_id the tagging pipeline can read.
                name = frame.get("name") if isinstance(frame, dict) else None
                upload_id = frame.get("file_upload_id") if isinstance(frame, dict) else None
                if isinstance(upload_id, str) and upload_id.strip():
                    state.source_type = "file"
                    state.file_upload_id = upload_id.strip()
                    state.file_name = name.strip() if isinstance(name, str) else ""
                else:
                    state.source_type = "api"
                    state.file_upload_id = ""
                    state.file_name = ""
                await _emit(websocket, state, [], providers)
                continue

            text = frame.get("text") if isinstance(frame, dict) else None
            if not isinstance(text, str) or not text.strip():
                await websocket.send_json(
                    {"type": "error", "detail": "Send a non-empty 'text'.", "fatal": False}
                )
                continue
            if len(text) > MAX_MESSAGE_CHARS:
                await websocket.send_json({
                    "type": "error",
                    "detail": f"Message too long (max {MAX_MESSAGE_CHARS} characters).",
                    "fatal": False,
                })
                continue

            turns += 1
            if turns > MAX_TURNS:
                await websocket.send_json({
                    "type": "error",
                    "detail": "This conversation has gone on too long. Send 'reset' to start over.",
                    "fatal": False,
                })
                turns = MAX_TURNS
                continue

            events = await asyncio.to_thread(process_turn, state, text.strip(), providers)
            await _emit(websocket, state, events, providers)

    except WebSocketDisconnect:
        logger.info(f"Workflow agent websocket disconnected (org_id={org_id})")
    except ValueError as exc:
        # Malformed (non-JSON) frame — receive_json raises before we can inspect it.
        logger.warning(f"Workflow agent received an unreadable frame: {exc}")
        try:
            await websocket.send_json(
                {"type": "error", "detail": "Expected a JSON frame.", "fatal": True}
            )
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("Workflow agent websocket failed")
        try:
            await websocket.send_json(
                {"type": "error", "detail": f"Agent failed: {exc}", "fatal": True}
            )
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
