import asyncio
import logging
import re
import json
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime, timezone
from openai import AsyncOpenAI

from .models import AgentAction, ChallengeType
from .docker_manager import DockerManager

logger = logging.getLogger(__name__)


class CTFAgent:
    def __init__(self, openai_api_key: str, model: str = "gpt-5-mini"):
        self.client = AsyncOpenAI(api_key=openai_api_key)
        self.model = model  # Using gpt-5-mini (the latest GPT-5 small model)
        self.docker_manager = DockerManager()
    
    def _detect_challenge_type(self, url: str) -> Optional[str]:
        """Attempt to detect challenge type from URL patterns."""
        url_lower = url.lower()
        
        # Web challenge indicators
        web_indicators = ['web', 'http', 'www', 'login', 'admin', 'upload', 'php', 'asp', 'jsp']
        if any(indicator in url_lower for indicator in web_indicators):
            return "web"
        
        # Crypto challenge indicators
        crypto_indicators = ['crypto', 'cipher', 'encrypt', 'decode', 'hash']
        if any(indicator in url_lower for indicator in crypto_indicators):
            return "crypto"
        
        # Reverse engineering indicators
        reverse_indicators = ['reverse', 'binary', 'exe', 'crackme', 'patch']
        if any(indicator in url_lower for indicator in reverse_indicators):
            return "reverse"
        
        # Forensics indicators
        forensics_indicators = ['forensics', 'memory', 'dump', 'pcap', 'image', 'disk']
        if any(indicator in url_lower for indicator in forensics_indicators):
            return "forensics"
        
        # Network/Pwnable indicators
        network_indicators = ['pwn', 'buffer', 'overflow', 'network', 'tcp', 'udp']
        if any(indicator in url_lower for indicator in network_indicators):
            return "pwnable"
        
        # Default to web if it's an HTTP(S) URL
        if url_lower.startswith(('http://', 'https://')):
            return "web"
        
        return None

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
            
            if container.attrs['State']['Status'] != 'running':
                if 'Error' in container.attrs['State']:
                    status_info.append(f"Error: {container.attrs['State']['Error']}")
                if 'ExitCode' in container.attrs['State']:
                    status_info.append(f"Exit Code: {container.attrs['State']['ExitCode']}")
            
            # Check network connectivity
            try:
                test_result = container.exec_run("ping -c 1 8.8.8.8", timeout=5)
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
        challenge_url: str,
        flag_format: str,
        challenge_type: Optional[ChallengeType] = None,
        callback: Optional[Callable] = None
    ) -> List[AgentAction]:
        """Main method to solve a CTF challenge."""
        actions = []
        
        try:
            # Create container
            await self.docker_manager.create_container(session_id)
            
            # Send initial status update
            if callback:
                init_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="session_initialized",
                    output=f"CTF Agent session started for {challenge_url}\nFlag format: {flag_format}\nContainer created successfully",
                    reasoning="Session initialization and container setup",
                    success=True
                )
                actions.append(init_action)
                await callback("action", init_action.dict())
            
            # Start with challenge analysis - let AI decide what tools it needs
            analysis_prompt = self._build_analysis_prompt(challenge_url, flag_format, challenge_type)
            analysis_actions = await self._execute_with_ai(
                session_id,
                analysis_prompt,
                actions,
                callback
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
                        success=True
                    )
                    actions.append(flag_action)
                    if callback:
                        await callback("flag_found", flag_action.dict())
                    break
                
                # Get next single action from AI
                next_prompt = self._build_iteration_prompt(actions, flag_format, i)
                iteration_actions = await self._execute_with_ai(
                    session_id,
                    next_prompt,
                    actions,
                    callback
                )
                actions.extend(iteration_actions)
                
                # Add a small delay between iterations to ensure frontend updates
                await asyncio.sleep(1)
                
                # Prevent infinite loops
                if not iteration_actions:
                    break
            
            return actions
            
        except Exception as e:
            error_action = AgentAction(
                timestamp=datetime.now(timezone.utc),
                action_type="error",
                output=str(e),
                reasoning="Exception occurred during challenge solving",
                success=False
            )
            actions.append(error_action)
            if callback:
                await callback("error", error_action.dict())
            return actions

    async def _execute_with_ai(
        self,
        session_id: str,
        prompt: str,
        previous_actions: List[AgentAction],
        callback: Optional[Callable] = None
    ) -> List[AgentAction]:
        """Execute AI-generated commands and return actions."""
        actions = []
        
        try:
            # Build consolidated input for Responses API (single-step)
            context_blocks = []
            if len(previous_actions) > 0:
                recent_context = "Previous session context:\n"
                for action in previous_actions:
                    if action.command:
                        recent_context += f"Executed: {action.command}\n"
                        if action.output and len(action.output.strip()) > 0:
                            output_preview = action.output[:500] + "..." if len(action.output) > 500 else action.output
                            recent_context += f"Result: {output_preview}\n"
                context_blocks.append(recent_context)

            user_block = f"User request:\n{prompt}"
            input_text = "\n\n".join(context_blocks + [user_block]) if context_blocks else user_block

            # Call Responses API with JSON schema for a single command, fallback to chat.completions if needed
            try:
                response = await self.client.responses.create(
                    model=self.model,
                    system=self._get_system_prompt(),
                    input=input_text,
                    max_output_tokens=10000,
                    reasoning={"effort": "medium"},
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "SingleCommand",
                            "strict": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "command": {"type": "string"},
                                    "reasoning": {"type": "string"}
                                },
                                "required": ["command"],
                                "additionalProperties": False
                            }
                        }
                    }
                )
                use_responses_api = True
            except Exception as e:
                logger.warning(f"Responses API call failed, falling back to chat.completions: {e}")
                messages = [
                    {"role": "system", "content": self._get_system_prompt()},
                    {"role": "user", "content": input_text}
                ]
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_completion_tokens=4000,
                    reasoning_effort="medium"
                )
                use_responses_api = False

            # Extract output text
            if use_responses_api:
                ai_response = getattr(response, "output_text", None)
                if ai_response is None:
                    try:
                        parts: List[str] = []
                        for out in getattr(response, "output", []) or []:
                            for content in getattr(out, "content", []) or []:
                                if getattr(content, "type", None) in ("output_text", "text") and getattr(content, "text", None):
                                    parts.append(content.text)
                        ai_response = "\n".join(parts)
                    except Exception:
                        ai_response = ""
            else:
                try:
                    ai_response = response.choices[0].message.content
                except Exception:
                    ai_response = ""

            logger.info(f"AI Responses output length: {len(ai_response or '')}")
            logger.debug(f"AI Response Content: {repr((ai_response or '')[:1000])}")

            if not ai_response or ai_response.strip() == "":
                logger.warning("AI returned empty response from Responses API")
                error_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="ai_empty_response",
                    output="AI returned empty response using Responses API",
                    reasoning="AI hit token limits or returned empty content",
                    success=False
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
                    commands = [{
                        "command": str(parsed.get("command", "")).strip(),
                        "reasoning": str(parsed.get("reasoning", "")).strip() or "No reasoning provided"
                    }]
            except Exception:
                commands = self._parse_ai_response(ai_response)
            
            # If no commands were parsed, log the full AI response for debugging
            if not commands:
                logger.warning(f"No commands extracted from AI response. Full response: {ai_response}")
                
                # Create a more informative action with the full AI response
                error_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="ai_response_parsing_failed",
                    output=f"AI provided response but no commands could be extracted.\n\nFull AI Response:\n{ai_response}",
                    reasoning="The AI responded but the response format didn't match expected patterns for command extraction",
                    success=False
                )
                actions.append(error_action)
                if callback:
                    await callback("action", error_action.dict())
                
                # Try a follow-up request with more explicit instructions  
                followup_prompt = f"""The previous response didn't contain an extractable command. Provide a SINGLE shell command as a JSON object (no prose), matching this schema:

{{
  "command": "curl -sSL {prompt.split('Target: ')[1].split()[0] if 'Target: ' in prompt else 'TARGET_URL'}",
  "reasoning": "Initial reconnaissance"
}}
"""
                
                try:
                    try:
                        followup_response = await self.client.responses.create(
                            model=self.model,
                            system="You are a CTF expert. Provide a SINGLE shell command in JSON object format only.",
                            input=followup_prompt,
                            max_output_tokens=500,
                            reasoning={"effort": "medium"},
                            response_format={
                                "type": "json_schema",
                                "json_schema": {
                                    "name": "SingleCommand",
                                    "strict": True,
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "command": {"type": "string"},
                                            "reasoning": {"type": "string"}
                                        },
                                        "required": ["command"],
                                        "additionalProperties": False
                                    }
                                }
                            }
                        )
                        followup_text = getattr(followup_response, "output_text", "") or ""
                    except Exception as e2:
                        logger.warning(f"Responses API follow-up failed, falling back: {e2}")
                        followup_cc = await self.client.chat.completions.create(
                            model=self.model,
                            messages=[
                                {"role": "system", "content": "You are a CTF expert. Provide a SINGLE shell command in JSON object format only."},
                                {"role": "user", "content": followup_prompt}
                            ],
                            max_completion_tokens=500,
                            reasoning_effort="medium"
                        )
                        followup_text = followup_cc.choices[0].message.content
                    try:
                        parsed_fu = json.loads(followup_text)
                        if isinstance(parsed_fu, dict) and parsed_fu.get("command"):
                            commands = [{
                                "command": str(parsed_fu.get("command", "")).strip(),
                                "reasoning": str(parsed_fu.get("reasoning", "")).strip() or "No reasoning provided"
                            }]
                    except Exception:
                        commands = self._parse_ai_response(followup_text)[:1]
                    if commands:
                        logger.info("Extracted a single command from followup request")
                    else:
                        logger.error("Followup request also failed to produce commands")
                        return actions
                        
                except Exception as followup_error:
                    logger.error(f"Followup request failed: {followup_error}")
                    return actions
            
            # Execute only one command per step
            for command_data in commands[:1]:
                command = command_data.get("command", "")
                reasoning = command_data.get("reasoning", "")
                
                if not command or not command.strip():
                    continue
                
                # Validate command doesn't contain clear JSON key artifacts
                if any(artifact in command for artifact in ['"command":', '"reasoning":']):
                    logger.warning(f"Skipping malformed command that still includes JSON keys: {command}")
                    continue
                
                # Clean up any remaining quotes or artifacts (but preserve quotes inside the command)
                command = command.strip('"{}[]')
                
                # Replace placeholder URLs with actual challenge URL if available
                if 'TARGET_URL' in command or 'TARGET_IP' in command:
                    # Try to extract URL from the prompt
                    if 'Target: ' in prompt:
                        actual_url = prompt.split('Target: ')[1].split()[0]
                        command = command.replace('TARGET_URL', actual_url)
                        command = command.replace('TARGET_IP', actual_url)
                        logger.info(f"Replaced placeholder with actual URL: {command}")
                
                # Fix common truncation issues with sed commands
                if "sed -n '" in command and not command.endswith("'"):
                    # Check if it ends with a pattern like '1,200p without closing quote
                    import re
                    sed_pattern = r"sed -n '(\d+,\d+p)$"
                    match = re.search(sed_pattern, command)
                    if match:
                        command += "'"
                        logger.info(f"Fixed truncated sed command: {command}")
                
                # Fix other common quote truncation patterns
                if command.count("'") % 2 == 1:  # Odd number of single quotes
                    # Common patterns where we can safely add closing quote
                    if any(pattern in command for pattern in ["sed -n '", "awk '", "grep '"]):
                        command += "'"
                        logger.info(f"Fixed unmatched single quote: {command}")
                
                if not command or len(command.strip()) == 0:
                    continue
                
                # Execute command in container
                result = await self.docker_manager.execute_command(session_id, command)
                
                # Enhanced output formatting with better observability
                output_parts = []
                output_parts.append(f"COMMAND: {command}")
                output_parts.append(f"EXIT CODE: {result.get('exit_code', 'unknown')}")
                
                if result.get('stdout'):
                    output_parts.append(f"STDOUT:\n{result['stdout']}")
                else:
                    output_parts.append("STDOUT: (empty)")
                
                if result.get('stderr'):
                    output_parts.append(f"STDERR:\n{result['stderr']}")
                else:
                    output_parts.append("STDERR: (empty)")
                
                # Add diagnostic info for failed commands
                if not result['success']:
                    output_parts.append(f"FAILURE REASON: Command exited with code {result.get('exit_code', 'unknown')}")
                    
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
                    success=result['success']
                )
                
                actions.append(action)
                
                if callback:
                    await callback("action", action.dict())

                # Small delay to prevent overwhelming
                await asyncio.sleep(0.5)
        
        except Exception as e:
            error_action = AgentAction(
                timestamp=datetime.now(timezone.utc),
                action_type="ai_error",
                output=str(e),
                reasoning="Failed to get AI response or execute commands",
                success=False
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
curl, wget, python3, nmap, grep, strings, base64, openssl, hashcat, john, binwalk, steghide, and many others.

IMPORTANT:
• Use /workspace for temporary files (not /tmp)
• Commands with pipes/redirects are automatically wrapped in bash
• Use actual URLs, not placeholders like TARGET_URL

WEB CHALLENGE STRATEGY (one step at a time):
At each turn, propose exactly ONE shell command with a short reasoning.
Prefer: curl -sSL (and -c/-b cookies when needed). Use actual URLs, no placeholders.

RESPONSE FORMAT:
Respond with ONLY a single JSON object with fields {command, reasoning}:

{
  "command": "curl -sSL https://actual-url.com",
  "reasoning": "Initial reconnaissance"
}

No other text. Just the JSON object."""

    def _build_analysis_prompt(self, url: str, flag_format: str, challenge_type: Optional[ChallengeType]) -> str:
        """Build initial analysis prompt."""
        return f"""Target: {url}
Flag format: {flag_format}

Start analyzing this challenge to find the flag."""

    def _build_iteration_prompt(self, actions: List[AgentAction], flag_format: str, iteration: int) -> str:
        """Build prompt for iterative solving."""
        import re  # For regex operations
        
        # Give ALL history - no limits so GPT has complete context
        recent_actions = actions
        
        context = "Previous work:\n"
        for action in recent_actions:
            if action.command:
                context += f"• {action.command}\n"
                if action.output:
                    # Extract STDOUT content
                    stdout_match = re.search(r'STDOUT:\n(.*?)(?:\n\nSTDERR:|$)', action.output, re.DOTALL)
                    if stdout_match:
                        stdout_content = stdout_match.group(1).strip()
                        if stdout_content and len(stdout_content) > 0:
                            # Keep more output for better context
                            if len(stdout_content) > 500:
                                output_preview = stdout_content[:500] + "..."
                            else:
                                output_preview = stdout_content
                            context += f"  → {output_preview}\n"
                    elif not action.success:
                        context += f"  → Failed\n"
                context += "\n"
        
        return f"""{context}
Flag format: {flag_format}

Continue working toward finding the flag. Propose exactly ONE next shell command with a short reasoning. Use actual URLs and include -sSL. Return only a single JSON object with keys command and reasoning."""

    def _parse_ai_response(self, response: str) -> List[Dict[str, str]]:
        """Parse AI response to extract commands with improved error handling."""
        logger.debug(f"Parsing AI response: {repr(response[:500])}...")
        try:
            # First, try to find JSON blocks with various formats
            json_patterns = [
                r'```json\s*(.*?)\s*```',  # Standard JSON blocks
                r'```\s*(\[.*?\])\s*```',  # JSON arrays in code blocks
                r'(\[[\s\S]*?\])',         # Any JSON array in the response
                r'```\s*([\s\S]*?)\s*```', # Any code block content
            ]
            
            # Also try to extract individual command/reasoning pairs from malformed JSON
            # Handle escaped quotes in commands by using a more sophisticated pattern
            command_reasoning_pattern = r'"command":\s*"((?:[^"\\]|\\.)*)"\s*[,}]?[\s\S]*?"reasoning":\s*"((?:[^"\\]|\\.)*)"'
            command_reasoning_matches = re.findall(command_reasoning_pattern, response, re.DOTALL)
            if command_reasoning_matches:
                extracted_commands = []
                for cmd, reason in command_reasoning_matches:
                    # Unescape any escaped quotes
                    cmd = cmd.replace('\\"', '"').replace("\\'", "'")
                    reason = reason.replace('\\"', '"').replace("\\'", "'")
                    extracted_commands.append({
                        'command': cmd.strip(),
                        'reasoning': reason.strip() if reason else 'No reasoning provided'
                    })
                if extracted_commands:
                    logger.debug(f"Extracted {len(extracted_commands)} commands via regex pattern")
                    for i, cmd in enumerate(extracted_commands):
                        logger.debug(f"Command {i+1}: {repr(cmd['command'])}")
                    return extracted_commands[:3]
            
            for pattern in json_patterns:
                matches = re.findall(pattern, response, re.DOTALL)
                for match in matches:
                    try:
                        logger.debug(f"Attempting to parse JSON: {repr(match[:200])}...")
                        parsed = json.loads(match)
                        if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
                            # Validate that each command has required fields
                            valid_commands = []
                            for cmd in parsed:
                                if 'command' in cmd:
                                    command_text = cmd['command'].strip()
                                    logger.debug(f"Extracted command from JSON: {repr(command_text)}")
                                    valid_commands.append({
                                        'command': command_text,
                                        'reasoning': cmd.get('reasoning', 'No reasoning provided').strip()
                                    })
                            if valid_commands:
                                logger.debug(f"Successfully parsed {len(valid_commands)} commands from JSON")
                                return valid_commands
                    except json.JSONDecodeError as e:
                        logger.debug(f"JSON parse failed: {e}")
                        # Try to fix common JSON issues and retry
                        fixed_match = self._fix_partial_json(match)
                        if fixed_match != match:
                            try:
                                logger.debug(f"Trying fixed JSON: {repr(fixed_match[:200])}...")
                                parsed = json.loads(fixed_match)
                                if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
                                    valid_commands = []
                                    for cmd in parsed:
                                        if 'command' in cmd:
                                            command_text = cmd['command'].strip()
                                            logger.debug(f"Extracted command from fixed JSON: {repr(command_text)}")
                                            valid_commands.append({
                                                'command': command_text,
                                                'reasoning': cmd.get('reasoning', 'No reasoning provided').strip()
                                            })
                                    if valid_commands:
                                        logger.debug(f"Successfully parsed {len(valid_commands)} commands from fixed JSON")
                                        return valid_commands
                            except json.JSONDecodeError:
                                continue
                        continue
            
            # Fallback: Look for structured text patterns
            commands = []
            lines = response.split('\n')
            
            # Pattern 1: Command: ... Reasoning: ...
            for i, line in enumerate(lines):
                line = line.strip()
                if line.lower().startswith(('command:', '- command:', '• command:')):
                    command = line.split(':', 1)[1].strip()
                    reasoning = "No reasoning provided"
                    
                    # Look for reasoning in next few lines
                    for j in range(i + 1, min(i + 4, len(lines))):
                        next_line = lines[j].strip()
                        if next_line.lower().startswith(('reasoning:', '- reasoning:', '• reasoning:')):
                            reasoning = next_line.split(':', 1)[1].strip()
                            break
                    
                    if command:
                        commands.append({"command": command, "reasoning": reasoning})
            
            # Pattern 2: Look for shell commands (starting with common commands)
            if not commands:
                common_commands = ['curl', 'wget', 'python', 'python3', 'ls', 'cat', 'grep', 'find', 'nmap', 'gobuster', 'dirb', 'nc', 'netcat', 'dig', 'host', 'ping', 'traceroute', 'telnet', 'ssh', 'ftp', 'scp', 'rsync']
                for line in lines:
                    line = line.strip()
                    # Remove common prefixes
                    cleaned_line = line
                    for prefix in ['$', '>', '#', '1.', '2.', '3.', '-', '•', '*']:
                        if line.startswith(prefix + ' '):
                            cleaned_line = line[len(prefix):].strip()
                            break
                    
                    for cmd_start in common_commands:
                        if cleaned_line.startswith(cmd_start + ' ') or cleaned_line == cmd_start:
                            commands.append({
                                "command": cleaned_line,
                                "reasoning": "Command extracted from AI response"
                            })
                            break
                            
            # Pattern 3: Look for numbered commands
            if not commands:
                for line in lines:
                    line = line.strip()
                    # Match patterns like "1. curl ..." or "- curl ..."
                    if re.match(r'^[\d\-\*\•]\s*\.?\s*(curl|wget|python|ls|cat|grep|find|nmap)', line, re.IGNORECASE):
                        command_match = re.search(r'^[\d\-\*\•]\s*\.?\s*(.+)', line)
                        if command_match:
                            commands.append({
                                "command": command_match.group(1).strip(),
                                "reasoning": "Numbered command extracted from AI response"
                            })
                            
            # Pattern 4: Look for JSON-like command patterns
            if not commands:
                for line in lines:
                    line = line.strip()
                    # Look for lines like '"command": "curl ..."' and extract just the command
                    command_match = re.search(r'"command":\s*"([^"]+)"', line)
                    if command_match:
                        commands.append({
                            "command": command_match.group(1),
                            "reasoning": "JSON command pattern extracted from AI response"
                        })
                        continue
                    
                    # Look for other command patterns but exclude JSON structure
                    if (line and 
                        not line.startswith(('#', '//', '/*', 'Note:', 'First', 'Next', 'Then', 'Let', 'I will', 'We should', '"command":', '"reasoning":')) and
                        ('http' in line or '--' in line or '.com' in line or '-v' in line or '-s' in line) and
                        not line.startswith(('{', '}', '[', ']'))):
                        commands.append({
                            "command": line,
                            "reasoning": "Pattern-matched command from AI response"
                        })
            
            # If we still don't have commands, try to extract any reasonable looking command
            if not commands:
                for line in lines:
                    line = line.strip()
                    # Look for lines that look like shell commands
                    if (line and not line.startswith('#') and 
                        ('/' in line or '-' in line or '=' in line) and
                        len(line.split()) > 1):
                        commands.append({
                            "command": line,
                            "reasoning": "Potential command extracted from response"
                        })
            
            return commands[:3]  # Limit to first 3 commands to avoid too many at once
            
        except Exception as e:
            logger.error(f"Failed to parse AI response: {e}")
            logger.error(f"Response was: {response[:500]}...")
            return []

    def _fix_partial_json(self, json_text: str) -> str:
        """Attempt to fix partial or malformed JSON."""
        # Remove any trailing incomplete entries
        json_text = json_text.strip()
        
        # Fix common sed command truncation issue first
        import re
        sed_fix_pattern = r'"command":\s*"([^"]*sed -n \'[^\']*)"'
        def fix_sed_quotes(match):
            command = match.group(1)
            if command.count("'") % 2 == 1:  # Odd number of quotes, missing closing quote
                command += "'"
                logger.debug(f"Fixed truncated sed command in JSON: {command}")
            return f'"command": "{command}"'
        
        json_text = re.sub(sed_fix_pattern, fix_sed_quotes, json_text)
        
        # If it ends with incomplete command entry, try to complete it
        if json_text.endswith('"command"'):
            # Truncated before the value, remove the incomplete entry
            json_text = json_text.rsplit(',', 1)[0] if ',' in json_text else json_text.rsplit('[', 1)[0] + '['
        elif json_text.endswith('"command":'):
            # Truncated after the colon, remove the incomplete entry
            json_text = json_text.rsplit(',', 1)[0] if ',' in json_text else json_text.rsplit('[', 1)[0] + '['
        elif json_text.endswith('"command": "'):
            # Truncated in the middle of command value, remove the incomplete entry
            json_text = json_text.rsplit(',', 1)[0] if ',' in json_text else json_text.rsplit('[', 1)[0] + '['
        
        # If it doesn't end with ] or }, try to close the JSON properly
        if not json_text.endswith((']', '}')):
            # Count open braces and brackets
            open_braces = json_text.count('{') - json_text.count('}')
            open_brackets = json_text.count('[') - json_text.count(']')
            
            # Close any open structures
            json_text += '}' * open_braces
            json_text += ']' * open_brackets
        
        # Fix common quote issues in commands
        # If we have an odd number of quotes in a command value, it's likely truncated
        command_pattern = r'"command":\s*"([^"]*)"'
        matches = re.finditer(command_pattern, json_text)
        for match in matches:
            command_value = match.group(1)
            # Check if command looks incomplete (ends with common command patterns)
            if command_value.endswith((' |', ' &&', ' ||', ' >', ' <', ' ;')):
                # This looks like a truncated command, remove the incomplete part
                truncated_pos = match.start()
                json_text = json_text[:truncated_pos]
                # Close the JSON properly
                open_braces = json_text.count('{') - json_text.count('}')
                open_brackets = json_text.count('[') - json_text.count(']')
                json_text += '}' * open_braces
                json_text += ']' * open_brackets
                break
        
        return json_text

    # Auto-exploration removed for strict one-by-one flow

    def _check_for_flag(self, actions: List[AgentAction], flag_format: str) -> Optional[str]:
        """Check if flag has been found in any action outputs."""
        # Convert flag format to regex
        flag_regex = flag_format.replace(".*", ".*?")
        pattern = re.compile(flag_regex, re.IGNORECASE)
        
        for action in reversed(actions[-10:]):  # Check last 10 actions
            if action.output and action.action_type != "session_initialized":
                # Skip session initialization messages that contain the flag format
                
                # Extract only the STDOUT section to avoid matching commands or metadata
                stdout_match = re.search(r'STDOUT:\n(.*?)(?:\n\nSTDERR:|$)', action.output, re.DOTALL)
                if stdout_match:
                    stdout_content = stdout_match.group(1).strip()
                    # Additional filtering: exclude lines that mention "flag format" or similar metadata
                    if stdout_content and not any(phrase in stdout_content.lower() for phrase in [
                        'flag format:', 'flag pattern:', 'target flag:', 'looking for:', 'searching for:'
                    ]):
                        match = pattern.search(stdout_content)
                        if match:
                            return match.group(0)
                else:
                    # Fallback: search full output but exclude command lines and metadata
                    lines = action.output.split('\n')
                    content_lines = [line for line in lines 
                                   if not line.startswith(('COMMAND:', 'EXIT CODE:', 'Flag format:', 'Target flag:', 'CTF Agent session'))]
                    content = '\n'.join(content_lines).strip()
                    if content and not any(phrase in content.lower() for phrase in [
                        'flag format:', 'flag pattern:', 'target flag:', 'looking for:', 'searching for:'
                    ]):
                        match = pattern.search(content)
                        if match:
                            return match.group(0)
        
        return None
