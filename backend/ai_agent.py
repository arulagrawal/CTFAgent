import asyncio
import logging
import re
import json
from typing import List, Dict, Any, Optional, Callable
import os
import uuid
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
        # Track last Responses API response.id per session for conversation state
        self._session_last_response_id: Dict[str, Optional[str]] = {}
        # Track challenge type per session for tailored prompts
        self._session_challenge_type: Dict[str, Optional[ChallengeType]] = {}

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
                return
            # Responses API usage fields
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)
            total_tokens = getattr(
                usage,
                "total_tokens",
                (input_tokens or 0) + (output_tokens or 0),
            )

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
        # try:
        #     desc = (description or "").lower()
        #     indicators = [
        #         "pyjail",
        #         "python jail",
        #         "python repl",
        #         "repl",
        #         "sandbox",
        #         "builtins",
        #         "__import__",
        #         ">>>",
        #         "traceback",
        #         "nameerror",
        #         "pdb",
        #     ]
        #     if any(tok in desc for tok in indicators):
        #         return True
        #     for name in uploaded_files:
        #         n = (name or "").lower()
        #         if n.endswith(".py") or ("jail" in n and "py" in n):
        #             return True
        #     # Look at recent outputs for Python prompts/errors
        #     for action in actions[-5:]:
        #         out = (action.output or "").lower()
        #         if any(
        #             tok in out
        #             for tok in [
        #                 ">>>",
        #                 "traceback (most recent call last)",
        #                 "nameerror",
        #                 "attributeerror",
        #                 "builtins",
        #                 "pdb",
        #             ]
        #         ):
        #             return True
        # except Exception:
        #     pass
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
        reasoning_effort: Optional[str] = None,
    ) -> List[AgentAction]:
        """Main method to solve a CTF challenge."""
        actions = []

        try:
            # Initialize usage tracking
            self._init_usage(session_id)
            # Store session challenge type for tailored prompts
            self._session_challenge_type[session_id] = challenge_type
            # Set per-session reasoning effort (default low)
            try:
                self._reasoning_effort = (
                    str(reasoning_effort).lower()
                    if reasoning_effort in ("low", "medium")
                    else "low"
                )
            except Exception:
                self._reasoning_effort = "low"
            # Reset conversation state
            self._session_last_response_id[session_id] = None
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
            max_iterations = 50
            for i in range(max_iterations):
                # Check if flag found
                flag = self._check_for_flag(actions, flag_format)
                if flag:
                    flag_action = AgentAction(
                        timestamp=datetime.now(timezone.utc),
                        action_type="flag_found",
                        output=f"{flag}",
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

            # Call Responses API with structured output (SingleCommand) and conversation state
            try:
                if callback:
                    await callback("ai_status", {"thinking": True})
                prev_id = self._session_last_response_id.get(session_id)
                _kwargs = {}
                if prev_id:
                    _kwargs["previous_response_id"] = prev_id
                response = await self.client.responses.parse(
                    model=self.model,
                    instructions=self._get_system_prompt(session_id),
                    input=[{"role": "user", "content": input_text}],
                    max_output_tokens=2**14,
                    reasoning={"effort": getattr(self, "_reasoning_effort", "low")},
                    text_format=SingleCommand,
                    service_tier="flex",
                    **_kwargs,
                )
                if callback:
                    await callback("ai_status", {"thinking": False})

                # Track usage for Responses API
                self._update_usage_from_response(session_id, response)
                # Update last response id for conversation continuation
                try:
                    self._session_last_response_id[session_id] = getattr(
                        response, "id", None
                    )
                except Exception:
                    pass
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

            # Use structured output directly (SingleCommand)
            output = response.output_parsed
            if not output:
                logger.warning(
                    "AI returned empty structured command from Responses API"
                )
                error_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="ai_empty_response",
                    output="AI returned empty response using Responses API",
                    reasoning=(
                        response.incomplete_details.reason
                        if response.incomplete_details
                        else f"No reasoning provided. Token usage: {response.usage.total_tokens if response.usage else 'unknown'}"
                    ),
                    success=False,
                )
                actions.append(error_action)
                if callback:
                    await callback("action", error_action.dict())
                return actions
            else:
                command = output.command
                reasoning = output.reasoning

            # Create action placeholder and stream output
            action_id = uuid.uuid4().hex
            stdout_acc: List[str] = []
            stderr_acc: List[str] = []

            action = AgentAction(
                id=action_id,
                timestamp=datetime.now(timezone.utc),
                action_type="command_execution",
                command=command,
                output="",
                reasoning=reasoning,
                success=True,
            )
            actions.append(action)
            if callback:
                await callback("action_create", action.dict())

            async def on_chunk(channel: str, text: str):
                if channel == "stdout":
                    stdout_acc.append(text)
                else:
                    stderr_acc.append(text)
                if callback:
                    await callback(
                        "action_chunk",
                        {"id": action_id, "channel": channel, "chunk": text},
                    )

            result = await self.docker_manager.execute_command_stream(
                session_id, command, on_chunk
            )

            # Build final formatted output for persistence and post-processing
            output_parts = []
            output_parts.append(f"COMMAND: {command}")
            output_parts.append(f"EXIT CODE: {result.get('exit_code', 'unknown')}")

            stdout_full = "".join(stdout_acc)
            stderr_full = "".join(stderr_acc)

            if stdout_full:
                output_parts.append(f"STDOUT:\n{stdout_full}")
            else:
                output_parts.append("STDOUT: (empty)")

            if stderr_full:
                output_parts.append(f"STDERR:\n{stderr_full}")
            else:
                output_parts.append("STDERR: (empty)")

            if not result["success"]:
                output_parts.append(
                    f"FAILURE REASON: Command exited with code {result.get('exit_code', 'unknown')}"
                )
                container_status = await self._check_container_health(session_id)
                if container_status:
                    output_parts.append(f"CONTAINER STATUS: {container_status}")

            formatted_output = "\n\n".join(output_parts)

            action.output = formatted_output
            action.success = bool(result.get("success"))

            if callback:
                await callback(
                    "action_complete",
                    {
                        "id": action_id,
                        "success": action.success,
                        "exit_code": result.get("exit_code"),
                        "final_output": formatted_output,
                    },
                )

            # Small delay to prevent overwhelming
            await asyncio.sleep(0.2)

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

    def _get_rev_guidance(self) -> str:
        lines = [
            "REVERSING TIPS:",
            "- Identify file type: file, ldd, checksec.",
            "- Inspect strings and symbols: strings -a, nm -D, readelf -a, objdump -d.",
            "- Use radare2 or Ghidra headless for static analysis; avoid interactive sessions.",
            "- Look for input checks, strcmp/memcmp, key derivations, and flag formatting functions.",
            "- Grep for the flag format in binaries/handouts; search embedded resources.",
            "- If obfuscated, dump sections, analyze control flow, and reconstruct constants with python3.",
        ]
        return "\n".join(lines)

    def _get_pwn_guidance(self) -> str:
        lines = [
            "BINARY EXPLOITATION TIPS:",
            "- Profile binary: file, checksec, pwn checksec, seccomp-tools if present.",
            "- Probe inputs for crashes or leaks; prefer scripted fuzzing via python3/pwntools.",
            # "- Use non-interactive tooling: gdb is not interactive here; instead, use gdb -batch -ex commands or pwndbg scripts to dump info.",
            # "- Look for format strings, buffer overflows, UAF, and GOT/PLT opportunities.",
            # "- Leak addresses to defeat ASLR; compute libc base with pwntools if libc present.",
            # "- Build ROP chains with ROPgadget or ropper; assemble payloads in python3.",
            "- For remote sockets, always script with pwntools; avoid netcat interactivity. Avoid long bruteforcing on the remote socket.",
            "- With pwntools, always set timeouts: remote(host, port, timeout=20); conn.timeout = 20; recv/recvuntil(..., timeout=20).",
            "- Leverage Python analysis libs: angr for symbolic execution/path finding and constraint solving; Capstone for disassembly (optionally Keystone/Unicorn).",
            "- Try to focus on using angr to find the flag.",
        ]
        return "\n".join(lines)

    def _get_system_prompt(self, session_id: Optional[str] = None) -> str:
        """Get the system prompt for the AI agent, tailored by challenge type and context."""
        # Base tools
        tools_block = (
            "TOOLS AVAILABLE:\n"
            "curl, wget, python3, cat, grep, strings, base64, openssl, hashcat, john, binwalk, steghide, and pwntools (via python3), and many others.\n"
        )

        # Non-interactive reminder
        non_interactive = (
            "NON-INTERACTIVE ENVIRONMENT:\n"
            "You do not have an interactive TTY. Avoid launching interactive tools (e.g., gdb TUI, interactive shells).\n"
            "If you need debugger output, use non-interactive modes (e.g., gdb -batch -ex '...' --args ./bin) or scripted analysis.\n"
        )

        # General guidance
        general = (
            "IMPORTANT:\n"
            "• Use /workspace for temporary files (not /tmp)\n"
            "• Commands with pipes/redirects are automatically wrapped in bash\n"
            "• Use actual targets (URLs or socket host:port), not placeholders like TARGET_URL or TARGET_HOST/TARGET_PORT\n\n"
            "GENERAL STRATEGY (one step at a time):\n"
            "At each turn, propose exactly ONE shell command with a short reasoning.\n"
            "If a URL is provided, prioritize interacting with the live site (e.g., curl -sSLi ...).\n"
            "If a socket host:port is provided, do NOT assume HTTP. Interact using python3 with pwntools. Only use curl or HTTP tools if an HTTP banner/response explicitly confirms HTTP.\n"
            "For multi-step interactive sockets, prefer pwntools scripts saved under /workspace (e.g., /workspace/solve.py) and run with python3.\n"
            "If handouts are provided, use them to inform live interactions or solve handout-only challenges.\n"
            "If any handout is an archive (.zip, .tar, .tar.gz, .tgz, .gz, .7z), extract it FIRST into /workspace/handouts/extracted (avoid re-extracting if already extracted).\n"
        )

        # Challenge-specific guidance
        ct = self._session_challenge_type.get(session_id) if session_id else None
        specific_blocks: List[str] = []
        if ct == ChallengeType.REVERSE:
            specific_blocks.append(self._get_rev_guidance())
        if ct == ChallengeType.PWNABLE:
            specific_blocks.append(self._get_pwn_guidance())
        # Only include Python jail tips if context indicates pyjail; handled dynamically in analysis/iteration prompts

        tail = (
            "\nRESPONSE FORMAT:\n"
            "Return exactly one shell command and a short reasoning."
        )

        return (
            "You are an expert CTF challenge solver. Analyze the target and find the flag.\n\n"
            + tools_block
            + non_interactive
            + general
            + ("\n\n".join(specific_blocks) + ("\n" if specific_blocks else ""))
            + tail
        )

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
                "This is a socket challenge: do NOT assume HTTP. Use python3 with pwntools (set connection and read timeouts). Only switch to HTTP tooling if an HTTP response is confirmed. Consider banner grabbing and protocol exploration."
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

        # Summarize only the most recent command result to keep tokens low
        recent_block = ""
        try:
            last = next((a for a in reversed(actions) if a.command), None)
        except Exception:
            last = None
        if last:
            recent_block = "Previous result:\n"
            recent_block += f"• {last.command}\n"
            if last.output:
                # Extract STDOUT
                stdout_match = re.search(
                    r"STDOUT:\n(.*?)(?:\n\nSTDERR:|$)", last.output, re.DOTALL
                )
                if stdout_match:
                    stdout_content = stdout_match.group(1).strip()
                    if stdout_content:
                        stdout_preview = stdout_content
                        recent_block += f"  → {stdout_preview}\n"
                # Exit code
                try:
                    exit_match = re.search(r"EXIT CODE:\s*(\d+)", last.output)
                    if exit_match and exit_match.group(1) != "0":
                        recent_block += f"  → exit={exit_match.group(1)}\n"
                except Exception:
                    pass
                # STDERR
                try:
                    stderr_match = re.search(r"STDERR:\n(.*)", last.output, re.DOTALL)
                    if stderr_match:
                        stderr_content = stderr_match.group(1).strip()
                        if stderr_content:
                            PREVIEW_LIMIT = 600
                            if len(stderr_content) > PREVIEW_LIMIT:
                                head = stderr_content[: PREVIEW_LIMIT // 2]
                                tail = stderr_content[-(PREVIEW_LIMIT // 2) :]
                                stderr_preview = f"{head}\n...\n{tail}"
                            else:
                                stderr_preview = stderr_content
                            recent_block += f"  → STDERR: {stderr_preview}\n"
                except Exception:
                    pass
            else:
                if not last.success:
                    recent_block += "  → Failed\n"
            recent_block += "\n"

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
                "Socket target: do NOT assume HTTP. Use python3 with pwntools and set timeouts (e.g., remote(host, port, timeout=20)); Only use HTTP tools if HTTP is confirmed."
            )
        if not url and not (socket_host and socket_port):
            guidance_lines.append(
                "No remote target provided; progress by analyzing handouts systematically."
            )

        if self._is_pyjail_context(None, uploaded_files, actions):
            guidance_lines.append(self._get_pyjail_guidance())
        guidance = " ".join(guidance_lines)

        return f"""{recent_block}
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
