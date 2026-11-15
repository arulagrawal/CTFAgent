from pydantic import BaseModel, HttpUrl, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
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
    url: Optional[HttpUrl] = None
    host: Optional[str] = None
    port: Optional[int] = None
    flag_format: str = "flag{.*}"
    challenge_type: Optional[ChallengeType] = None
    challenge_name: Optional[str] = None
    description: Optional[str] = None
    timeout: int = 600  # 10 minutes default


class AgentAction(BaseModel):
    id: Optional[str] = None
    timestamp: datetime
    action_type: str
    command: Optional[str] = None
    output: Optional[str] = None
    reasoning: Optional[str] = None
    success: bool = True


class ChallengeSession(BaseModel):
    session_id: str
    challenge_url: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    challenge_name: Optional[str] = None
    challenge_type: Optional[ChallengeType] = None
    flag_format: str
    status: ChallengeStatus
    created_at: datetime
    updated_at: datetime
    actions: List[AgentAction] = Field(default_factory=list)
    found_flag: Optional[str] = None
    container_id: Optional[str] = None
    error_message: Optional[str] = None
    uploaded_files: List[str] = Field(
        default_factory=list
    )  # Filenames available in /workspace/handouts
    estimated_cost_usd: Optional[float] = None
    model: str = "gpt-6-luna"
    reasoning_effort: Optional[str] = None
    usage: Dict[str, Any] = Field(default_factory=dict)


class WebSocketMessage(BaseModel):
    type: str  # "action", "status", "flag_found", "error"
    session_id: str
    data: Dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WriteupResponse(BaseModel):
    writeup: str
    solve_script: Optional[str] = None
