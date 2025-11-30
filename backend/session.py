import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .models import ChallengeType


@dataclass
class Session:
    """Encapsulates all per-session agent state."""

    session_id: str
    model: str
    api_string: str
    reasoning_effort: str = "low"
    challenge_type: Optional[ChallengeType] = None
    challenge_name: Optional[str] = None
    handout_dir_ready: bool = False
    handouts: List[str] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    last_response_id: Optional[str] = None
    deadline: Optional[float] = None

    def set_model(self, model: str, api_string: str) -> None:
        self.model = model
        self.api_string = api_string
        if not self.usage:
            self.reset_usage()
        else:
            self.usage["model"] = model

    def set_reasoning_effort(
        self, effort: Optional[str], allowed: Tuple[str, ...], default: str
    ) -> None:
        candidate = (effort or "").strip().lower()
        if candidate not in allowed:
            candidate = default
        self.reasoning_effort = candidate

    def set_deadline(self, timeout_seconds: Optional[int]) -> None:
        if timeout_seconds and timeout_seconds > 0:
            self.deadline = time.monotonic() + float(timeout_seconds)
        else:
            self.deadline = None

    def remaining_time(self) -> Optional[float]:
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def command_timeout(self, buffer: float = 0.5) -> Optional[float]:
        remaining = self.remaining_time()
        if remaining is None:
            return None
        if remaining <= buffer:
            return max(0.1, remaining)
        return remaining - buffer

    def reset_usage(self) -> None:
        self.usage = {
            "model": self.model,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def update_usage_from_response(self, response: Any) -> None:
        try:
            usage = getattr(response, "usage", None)
            if not usage:
                return

            input_tokens = getattr(usage, "input_tokens", 0) or 0
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", input_tokens + output_tokens)

            stats = self.usage or {}
            stats.setdefault("model", self.model)
            stats.setdefault("requests", 0)
            stats.setdefault("input_tokens", 0)
            stats.setdefault("output_tokens", 0)
            stats.setdefault("total_tokens", 0)

            stats["requests"] += 1
            stats["input_tokens"] += int(input_tokens)
            stats["output_tokens"] += int(output_tokens)
            stats["total_tokens"] += int(total_tokens)

            self.usage = stats
        except Exception:
            # Best effort; ignore metering errors
            pass

    def set_handouts(self, files: Optional[List[str]]) -> None:
        self.handouts = []
        if not files:
            return
        for name in files:
            self.add_handout(name)

    def add_handout(self, filename: str) -> None:
        if not filename:
            return
        if filename not in self.handouts:
            self.handouts.append(filename)

    def mark_handout_dir_ready(self) -> None:
        self.handout_dir_ready = True

