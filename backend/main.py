import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List
import json

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

from .models import (
    ChallengeRequest, 
    ChallengeSession, 
    ChallengeStatus, 
    AgentAction,
    WebSocketMessage
)
from .ai_agent import CTFAgent
from .docker_manager import DockerManager

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize FastAPI app
app = FastAPI(
    title="CTF Agent",
    description="AI-powered CTF challenge solving agent",
    version="1.0.0"
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

# Global state
sessions: Dict[str, ChallengeSession] = {}
websocket_connections: Dict[str, WebSocket] = {}

# Initialize AI agent
try:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY environment variable is required")
    
    ai_agent = CTFAgent(
        openai_api_key=openai_api_key,
        model=os.getenv("OPENAI_MODEL", "gpt-5-mini")
    )
except Exception as e:
    logger.error(f"Failed to initialize AI agent: {e}")
    ai_agent = None


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
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "ai_agent_ready": ai_agent is not None,
        "active_sessions": len(sessions)
    }


@app.post("/challenges/start")
async def start_challenge(request: ChallengeRequest):
    """Start a new CTF challenge solving session."""
    if not ai_agent:
        raise HTTPException(status_code=500, detail="AI agent not initialized")
    
    session_id = str(uuid.uuid4())
    
    # Create session
    session = ChallengeSession(
        session_id=session_id,
        challenge_url=str(request.url),
        flag_format=request.flag_format,
        status=ChallengeStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc)
    )
    
    sessions[session_id] = session
    
    # Start solving in background
    asyncio.create_task(solve_challenge_background(session_id, request))
    
    return {"session_id": session_id, "status": "started"}


@app.get("/challenges/{session_id}")
async def get_challenge_status(session_id: str):
    """Get the status of a challenge solving session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return sessions[session_id]


@app.get("/challenges/{session_id}/actions")
async def get_challenge_actions(session_id: str):
    """Get all actions for a challenge session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    return {"actions": sessions[session_id].actions}


@app.post("/challenges/{session_id}/stop")
async def stop_challenge(session_id: str):
    """Stop a running challenge session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session = sessions[session_id]
    if session.status == ChallengeStatus.RUNNING:
        # Clean up container
        if session.container_id:
            try:
                await ai_agent.docker_manager.cleanup_container(session_id)
            except Exception as e:
                logger.error(f"Failed to cleanup container for session {session_id}: {e}")
        
        session.status = ChallengeStatus.FAILED
        session.updated_at = datetime.now(timezone.utc)
        session.error_message = "Stopped by user"
    
    return {"status": "stopped"}


@app.delete("/challenges/{session_id}")
async def delete_challenge(session_id: str):
    """Delete a challenge session."""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Clean up container if exists
    session = sessions[session_id]
    if session.container_id:
        try:
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


async def solve_challenge_background(session_id: str, request: ChallengeRequest):
    """Background task to solve challenge."""
    session = sessions[session_id]
    session.status = ChallengeStatus.RUNNING
    session.updated_at = datetime.now(timezone.utc)
    
    # WebSocket callback for real-time updates
    async def websocket_callback(event_type: str, data: dict):
        # Update session actions in real-time
        if event_type == "action" and "timestamp" in data:
            action = AgentAction(**data)
            session.actions.append(action)
            session.updated_at = datetime.now(timezone.utc)
            logger.info(f"Added action to session {session_id}: {data.get('action_type', 'unknown')}")
        
        message = WebSocketMessage(
            type=event_type,
            session_id=session_id,
            data=data
        )
        logger.info(f"Sending WebSocket message: {event_type} for session {session_id}")
        await websocket_manager.send_message(session_id, message)
    
    try:
        # Start solving
        actions = await ai_agent.solve_challenge(
            session_id=session_id,
            challenge_url=str(request.url),
            flag_format=request.flag_format,
            challenge_type=request.challenge_type,
            callback=websocket_callback
        )
        
        # Update session with results
        session.actions = actions
        session.updated_at = datetime.now(timezone.utc)
        
        # Check if flag was found
        flag_found = any(action.action_type == "flag_found" for action in actions)
        if flag_found:
            flag_action = next(action for action in actions if action.action_type == "flag_found")
            session.found_flag = flag_action.output
            session.status = ChallengeStatus.COMPLETED
        else:
            session.status = ChallengeStatus.FAILED
            session.error_message = "Flag not found within time limit"
        
    except Exception as e:
        logger.error(f"Challenge solving failed for session {session_id}: {e}")
        session.status = ChallengeStatus.FAILED
        session.error_message = str(e)
        session.updated_at = datetime.now(timezone.utc)
        
        # Send error via WebSocket
        error_message = WebSocketMessage(
            type="error",
            session_id=session_id,
            data={"error": str(e)}
        )
        await websocket_manager.send_message(session_id, error_message)
    
    finally:
        # Clean up container
        try:
            await ai_agent.docker_manager.cleanup_container(session_id)
        except Exception as e:
            logger.error(f"Failed to cleanup container for session {session_id}: {e}")


if __name__ == "__main__":
    import uvicorn
    
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", 8000))
    debug = os.getenv("DEBUG", "false").lower() == "true"
    
    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=debug,
        log_level="info"
    )
