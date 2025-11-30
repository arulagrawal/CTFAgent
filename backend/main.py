import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Dict, List
import json
import re

from fastapi import (
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    Request,
    UploadFile,
    File,
    Form,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from fastapi.encoders import jsonable_encoder
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel

from .models import (
    ChallengeRequest,
    ChallengeSession,
    ChallengeStatus,
    ChallengeType,
    AgentAction,
    WebSocketMessage,
)
from .ai_agent import CTFAgent, REASONING_EFFORT_CHOICES
from .docker_manager import DockerManager
from .llm_models import is_valid_model, get_api_string, get_available_models

DEFAULT_FLAG_FORMAT = "SCF2024-[A-Za-z0-9-_]{10,}"
FLAG_FORMAT_PATH = Path("data/settings/flag_format.txt")
flag_format_lock = asyncio.Lock()


def _ensure_flag_format_path():
    FLAG_FORMAT_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load_flag_format_from_disk() -> str:
    try:
        if FLAG_FORMAT_PATH.exists():
            value = FLAG_FORMAT_PATH.read_text(encoding="utf-8").strip()
            return value or DEFAULT_FLAG_FORMAT
    except Exception as e:
        logger.warning(f"Failed to load flag format from disk: {e}")
    return DEFAULT_FLAG_FORMAT


def _save_flag_format_to_disk(value: str):
    try:
        _ensure_flag_format_path()
        FLAG_FORMAT_PATH.write_text(value, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to persist flag format: {e}")


current_flag_format = _load_flag_format_from_disk()


def get_current_flag_format() -> str:
    return current_flag_format or DEFAULT_FLAG_FORMAT


class FlagFormatUpdate(BaseModel):
    flag_format: str

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="CTF Agent",
    description="AI-powered CTF challenge solving agent",
    version="1.0.0",
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup templates and static files
templates = Jinja2Templates(directory="backend/templates")
app.mount("/static", StaticFiles(directory="backend/static"), name="static")


STATIC_VERSION = os.getenv("STATIC_VERSION") or str(int(datetime.now(timezone.utc).timestamp()))
templates.env.globals["STATIC_VERSION"] = STATIC_VERSION

# Global state
sessions: Dict[str, ChallengeSession] = {}
websocket_connections: Dict[str, WebSocket] = {}
# Track background tasks by session for cancellation
background_tasks: Dict[str, asyncio.Task] = {}

# Initialize AI agent
try:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY environment variable is required")

    model_from_env = os.getenv("OPENAI_MODEL", "gpt-5-mini")
    if not is_valid_model(model_from_env):
        logger.warning(f"Invalid model '{model_from_env}' from OPENAI_MODEL env var, using 'gpt-5-mini'")
        model_from_env = "gpt-5-mini"

    ai_agent = CTFAgent(
        openai_api_key=openai_api_key, model=model_from_env
    )
except Exception as e:
    logger.error(f"Failed to initialize AI agent: {e}")
    ai_agent = None
    sys.exit(1)


class WebSocketManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        logger.info(f"WebSocket connected for session {session_id}")

    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]
            logger.info(f"WebSocket disconnected for session {session_id}")

    async def send_message(self, session_id: str, message: WebSocketMessage):
        if session_id in self.active_connections:
            try:
                await self.active_connections[session_id].send_text(message.json())
            except Exception as e:
                logger.error(f"Failed to send WebSocket message to {session_id}: {e}")
                self.disconnect(session_id)


websocket_manager = WebSocketManager()


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "default_flag_format": get_current_flag_format()},
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Render the multi-session dashboard."""
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "default_flag_format": get_current_flag_format()},
    )


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "ai_agent_ready": ai_agent is not None,
        "active_sessions": len(sessions),
    }


@app.get("/challenges")
async def list_challenges(status: ChallengeStatus | None = None):
    """Return all challenge sessions, optionally filtered by status."""
    items = list(sessions.values())
    if status:
        items = [session for session in items if session.status == status]

    # Sort by newest first for easier scanning on the dashboard
    items.sort(key=lambda s: s.updated_at, reverse=True)
    return {"sessions": jsonable_encoder(items)}


@app.get("/settings/flag-format")
async def get_flag_format_setting():
    return {"flag_format": get_current_flag_format()}


@app.post("/settings/flag-format")
async def update_flag_format_setting(payload: FlagFormatUpdate):
    global current_flag_format
    value = (payload.flag_format or "").strip() or DEFAULT_FLAG_FORMAT
    try:
        re.compile(value)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid flag regex: {e}")

    async with flag_format_lock:
        current_flag_format = value
        _save_flag_format_to_disk(value)

    logger.info(f"Updated default flag format to: {value}")
    return {"flag_format": current_flag_format}


@app.post("/challenges/start")
async def start_challenge(
    url: str | None = Form(None),
    host: str | None = Form(None),
    port: int | None = Form(None),
    flag_format: str | None = Form(None),
    challenge_type: str | None = Form(None),
    challenge_name: str | None = Form(None),
    description: str | None = Form(None),
    timeout: int = Form(600),
    model: str = Form("gpt-5-mini"),
    reasoning_effort: str = Form("low"),
    handouts: List[UploadFile] | None = None,
):
    """Start a new CTF challenge solving session."""
    if not ai_agent:
        raise HTTPException(status_code=500, detail="AI agent not initialized")

    session_id = str(uuid.uuid4())

    # Validate inputs: allow URL or socket or handout-only
    if not url and not (host and port) and not handouts:
        raise HTTPException(
            status_code=400,
            detail="Provide a URL, or a host+port, or at least one handout.",
        )

    # Validate flag regex early
    if not flag_format:
        flag_format = get_current_flag_format()

    try:
        re.compile(flag_format)
    except re.error as e:
        raise HTTPException(status_code=400, detail=f"Invalid flag regex: {e}")

    # Validate model name
    if not is_valid_model(model):
        available_models = ', '.join(get_available_models().keys())
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model '{model}'. Available models: {available_models}"
        )

    # Normalize reasoning effort selection
    reasoning_effort_value = (reasoning_effort or "").strip().lower()
    if reasoning_effort_value not in REASONING_EFFORT_CHOICES:
        reasoning_effort_value = REASONING_EFFORT_CHOICES[0]

    # Normalize timeout with sane bounds
    timeout_value = timeout or 600
    timeout_value = max(60, min(int(timeout_value), 1800))

    # Create session
    session = ChallengeSession(
        session_id=session_id,
        challenge_url=str(url) if url else None,
        host=host,
        port=port,
        challenge_name=challenge_name.strip() if challenge_name else None,
        flag_format=flag_format,
        found_flag=None,
        status=ChallengeStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        uploaded_files=[],
    )

    sessions[session_id] = session

    # Save uploaded handouts (if any)
    if handouts:
        base_dir = os.path.join("data", "handouts", session_id)
        os.makedirs(base_dir, exist_ok=True)
        for file in handouts:
            try:
                content = await file.read()
                safe_name = file.filename or f"upload_{uuid.uuid4().hex}"
                dest_path = os.path.join(base_dir, safe_name)
                with open(dest_path, "wb") as f:
                    f.write(content)
                session.uploaded_files.append(safe_name)
            except Exception as e:
                logger.error(f"Failed to save handout {file.filename}: {e}")

    # Start solving in background
    # Start and track background solving task for later cancellation
    task = asyncio.create_task(
        solve_challenge_background_form(
            session_id,
            url,
            host,
            port,
            flag_format,
            challenge_type,
            challenge_name,
            description,
        timeout_value,
            model,
            reasoning_effort_value,
        )
    )
    background_tasks[session_id] = task

    return {"session_id": session_id, "status": "started"}


@app.post("/challenges/{session_id}/upload")
async def upload_handout(session_id: str, file: UploadFile = File(...)):
    """Upload a handout file and make it available under /workspace/handouts inside the container."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions[session_id]

    # Save to backend storage under data/handouts/{session_id}/
    base_dir = os.path.join("data", "handouts", session_id)
    os.makedirs(base_dir, exist_ok=True)
    safe_name = file.filename or f"upload_{uuid.uuid4().hex}"
    dest_path = os.path.join(base_dir, safe_name)
    try:
        content = await file.read()
        with open(dest_path, "wb") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    # Track file in session
    if safe_name not in session.uploaded_files:
        session.uploaded_files.append(safe_name)
    session.updated_at = datetime.now(timezone.utc)

    # If container is running, copy immediately
    try:
        if not ai_agent:
            raise RuntimeError("AI agent not initialized")
        await ai_agent.ensure_handout_dir(session_id)
        ok = await ai_agent.copy_host_file_to_container(
            session_id, dest_path, f"/workspace/handouts/{safe_name}"
        )
        if not ok:
            logger.warning(
                f"Failed to copy {safe_name} into container for session {session_id}"
            )
        else:
            ai_agent.note_uploaded_file(session_id, safe_name)
    except Exception as e:
        logger.warning(f"Could not copy handout into container now: {e}")

    return {"status": "uploaded", "filename": safe_name}


@app.get("/challenges/{session_id}")
async def get_challenge_status(session_id: str):
    """Get the status of a challenge solving session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    return jsonable_encoder(sessions[session_id])


@app.get("/challenges/{session_id}/actions")
async def get_challenge_actions(session_id: str):
    """Get all actions for a challenge session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"actions": jsonable_encoder(sessions[session_id].actions)}


@app.post("/challenges/{session_id}/stop")
async def stop_challenge(session_id: str):
    """Stop a running challenge session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    session = sessions[session_id]
    if session.status == ChallengeStatus.RUNNING:
        # 1) Hard-kill the container immediately (non-blocking)
        try:
            if ai_agent:
                asyncio.create_task(ai_agent.docker_manager.kill_container(session_id))
        except Exception as e:
            logger.warning(
                f"Failed to schedule container kill for session {session_id}: {e}"
            )

        # 2) Update session state immediately
        session.status = ChallengeStatus.FAILED
        session.updated_at = datetime.now(timezone.utc)
        session.error_message = "Stopped by user"

        # 3) Notify frontend right away
        try:
            cancel_message = WebSocketMessage(
                type="status",
                session_id=session_id,
                data={"status": session.status, "error": session.error_message},
            )
            asyncio.create_task(websocket_manager.send_message(session_id, cancel_message))
        except Exception as e:
            logger.debug(f"WebSocket notify on stop failed for {session_id}: {e}")

        # 4) Cancel background task but do not await (avoid blocking on long exec)
        task = background_tasks.get(session_id)
        if task and not task.done():
            task.cancel()
        background_tasks.pop(session_id, None)

    return {"status": "stopped"}


@app.delete("/challenges/{session_id}")
async def delete_challenge(session_id: str):
    """Delete a challenge session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")

    # Cancel any running background task
    task = background_tasks.get(session_id)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.info(f"Background task for session {session_id} cancelled (delete)")
        except Exception as e:
            logger.warning(
                f"Background task for session {session_id} error on delete: {e}"
            )
    background_tasks.pop(session_id, None)

    # Clean up container if exists
    session = sessions[session_id]
    if session.container_id:
        try:
            if ai_agent:
                await ai_agent.docker_manager.cleanup_container(session_id)
        except Exception as e:
            logger.error(f"Failed to cleanup container for session {session_id}: {e}")

    # Remove session
    del sessions[session_id]

    return {"status": "deleted"}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket endpoint for real-time updates."""
    await websocket_manager.connect(websocket, session_id)

    try:
        while True:
            # Keep connection alive
            data = await websocket.receive_text()

            # Echo back for now (could handle client commands)
            await websocket.send_text(f"Echo: {data}")

    except WebSocketDisconnect:
        websocket_manager.disconnect(session_id)


async def solve_challenge_background_form(
    session_id: str,
    url: str | None,
    host: str | None,
    port: int | None,
    flag_format: str,
    challenge_type: str | None,
    challenge_name: str | None,
    description: str | None,
    timeout: int,
    model: str,
    reasoning_effort: str,
):
    """Background task to solve challenge."""
    timeout = timeout or 600
    timeout = max(1, timeout)
    session = sessions[session_id]
    session.status = ChallengeStatus.RUNNING
    session.updated_at = datetime.now(timezone.utc)

    # WebSocket callback for real-time updates
    async def websocket_callback(event_type: str, data: dict):
        # Maintain session actions and forward messages
        try:
            if event_type in ("action", "action_create") and "timestamp" in data:
                action = AgentAction(**data)
                # Ensure id presence
                if not getattr(action, "id", None):
                    import uuid as _uuid

                    action.id = _uuid.uuid4().hex
                session.actions.append(action)
                session.updated_at = datetime.now(timezone.utc)
                logger.info(
                    f"Added action to session {session_id}: {data.get('action_type', 'unknown')}"
                )
            elif event_type == "action_complete" and data.get("id"):
                # Update the existing action's output/success
                for a in session.actions:
                    if getattr(a, "id", None) == data["id"]:
                        final_output = data.get("final_output")
                        if final_output is not None:
                            a.output = final_output
                        if "success" in data:
                            a.success = bool(data["success"])  # type: ignore[assignment]
                        session.updated_at = datetime.now(timezone.utc)
                        break
        except Exception as e:
            logger.debug(f"websocket_callback state update error: {e}")

        message = WebSocketMessage(type=event_type, session_id=session_id, data=data)
        logger.info(f"Sending WebSocket message: {event_type} for session {session_id}")
        asyncio.create_task(websocket_manager.send_message(session_id, message))

    try:
        # Start solving
        if not ai_agent:
            raise RuntimeError("AI agent not initialized")

        # Ensure any pre-uploaded handouts are copied into container before starting
        if session.uploaded_files:
            base_dir = os.path.join("data", "handouts", session_id)
            try:
                await ai_agent.ensure_handout_dir(session_id)
            except Exception:
                pass
            for name in session.uploaded_files:
                host_path = os.path.join(base_dir, name)
                dest_path = f"/workspace/handouts/{name}"
                try:
                    ok = await ai_agent.copy_host_file_to_container(
                        session_id, host_path, dest_path
                    )
                    if not ok:
                        logger.warning(
                            f"Pre-copy failed for {name} in session {session_id}"
                        )
                except Exception as e:
                    logger.warning(f"Error copying handout {name} before start: {e}")

        # Normalize challenge type string to enum when possible
        ct_enum = None
        try:
            if challenge_type:
                # Accept case-insensitive input matching enum values
                ct_enum = ChallengeType(challenge_type.lower())
        except Exception:
            ct_enum = None

        actions = await asyncio.wait_for(
            ai_agent.solve_challenge(
                session_id=session_id,
                challenge_url=str(url) if url else None,
                socket_host=host,
                socket_port=port,
                flag_format=flag_format,
                challenge_type=ct_enum,  # type: ignore[arg-type]
                challenge_name=challenge_name,
                description=description,
                uploaded_files=session.uploaded_files,
                callback=websocket_callback,
                reasoning_effort=reasoning_effort,
                model=model,
                session_timeout=timeout,
            ),
            timeout=timeout,
        )

        # Update session with results
        session.actions = actions
        session.updated_at = datetime.now(timezone.utc)
        # Store estimated cost on session if available
        try:
            usage_action = (
                ai_agent.build_usage_summary_action(session_id) if ai_agent else None
            )
            if usage_action and usage_action.output:
                # Extract cost from summary line
                m = re.search(r"Estimated cost: \$(\d+\.\d+)", usage_action.output)
                if m:
                    session.estimated_cost_usd = float(m.group(1))
        except Exception:
            pass

        # Check if flag was found
        flag_found = any(action.action_type == "flag_found" for action in actions)
        if flag_found:
            flag_action = next(
                action for action in actions if action.action_type == "flag_found"
            )
            session.found_flag = flag_action.output
            session.status = ChallengeStatus.COMPLETED
        else:
            session.status = ChallengeStatus.FAILED
            session.error_message = "Flag not found within time limit"

    except asyncio.TimeoutError:
        logger.error(f"Challenge solving timed out for session {session_id}")
        session.status = ChallengeStatus.FAILED
        session.error_message = f"Timed out after {timeout} seconds"
        session.updated_at = datetime.now(timezone.utc)

        timeout_action = AgentAction(
            timestamp=datetime.now(timezone.utc),
            action_type="session_timeout",
            output=f"Session timed out after {timeout} seconds.",
            reasoning="Global session timeout reached",
            success=False,
        )
        session.actions.append(timeout_action)
        asyncio.create_task(
            websocket_manager.send_message(
                session_id,
                WebSocketMessage(
                    type="action",
                    session_id=session_id,
                    data=timeout_action.dict(),
                ),
            )
        )
        asyncio.create_task(
            websocket_manager.send_message(
                session_id,
                WebSocketMessage(
                    type="status",
                    session_id=session_id,
                    data={"status": session.status, "error": session.error_message},
                ),
            )
        )

        try:
            if ai_agent:
                usage_action = ai_agent.build_usage_summary_action(session_id)
                if usage_action:
                    session.actions.append(usage_action)
                    asyncio.create_task(
                        websocket_manager.send_message(
                            session_id,
                            WebSocketMessage(
                                type="action",
                                session_id=session_id,
                                data=usage_action.dict(),
                            ),
                        )
                    )
                    if usage_action.output:
                        m = re.search(r"Estimated cost: \$(\d+\.\d+)", usage_action.output)
                        if m:
                            session.estimated_cost_usd = float(m.group(1))
        except Exception:
            pass

    except asyncio.CancelledError:
        logger.info(f"Background solving cancelled for session {session_id}")
        session.status = ChallengeStatus.FAILED
        session.error_message = "Stopped by user"
        session.updated_at = datetime.now(timezone.utc)
        # Emit usage summary if available
        try:
            if ai_agent:
                usage_action = ai_agent.build_usage_summary_action(session_id)
                if usage_action:
                    session.actions.append(usage_action)
                    asyncio.create_task(
                        websocket_manager.send_message(
                            session_id,
                            WebSocketMessage(
                                type="action",
                                session_id=session_id,
                                data=usage_action.dict(),
                            ),
                        )
                    )
                    # Extract and store cost on session
                    if usage_action.output:
                        m = re.search(
                            r"Estimated cost: \$(\d+\.\d+)", usage_action.output
                        )
                        if m:
                            session.estimated_cost_usd = float(m.group(1))
        except Exception:
            pass
        # Attempt cleanup
        try:
            if ai_agent:
                await ai_agent.docker_manager.cleanup_container(session_id)
        except Exception as e:
            logger.error(
                f"Failed to cleanup container after cancel for session {session_id}: {e}"
            )
        # Inform frontend via WebSocket
        try:
            cancel_message = WebSocketMessage(
                type="status",
                session_id=session_id,
                data={"status": session.status, "error": session.error_message},
            )
            asyncio.create_task(websocket_manager.send_message(session_id, cancel_message))
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"Challenge solving failed for session {session_id}: {e}")
        session.status = ChallengeStatus.FAILED
        session.error_message = str(e)
        session.updated_at = datetime.now(timezone.utc)
        # Store estimated cost on session if available
        try:
            if ai_agent:
                usage_action = ai_agent.build_usage_summary_action(session_id)
                if usage_action and usage_action.output:
                    m = re.search(r"Estimated cost: \$(\d+\.\d+)", usage_action.output)
                    if m:
                        session.estimated_cost_usd = float(m.group(1))
        except Exception:
            pass

        # Send error via WebSocket
        error_message = WebSocketMessage(
            type="error", session_id=session_id, data={"error": str(e)}
        )
        asyncio.create_task(websocket_manager.send_message(session_id, error_message))

    finally:
        # Clean up container
        try:
            if ai_agent:
                await ai_agent.docker_manager.cleanup_container(session_id)
        except Exception as e:
            logger.error(f"Failed to cleanup container for session {session_id}: {e}")
        try:
            if ai_agent:
                ai_agent.clear_session_state(session_id)
        except Exception as e:
            logger.error(f"Failed to clear session state for {session_id}: {e}")
        # Remove task from registry if present
        background_tasks.pop(session_id, None)


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))
    debug = os.getenv("DEBUG", "false").lower() == "true"

    uvicorn.run(
        "backend.main:app", host=host, port=port, reload=debug, log_level="info"
    )
