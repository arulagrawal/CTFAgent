from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum


class ChallengeType(str, Enum):
    WEB = "web"
    CRYPTO = "crypto"
    REVERSE = "reverse"
    PWNABLE = "pwnable"
    FORENSICS = "forensics"
    MISC = "misc"


class ChallengeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ChallengeRequest(BaseModel):
    url: HttpUrl
    flag_format: str = "flag{.*}"
    challenge_type: Optional[ChallengeType] = None
    description: Optional[str] = None
    timeout: int = 300  # 5 minutes default


class AgentAction(BaseModel):
    timestamp: datetime
    action_type: str
    command: Optional[str] = None
    output: Optional[str] = None
    reasoning: Optional[str] = None
    success: bool = True


class ChallengeSession(BaseModel):
    session_id: str
    challenge_url: str
    flag_format: str
    status: ChallengeStatus
    created_at: datetime
    updated_at: datetime
    actions: List[AgentAction] = []
    found_flag: Optional[str] = None
    container_id: Optional[str] = None
    error_message: Optional[str] = None


class WebSocketMessage(BaseModel):
    type: str  # "action", "status", "flag_found", "error"
    session_id: str
    data: Dict[str, Any]
    timestamp: datetime = datetime.now()
