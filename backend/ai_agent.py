import asyncio
import logging
import re
import json
from typing import List, Dict, Any, Optional, Callable
import os
from datetime import datetime, timezone
from openai import AsyncOpenAI

from .models import AgentAction, ChallengeType
from .docker_manager import DockerManager
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# Substrings that invalidate a matched flag candidate (case-insensitive)
FLAG_CANDIDATE_BANNED_SUBSTRINGS = ("fake", "redacted")


class SingleCommand(BaseModel):
    command: str
    reasoning: str


class CTFAgent:
    def __init__(self, openai_api_key: str, model: str = "gpt-5-mini"):
        self.client = AsyncOpenAI(api_key=openai_api_key)
        self.model = model
        self.docker_manager = DockerManager()
        self._ensured_handout_dir: Dict[str, bool] = {}
        self._session_handouts: Dict[str, List[str]] = {}
        # Track OpenAI usage per session
        self._session_usage: Dict[str, Dict[str, Any]] = {}

    def _init_usage(self, session_id: str) -> None:
        self._session_usage[session_id] = {
            "model": self.model,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def _update_usage_from_response(self, session_id: str, response: Any) -> None:
        try:
            stats = self._session_usage.setdefault(
                session_id,
                {
                    "model": self.model,
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                },
            )
            usage = getattr(response, "usage", None)
            if not usage:
                # Try alternative locations/shapes
                usage = getattr(getattr(response, "choices", [{}])[0], "usage", None)
            if not usage:
                return
            # Handle both Responses API and Chat Completions shapes
            input_tokens = getattr(usage, "input_tokens", None)
            if input_tokens is None:
                input_tokens = getattr(usage, "prompt_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", None)
            if output_tokens is None:
                output_tokens = getattr(usage, "completion_tokens", 0)
            total_tokens = getattr(usage, "total_tokens", None)
            if total_tokens is None:
                total_tokens = (input_tokens or 0) + (output_tokens or 0)

            stats["requests"] += 1
            stats["input_tokens"] += int(input_tokens or 0)
            stats["output_tokens"] += int(output_tokens or 0)
            stats["total_tokens"] += int(total_tokens or 0)
        except Exception:
            # Best effort; ignore errors in metering
            pass

    def _estimate_cost_usd(self, session_id: str) -> Optional[float]:
        stats = self._session_usage.get(session_id)
        if not stats:
            return None
        try:
            model_name = str(stats.get("model", self.model)).lower()
            # Default to gpt-5 pricing unless explicitly mini
            if "mini" in model_name or model_name.replace(" ", "").replace(
                "_", "-"
            ) in ("gpt-5-mini", "gpt5-mini"):
                price_in = 0.250  # USD per 1M input tokens
                price_out = 2.000  # USD per 1M output tokens
            else:
                price_in = 1.250  # USD per 1M input tokens
                price_out = 10.000  # USD per 1M output tokens

            cost = (stats["input_tokens"] / 1_000_000) * price_in + (
                stats["output_tokens"] / 1_000_000
            ) * price_out
            return round(cost, 6)
        except Exception:
            return None

    def _build_usage_summary_action(self, session_id: str) -> Optional[AgentAction]:
        stats = self._session_usage.get(session_id)
        if not stats:
            return None
        estimated_cost = self._estimate_cost_usd(session_id)
        cost_str = f"${estimated_cost:.6f}" if estimated_cost is not None else "N/A"
        summary = (
            f"Usage summary for model {stats.get('model', self.model)}\n"
            f"Requests: {stats.get('requests', 0)}\n"
            f"Input tokens: {stats.get('input_tokens', 0)}\n"
            f"Output tokens: {stats.get('output_tokens', 0)}\n"
            f"Total tokens: {stats.get('total_tokens', 0)}\n"
            f"Estimated cost: {cost_str}"
        )
        return AgentAction(
            timestamp=datetime.now(timezone.utc),
            action_type="usage_summary",
            output=summary,
            reasoning="Session token usage and cost estimate",
            success=True,
        )

    def build_usage_summary_action(self, session_id: str) -> Optional[AgentAction]:
        return self._build_usage_summary_action(session_id)

    async def ensure_handout_dir(self, session_id: str) -> None:
        """Ensure /workspace/handouts exists inside the container."""
        try:
            await self.docker_manager.execute_command(
                session_id, "mkdir -p /workspace/handouts"
            )
            self._ensured_handout_dir[session_id] = True
        except Exception:
            pass

    async def copy_host_file_to_container(
        self, session_id: str, host_path: str, container_path: str
    ) -> bool:
        """Copy a previously saved host file into the container at container_path (binary-safe)."""
        await self.ensure_handout_dir(session_id)
        return await self.docker_manager.copy_local_file_to_container(
            session_id, host_path, container_path
        )

    def note_uploaded_file(self, session_id: str, filename: str) -> None:
        files = self._session_handouts.setdefault(session_id, [])
        if filename not in files:
            files.append(filename)

    def _detect_challenge_type(self, url: str) -> Optional[str]:
        """Attempt to detect challenge type from URL patterns."""
        url_lower = url.lower()

        # Web challenge indicators
        web_indicators = [
            "web",
            "http",
            "www",
            "login",
            "admin",
            "upload",
            "php",
            "asp",
            "jsp",
        ]
        if any(indicator in url_lower for indicator in web_indicators):
            return "web"

        # Crypto challenge indicators
        crypto_indicators = ["crypto", "cipher", "encrypt", "decode", "hash"]
        if any(indicator in url_lower for indicator in crypto_indicators):
            return "crypto"

        # Reverse engineering indicators
        reverse_indicators = ["reverse", "binary", "exe", "crackme", "patch"]
        if any(indicator in url_lower for indicator in reverse_indicators):
            return "reverse"

        # Forensics indicators
        forensics_indicators = ["forensics", "memory", "dump", "pcap", "image", "disk"]
        if any(indicator in url_lower for indicator in forensics_indicators):
            return "forensics"

        # Network/Pwnable indicators
        network_indicators = ["pwn", "buffer", "overflow", "network", "tcp", "udp"]
        if any(indicator in url_lower for indicator in network_indicators):
            return "pwnable"

        # Default to web if it's an HTTP(S) URL
        if url_lower.startswith(("http://", "https://")):
            return "web"

        return None

    def _is_pyjail_context(
        self,
        description: Optional[str],
        uploaded_files: List[str],
        actions: List[AgentAction],
    ) -> bool:
        """Heuristically detect if this looks like a Python jail/REPL challenge."""
        try:
            desc = (description or "").lower()
            indicators = [
                "pyjail",
                "python jail",
                "python repl",
                "repl",
                "sandbox",
                "builtins",
                "__import__",
                ">>>",
                "traceback",
                "nameerror",
                "pdb",
            ]
            if any(tok in desc for tok in indicators):
                return True
            for name in uploaded_files:
                n = (name or "").lower()
                if n.endswith(".py") or ("jail" in n and "py" in n):
                    return True
            # Look at recent outputs for Python prompts/errors
            for action in actions[-5:]:
                out = (action.output or "").lower()
                if any(
                    tok in out
                    for tok in [
                        ">>>",
                        "traceback (most recent call last)",
                        "nameerror",
                        "attributeerror",
                        "builtins",
                        "pdb",
                    ]
                ):
                    return True
        except Exception:
            pass
        return False

    def _get_pyjail_guidance(self) -> str:
        lines = [
            "PYJAIL HINTS:",
            "- Try breakpoint() to enter pdb; from pdb, import os and system to execute commands.",
            "- Use builtins/__import__: import builtins; builtins.__import__('os').system('sh').",
            "- Use object.__subclasses__() to find Popen or file-like classes and pivot to exec/read.",
            "- Build blocked strings via chr()/bytes: ''.join(chr(x) for x in [111,115]) -> 'os'.",
            "- Send raw bytes when Unicode is filtered; use printf -e '\\xNN' or a short python3 script to send bytes.",
        ]
        return "\n".join(lines)

    async def _check_container_health(self, session_id: str) -> Optional[str]:
        """Check container health and return diagnostic information."""
        try:
            if session_id not in self.docker_manager.containers:
                return "Container not found"

            container = self.docker_manager.containers[session_id]
            container.reload()  # Refresh container state

            status_info = []
            status_info.append(f"Status: {container.status}")
            status_info.append(f"State: {container.attrs['State']['Status']}")

            if container.attrs["State"]["Status"] != "running":
                if "Error" in container.attrs["State"]:
                    status_info.append(f"Error: {container.attrs['State']['Error']}")
                if "ExitCode" in container.attrs["State"]:
                    status_info.append(
                        f"Exit Code: {container.attrs['State']['ExitCode']}"
                    )

            # Check network connectivity
            try:
                test_result = container.exec_run("ping -c 1 8.8.8.8")
                if test_result.exit_code != 0:
                    status_info.append("Network: No internet connectivity")
                else:
                    status_info.append("Network: Internet accessible")
            except Exception:
                status_info.append("Network: Unable to test connectivity")

            return " | ".join(status_info)

        except Exception as e:
            return f"Health check failed: {str(e)}"

    async def solve_challenge(
        self,
        session_id: str,
        challenge_url: Optional[str],
        socket_host: Optional[str] = None,
        socket_port: Optional[int] = None,
        flag_format: str = "flag{.*}",
        challenge_type: Optional[ChallengeType] = None,
        description: Optional[str] = None,
        uploaded_files: Optional[List[str]] = None,
        callback: Optional[Callable] = None,
    ) -> List[AgentAction]:
        """Main method to solve a CTF challenge."""
        actions = []

        try:
            # Initialize usage tracking
            self._init_usage(session_id)
            # Create container
            await self.docker_manager.create_container(session_id)
            # Ensure handouts dir exists
            await self.ensure_handout_dir(session_id)
            # Initialize session handouts
            self._session_handouts[session_id] = list(uploaded_files or [])
            # Copy any pre-uploaded handouts into container now that it exists
            if self._session_handouts[session_id]:
                base_dir = os.path.join("data", "handouts", session_id)
                for name in self._session_handouts[session_id]:
                    host_path = os.path.join(base_dir, name)
                    dest_path = f"/workspace/handouts/{name}"
                    try:
                        ok = await self.copy_host_file_to_container(
                            session_id, host_path, dest_path
                        )
                        if not ok:
                            logger.warning(
                                f"Failed to copy pre-uploaded handout {name} to container for session {session_id}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Error copying pre-uploaded handout {name}: {e}"
                        )

            # Send initial status update
            if callback:
                init_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="session_initialized",
                    output=(
                        (
                            f"CTF Agent session started. Target URL: {challenge_url}\n"
                            if challenge_url
                            else ""
                        )
                        + (
                            f"CTF Agent session started. Target SOCKET: {socket_host}:{socket_port}\n"
                            if socket_host and socket_port
                            else ""
                        )
                        + (
                            "CTF Agent session started. Handout-only challenge.\n"
                            if not challenge_url and not (socket_host and socket_port)
                            else ""
                        )
                        + f"Flag format: {flag_format}\nContainer created successfully"
                    ),
                    reasoning="Session initialization and container setup",
                    success=True,
                )
                actions.append(init_action)
                await callback("action", init_action.dict())

            # Start with challenge analysis - let AI decide what tools it needs
            analysis_prompt = self._build_analysis_prompt(
                url=challenge_url,
                flag_format=flag_format,
                challenge_type=challenge_type,
                description=description,
                uploaded_files=self._session_handouts.get(session_id, []),
                socket_host=socket_host,
                socket_port=socket_port,
            )
            analysis_actions = await self._execute_with_ai(
                session_id, analysis_prompt, actions, callback
            )
            actions.extend(analysis_actions)

            # Iterative solving process (strict one-by-one)
            max_iterations = 20
            for i in range(max_iterations):
                # Check if flag found
                flag = self._check_for_flag(actions, flag_format)
                if flag:
                    flag_action = AgentAction(
                        timestamp=datetime.now(timezone.utc),
                        action_type="flag_found",
                        output=f"Flag found: {flag}",
                        reasoning="Flag pattern matched in previous outputs",
                        success=True,
                    )
                    actions.append(flag_action)
                    if callback:
                        await callback("flag_found", flag_action.dict())
                    break

                # Get next single action from AI
                next_prompt = self._build_iteration_prompt(
                    actions=actions,
                    flag_format=flag_format,
                    iteration=i,
                    uploaded_files=self._session_handouts.get(session_id, []),
                    url=challenge_url,
                    socket_host=socket_host,
                    socket_port=socket_port,
                )
                iteration_actions = await self._execute_with_ai(
                    session_id, next_prompt, actions, callback
                )
                actions.extend(iteration_actions)

                # Add a small delay between iterations to ensure frontend updates
                await asyncio.sleep(1)

                # Prevent infinite loops
                if not iteration_actions:
                    break

            # Append usage summary before returning
            try:
                usage_action = self._build_usage_summary_action(session_id)
                if usage_action:
                    actions.append(usage_action)
                    if callback:
                        await callback("action", usage_action.dict())
            except Exception:
                pass
            return actions

        except asyncio.CancelledError:
            # Propagate cancellation so the background task can handle it
            raise
        except Exception as e:
            error_action = AgentAction(
                timestamp=datetime.now(timezone.utc),
                action_type="error",
                output=str(e),
                reasoning="Exception occurred during challenge solving",
                success=False,
            )
            actions.append(error_action)
            if callback:
                await callback("error", error_action.dict())
            # Append usage summary on error
            try:
                usage_action = self._build_usage_summary_action(session_id)
                if usage_action:
                    actions.append(usage_action)
                    if callback:
                        await callback("action", usage_action.dict())
            except Exception:
                pass
            return actions

    async def _execute_with_ai(
        self,
        session_id: str,
        prompt: str,
        previous_actions: List[AgentAction],
        callback: Optional[Callable] = None,
    ) -> List[AgentAction]:
        """Execute AI-generated commands and return actions."""
        actions = []

        try:
            # Use the provided prompt directly; it already contains any needed history
            input_text = str(prompt)

            # Call Responses API with structured output (SingleCommand)
            try:
                response = await self.client.responses.parse(
                    model=self.model,
                    instructions=self._get_system_prompt(),
                    input=[{"role": "user", "content": input_text}],
                    max_output_tokens=10000,
                    reasoning={"effort": "medium"},
                    text_format=SingleCommand,
                )
                # Track usage for Responses API
                self._update_usage_from_response(session_id, response)
            except Exception as e:
                logger.error(f"Responses API call failed: {e}")
                error_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="ai_error",
                    output=f"Responses API call failed: {e}",
                    reasoning="Structured output call failed",
                    success=False,
                )
                actions.append(error_action)
                if callback:
                    await callback("action", error_action.dict())
                return actions

            # Extract output text or structured parse
            ai_response = None
            try:
                sc = getattr(response, "output_parsed", None)
                if sc is not None and getattr(sc, "command", None):
                    ai_response = json.dumps(
                        {
                            "command": sc.command,
                            "reasoning": getattr(sc, "reasoning", "")
                            or "No reasoning provided",
                        }
                    )
            except Exception:
                ai_response = None
            if ai_response is None:
                try:
                    parts: List[str] = []
                    for out in getattr(response, "output", []) or []:
                        for content in getattr(out, "content", []) or []:
                            if getattr(content, "type", None) in (
                                "output_text",
                                "text",
                            ) and getattr(content, "text", None):
                                parts.append(content.text)
                    ai_response = "\n".join(parts)
                except Exception:
                    ai_response = ""

            logger.info(f"AI Responses output length: {len(ai_response or '')}")

            if not ai_response or ai_response.strip() == "":
                logger.warning("AI returned empty response from Responses API")
                error_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="ai_empty_response",
                    output="AI returned empty response using Responses API",
                    reasoning="AI hit token limits or returned empty content",
                    success=False,
                )
                actions.append(error_action)
                if callback:
                    await callback("action", error_action.dict())
                return actions

            # Parse AI response for a single command (prefer strict JSON)
            try:
                parsed = json.loads(ai_response)
                commands = []
                if isinstance(parsed, dict) and parsed.get("command"):
                    commands = [
                        {
                            "command": str(parsed.get("command", "")).strip(),
                            "reasoning": str(parsed.get("reasoning", "")).strip()
                            or "No reasoning provided",
                        }
                    ]
            except Exception:
                pass

            # If no commands were parsed, log the full AI response for debugging
            if not commands:
                logger.warning(
                    f"No commands extracted from AI response (length={len(ai_response or '')})."
                )

                # Create a more informative action with the full AI response
                error_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="ai_response_parsing_failed",
                    output=f"AI provided response but no commands could be extracted.\n\nFull AI Response:\n{ai_response}",
                    reasoning="The AI responded but the response format didn't match expected patterns for command extraction",
                    success=False,
                )
                actions.append(error_action)
                if callback:
                    await callback("action", error_action.dict())

            # Execute only one command per step
            for command_data in commands[:1]:
                command = command_data.get("command", "")
                reasoning = command_data.get("reasoning", "")

                if not command or not command.strip():
                    continue

                # Validate command doesn't contain clear JSON key artifacts
                if any(
                    artifact in command for artifact in ['"command":', '"reasoning":']
                ):
                    logger.warning(
                        f"Skipping malformed command that still includes JSON keys: {command}"
                    )
                    continue

                # Clean up any remaining quotes or artifacts (but preserve quotes inside the command)
                command = command.strip('"{}[]')

                # Replace placeholder targets with actual values when present in prompt
                # URL replacement
                if "TARGET_URL" in command or "TARGET_IP" in command:
                    actual_url_match = re.search(r"Target URL:\s*(\S+)", prompt)
                    if actual_url_match:
                        actual_url = actual_url_match.group(1)
                        command = command.replace("TARGET_URL", actual_url)
                        command = command.replace("TARGET_IP", actual_url)
                        logger.info(f"Replaced placeholder with actual URL: {command}")

                # Socket replacement
                if "TARGET_HOST" in command or "TARGET_PORT" in command:
                    host_match = re.search(r"Target SOCKET:\s*([^:\s]+):(\d+)", prompt)
                    if host_match:
                        actual_host = host_match.group(1)
                        actual_port = host_match.group(2)
                        command = command.replace("TARGET_HOST", actual_host)
                        command = command.replace("TARGET_PORT", actual_port)
                        logger.info(
                            f"Replaced placeholders with actual socket: {command}"
                        )

                # If command is reading a full file, encourage using cat (less confusion on output)
                if re.search(r"sed\s+-n\s+'1,\d+p'\s+\S+", command):
                    # Replace with cat for clarity
                    file_match = re.search(r"sed\s+-n\s+'1,\d+p'\s+(\S+)", command)
                    if file_match:
                        file_path = file_match.group(1)
                        command = f"cat {file_path}"
                        logger.info(f"Rewrote sed full-file read to cat: {command}")

                if not command or len(command.strip()) == 0:
                    continue

                # Execute command in container
                result = await self.docker_manager.execute_command(session_id, command)

                # Enhanced output formatting with better observability
                output_parts = []
                output_parts.append(f"COMMAND: {command}")
                output_parts.append(f"EXIT CODE: {result.get('exit_code', 'unknown')}")

                if result.get("stdout"):
                    output_parts.append(f"STDOUT:\n{result['stdout']}")
                else:
                    output_parts.append("STDOUT: (empty)")

                if result.get("stderr"):
                    output_parts.append(f"STDERR:\n{result['stderr']}")
                else:
                    output_parts.append("STDERR: (empty)")

                # Add diagnostic info for failed commands
                if not result["success"]:
                    output_parts.append(
                        f"FAILURE REASON: Command exited with code {result.get('exit_code', 'unknown')}"
                    )

                    # Add container health check
                    container_status = await self._check_container_health(session_id)
                    if container_status:
                        output_parts.append(f"CONTAINER STATUS: {container_status}")

                formatted_output = "\n\n".join(output_parts)

                action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="command_execution",
                    command=command,
                    output=formatted_output,
                    reasoning=reasoning,
                    success=result["success"],
                )

                actions.append(action)

                if callback:
                    await callback("action", action.dict())

                # Small delay to prevent overwhelming
                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            # Propagate cancellation to stop immediately
            raise
        except Exception as e:
            error_action = AgentAction(
                timestamp=datetime.now(timezone.utc),
                action_type="ai_error",
                output=str(e),
                reasoning="Failed to get AI response or execute commands",
                success=False,
            )
            actions.append(error_action)
            if callback:
                # Emit as action to avoid front-end error banner for non-fatal AI issues
                await callback("action", error_action.dict())

        return actions

    def _get_system_prompt(self) -> str:
        """Get the system prompt for the AI agent."""
        return """You are an expert CTF challenge solver. Analyze the target and find the flag.

TOOLS AVAILABLE:
curl, wget, python3, cat, grep, strings, base64, openssl, hashcat, john, binwalk, steghide, and pwntools (via python3), and many others.

IMPORTANT:
• Use /workspace for temporary files (not /tmp)
• Commands with pipes/redirects are automatically wrapped in bash
• Use actual targets (URLs or socket host:port), not placeholders like TARGET_URL or TARGET_HOST/TARGET_PORT

GENERAL STRATEGY (one step at a time):
At each turn, propose exactly ONE shell command with a short reasoning.
If a URL is provided, prioritize interacting with the live site (e.g., curl -sSLi ...).
If a socket host:port is provided, do NOT assume HTTP. Interact using python3 with pwntools. Only use curl or HTTP tools if an HTTP banner/response explicitly confirms HTTP.
For multi-step interactive sockets, prefer pwntools scripts saved under /workspace (e.g., /workspace/solve.py) and run with python3.
If handouts are provided, use them to inform live interactions or solve handout-only challenges.
If any handout is an archive (.zip, .tar, .tar.gz, .tgz, .gz, .7z), extract it FIRST into /workspace/handouts/extracted (avoid re-extracting if already extracted).

PYTHON JAIL STRATEGY (when interacting with Python REPL/jail):
• Try quick escapes: breakpoint() to drop into pdb (then use !/!shell to run commands), help(), dir(), vars(), locals(), globals().
• Leverage builtins: import builtins; builtins.__import__('os').system('sh'), or __import__('os').popen('sh').read().
• Explore object graph: object.__subclasses__() to locate Popen or IO classes to execute commands/read files.
• Bypass filters: build names dynamically (chr(), bytes, join) to avoid blacklists; consider non‑Unicode payloads.
• For sockets, prefer pwntools; to send bytes, write small python3 scripts rather than echo.

IMPORTANT:
Always extract archives first. Always look at the contents of the archive before trying to solve the challenge.

RESPONSE FORMAT:
Return exactly one shell command and a short reasoning."""

    def _build_analysis_prompt(
        self,
        url: Optional[str],
        flag_format: str,
        challenge_type: Optional[ChallengeType],
        description: Optional[str],
        uploaded_files: List[str],
        socket_host: Optional[str] = None,
        socket_port: Optional[int] = None,
    ) -> str:
        """Build initial analysis prompt tailored to URL, socket, or handout-only."""
        desc = (description or "").strip()
        desc_block = f"Description: {desc}\n\n" if desc else ""
        files_block = ""
        if uploaded_files:
            files_list = "\n".join(
                f"- /workspace/handouts/{name}" for name in uploaded_files
            )
            files_block = f"Handout files:\n{files_list}\n\n"

        target_block = []
        if url:
            target_block.append(f"Target URL: {url}")
        if socket_host and socket_port:
            target_block.append(f"Target SOCKET: {socket_host}:{socket_port}")
        if not target_block:
            target_block.append("Handout-only: No URL or socket provided")

        # Tailor guidance
        guidance_lines = [
            "All uploaded handout files are available under /workspace/handouts.",
            "Use tar, unzip, strings, file, binwalk, and other tools as appropriate.",
            "If any handout is an archive (.zip, .tar, .tar.gz, .tgz, .gz, .7z), extract it FIRST into /workspace/handouts/extracted (avoid re-extracting if it already exists).",
        ]
        if url:
            guidance_lines.append(
                "Prioritize interacting with the live site using curl -sSL; use handouts to inform requests."
            )
        if socket_host and socket_port:
            guidance_lines.append(
                "This is a socket challenge: do NOT assume HTTP. Use python3 with pwntools. Only switch to HTTP tooling if an HTTP response is confirmed. Consider banner grabbing and protocol exploration."
            )
        if not url and not (socket_host and socket_port):
            guidance_lines.append(
                "No remote target provided; focus on analyzing handouts to recover the flag."
            )

        if self._is_pyjail_context(description, uploaded_files, []):
            guidance_lines.append("")
            guidance_lines.append(self._get_pyjail_guidance())
        guidance = "\n".join(guidance_lines)

        return f"""{chr(10).join(target_block)}
Flag format: {flag_format}
{desc_block}
{files_block}{guidance}

Start analyzing this challenge to find the flag. Propose exactly ONE shell command with a short reasoning."""

    def _build_iteration_prompt(
        self,
        actions: List[AgentAction],
        flag_format: str,
        iteration: int,
        uploaded_files: List[str],
        url: Optional[str] = None,
        socket_host: Optional[str] = None,
        socket_port: Optional[int] = None,
    ) -> str:
        """Build prompt for iterative solving tailored to URL/socket/handout-only."""
        import re  # For regex operations

        # Give ALL history - no limits so GPT has complete context
        recent_actions = actions

        context = "Previous work:\n"
        for action in recent_actions:
            if action.command:
                context += f"• {action.command}\n"
                if action.output:
                    # Extract STDOUT content
                    stdout_match = re.search(
                        r"STDOUT:\n(.*?)(?:\n\nSTDERR:|$)", action.output, re.DOTALL
                    )
                    if stdout_match:
                        stdout_content = stdout_match.group(1).strip()
                        if stdout_content and len(stdout_content) > 0:
                            context += f"  → {stdout_content}\n"
                    # Extract EXIT CODE and STDERR preview to give failure context
                    try:
                        exit_match = re.search(r"EXIT CODE:\s*(\d+)", action.output)
                        if exit_match and exit_match.group(1) != "0":
                            context += f"  → exit={exit_match.group(1)}\n"

                        stderr_match = re.search(r"STDERR:\n(.*)", action.output, re.DOTALL)
                        if stderr_match:
                            stderr_content = stderr_match.group(1).strip()
                            if stderr_content:
                                PREVIEW_LIMIT = 800
                                if len(stderr_content) > PREVIEW_LIMIT:
                                    head = stderr_content[: PREVIEW_LIMIT // 2]
                                    tail = stderr_content[-(PREVIEW_LIMIT // 2) :]
                                    stderr_preview = f"{head}\n...\n{tail}"
                                else:
                                    stderr_preview = stderr_content
                                context += f"  → STDERR: {stderr_preview}\n"
                    except Exception:
                        pass
                else:
                    if not action.success:
                        context += f"  → Failed\n"
                context += "\n"

        files_block = ""
        if uploaded_files:
            files_list = "\n".join(
                f"- /workspace/handouts/{name}" for name in uploaded_files
            )
            files_block = f"Available handout files (re-usable):\n{files_list}\n\n"
        target_block = []
        if url:
            target_block.append(f"Target URL: {url}")
        if socket_host and socket_port:
            target_block.append(f"Target SOCKET: {socket_host}:{socket_port}")
        if not target_block:
            target_block.append("Handout-only: No URL or socket provided")

        guidance_lines = [
            "If handouts include an archive and it is not yet extracted, extract it FIRST into /workspace/handouts/extracted.",
        ]
        if url:
            guidance_lines.append(
                "Prefer interacting with the live site using curl -sSL; alternate between handout insights and live probing."
            )
        if socket_host and socket_port:
            guidance_lines.append(
                "Socket target: do NOT assume HTTP. Use python3 with pwntools; Only use HTTP tools if HTTP is confirmed."
            )
        if not url and not (socket_host and socket_port):
            guidance_lines.append(
                "No remote target provided; progress by analyzing handouts systematically."
            )

        if self._is_pyjail_context(None, uploaded_files, actions):
            guidance_lines.append(self._get_pyjail_guidance())
        guidance = " ".join(guidance_lines)

        return f"""{context}
{chr(10).join(target_block)}
Flag format: {flag_format}

{files_block}
Continue working toward finding the flag. {guidance} Propose exactly ONE next shell command with a short reasoning. If you just read a local file, avoid reprinting it fully; summarize and move on."""

    def _check_for_flag(
        self, actions: List[AgentAction], flag_format: str
    ) -> Optional[str]:
        """Check if flag has been found in any action outputs."""
        # Convert flag format to regex
        flag_regex = flag_format.replace(".*", ".*?")
        pattern = re.compile(flag_regex, re.IGNORECASE)

        for action in reversed(actions[-10:]):  # Check last 10 actions
            if action.output and action.action_type != "session_initialized":
                # Skip session initialization messages that contain the flag format

                # Extract only the STDOUT section to avoid matching commands or metadata
                stdout_match = re.search(
                    r"STDOUT:\n(.*?)(?:\n\nSTDERR:|$)", action.output, re.DOTALL
                )
                if stdout_match:
                    stdout_content = stdout_match.group(1).strip()
                    # Additional filtering: exclude lines that mention "flag format" or similar metadata
                    if stdout_content and not any(
                        phrase in stdout_content.lower()
                        for phrase in [
                            "flag format:",
                            "flag pattern:",
                            "target flag:",
                            "looking for:",
                            "searching for:",
                        ]
                    ):
                        for match in pattern.finditer(stdout_content):
                            candidate = match.group(0)
                            if all(
                                banned not in candidate.lower()
                                for banned in FLAG_CANDIDATE_BANNED_SUBSTRINGS
                            ):
                                return candidate
                else:
                    # Fallback: search full output but exclude command lines and metadata
                    lines = action.output.split("\n")
                    content_lines = [
                        line
                        for line in lines
                        if not line.startswith(
                            (
                                "COMMAND:",
                                "EXIT CODE:",
                                "Flag format:",
                                "Target flag:",
                                "CTF Agent session",
                            )
                        )
                    ]
                    content = "\n".join(content_lines).strip()
                    if content and not any(
                        phrase in content.lower()
                        for phrase in [
                            "flag format:",
                            "flag pattern:",
                            "target flag:",
                            "looking for:",
                            "searching for:",
                        ]
                    ):
                        for match in pattern.finditer(content):
                            candidate = match.group(0)
                            if all(
                                banned not in candidate.lower()
                                for banned in FLAG_CANDIDATE_BANNED_SUBSTRINGS
                            ):
                                return candidate

        return None
