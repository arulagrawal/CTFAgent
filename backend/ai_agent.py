import asyncio
import logging
import re
import json
from typing import List, Dict, Any, Optional, Callable
from datetime import datetime, timezone
import openai
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
            
            # Iterative solving process
            max_iterations = 10
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
                
                # Get next action from AI
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
                
                # Detect if AI is repeating commands (loop detection)
                recent_commands = [action.command for action in actions[-6:] if action.command]
                if len(recent_commands) >= 4:
                    # Check if we're repeating similar commands
                    command_similarity = sum(1 for cmd1 in recent_commands[-2:] for cmd2 in recent_commands[-4:-2] 
                                           if cmd1 and cmd2 and (cmd1 == cmd2 or cmd1.split()[0] == cmd2.split()[0]))
                    if command_similarity >= 2:
                        logger.warning(f"Detected potential command repetition. Similarity score: {command_similarity}")
                        # Add a hint to break the loop
                        loop_hint_action = AgentAction(
                            timestamp=datetime.now(timezone.utc),
                            action_type="system_hint",
                            output="SYSTEM HINT: You may be repeating similar commands. Try a completely different approach or tool. Focus on exploiting any discovered vulnerabilities rather than more reconnaissance.",
                            reasoning="Loop detection triggered",
                            success=True
                        )
                        actions.append(loop_hint_action)
                        if callback:
                            await callback("action", loop_hint_action.dict())
                
                # Prevent infinite loops
                if not iteration_actions or all(not action.success for action in iteration_actions[-3:]):
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
            # Build conversation history for better context
            messages = [{"role": "system", "content": self._get_system_prompt()}]
            
            # Add some recent context if available
            if len(previous_actions) > 0:
                recent_context = "Previous session context:\n"
                for action in previous_actions[-3:]:  # Last 3 actions for context
                    if action.command:
                        recent_context += f"Executed: {action.command}\n"
                        if action.output and len(action.output.strip()) > 0:
                            output_preview = action.output[:200] + "..." if len(action.output) > 200 else action.output
                            recent_context += f"Result: {output_preview}\n"
                
                messages.append({"role": "assistant", "content": recent_context})
            
            messages.append({"role": "user", "content": prompt})
            
            # Get AI response with GPT-5 parameters
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_completion_tokens=10000,  # Further increased to prevent truncation
                reasoning_effort="medium"  # Reduced reasoning effort to save tokens for response
            )
            
            ai_response = response.choices[0].message.content

            logger.info(f"AI Response: {response}")
            logger.debug(f"AI Response Content: {repr(ai_response)}")
            
            # Check if response is empty or None
            if not ai_response or ai_response.strip() == "":
                logger.warning("AI returned empty response, likely due to token limits")
                error_action = AgentAction(
                    timestamp=datetime.now(timezone.utc),
                    action_type="ai_empty_response",
                    output=f"AI returned empty response. Finish reason: {response.choices[0].finish_reason}. Usage: {response.usage}",
                    reasoning="AI hit token limits or returned empty content",
                    success=False
                )
                actions.append(error_action)
                if callback:
                    await callback("action", error_action.dict())
                return actions
            
            # Check if response was truncated due to length limits
            if response.choices[0].finish_reason == 'length':
                logger.warning(f"AI response was truncated due to length limits. Response: {repr(ai_response[-100:])}")
                # Try to fix common truncation issues
                if not ai_response.rstrip().endswith((']', '}')):
                    logger.info("Attempting to fix truncated JSON response")
                    # If it looks like JSON was cut off, try to complete it
                    if ai_response.count('[') > ai_response.count(']'):
                        ai_response += ']'
                    if ai_response.count('{') > ai_response.count('}'):
                        ai_response += '}'
            
            # Parse AI response for commands
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
                followup_prompt = f"""The previous response didn't contain extractable commands. Please provide specific shell commands in this exact JSON format:

[
    {{
        "command": "curl -v {prompt.split('Target: ')[1].split()[0] if 'Target: ' in prompt else 'TARGET_URL'}",
        "reasoning": "Initial reconnaissance of the target"
    }}
]

Provide 1-3 concrete shell commands to start analyzing the challenge. Use the actual target URL, not placeholders."""
                
                try:
                    followup_response = await self.client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": "You are a CTF expert. Provide shell commands in JSON format only."},
                            {"role": "user", "content": followup_prompt}
                        ],
                        max_completion_tokens=500,
                        reasoning_effort="medium"
                    )
                    
                    followup_commands = self._parse_ai_response(followup_response.choices[0].message.content)
                    if followup_commands:
                        logger.info(f"Extracted {len(followup_commands)} commands from followup request")
                        commands = followup_commands
                    else:
                        logger.error("Followup request also failed to produce commands")
                        return actions
                        
                except Exception as followup_error:
                    logger.error(f"Followup request failed: {followup_error}")
                    return actions
            
            for command_data in commands:
                command = command_data.get("command", "")
                reasoning = command_data.get("reasoning", "")
                
                if not command or not command.strip():
                    continue
                
                # Validate command doesn't contain JSON artifacts
                if any(artifact in command for artifact in ['"command":', '"reasoning":', '{"', '"}', '["', '"]']):
                    logger.warning(f"Skipping malformed command with JSON artifacts: {command}")
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
                await callback("error", error_action.dict())
        
        return actions

    def _get_system_prompt(self) -> str:
        """Get the system prompt for the AI agent."""
        return """You are an expert CTF (Capture The Flag) challenge solving agent with deep knowledge of cybersecurity. Your goal is to analyze and solve various types of challenges by thinking strategically and adapting your approach.

AVAILABLE TOOLS IN CONTAINER:
• Web Analysis: curl, wget, python with requests library
• Cryptography: openssl, hashcat, john, python crypto libraries, sage
• Reverse Engineering: gdb, radare2, ghidra, binutils, objdump, strings
• Forensics: binwalk, foremost, strings, hexdump, file, exiftool
• Network: nmap, netcat, telnet, wireshark, tcpdump
• Programming: python3, nodejs, gcc, make
• Steganography: steghide, stegsolve
• General: vim, grep, find, awk, sed, base64, xxd

DIAGNOSTIC COMMANDS:
• Check tool availability: "which curl && curl --version"
• Test network connectivity: "ping -c 1 8.8.8.8"
• Check workspace: "pwd && ls -la"
• Environment check: "env | grep -E '(HTTP|PROXY)'"

IMPORTANT NOTES:
• Use /workspace directory for temporary files instead of /tmp (e.g., -o /workspace/file.html)
• The /tmp directory has noexec restrictions
• All commands with &&, ||, |, >, < are automatically wrapped in bash shell
• Working directory is always /workspace

RESPONSE FORMAT:
CRITICAL: Always respond with ONLY a valid JSON array. No other text. Use ACTUAL URLs provided in the challenge, not placeholders like TARGET_URL. Example:

[
    {
        "command": "curl -I https://actual-challenge-url.com",
        "reasoning": "Check HTTP headers for server information"
    },
    {
        "command": "curl -s https://actual-challenge-url.com | grep -i flag",
        "reasoning": "Search page content for flag pattern"
    }
]

IMPORTANT: Replace placeholder URLs with the actual challenge URL. DO NOT use TARGET_URL, TARGET_IP, or similar placeholders.
DO NOT include any explanatory text before or after the JSON. ONLY the JSON array.

PROBLEM-SOLVING APPROACH:
1. ANALYZE: First understand what type of challenge this is
2. RECONNAISSANCE: Gather information about the target
3. HYPOTHESIS: Form theories about potential vulnerabilities or solutions
4. TEST: Execute targeted commands to test your hypotheses
5. ADAPT: Based on results, refine your approach
6. EXPLOIT: When you find a weakness, exploit it systematically
7. EXTRACT: Look for flags in all outputs using the provided pattern

CHALLENGE TYPES & STRATEGIES:
• Web: Check source, headers, directories, parameters, cookies, forms, SQL injection, XSS
• Crypto: Analyze ciphers, keys, encoding, hashing, frequency analysis, known attacks
• Reverse: Disassemble, analyze strings, debug, understand program flow
• Forensics: File analysis, metadata, hidden data, memory dumps, network captures
• Steganography: Hidden messages in images, audio, text
• Network: Port scans, protocol analysis, packet inspection
• Misc: Think outside the box, combine multiple techniques

IMPORTANT RULES:
• Be methodical but efficient - don't waste time on unlikely paths
• Always explain your reasoning clearly
• If one approach fails, try a different angle
• Look for flags in ALL command outputs, not just final results
• Use appropriate tools for each challenge type
• Build on previous findings - each command should advance your understanding
• When stuck, step back and reconsider the challenge from a different perspective"""

    def _build_analysis_prompt(self, url: str, flag_format: str, challenge_type: Optional[ChallengeType]) -> str:
        """Build initial analysis prompt."""
        # Auto-detect challenge type if not provided
        detected_type = self._detect_challenge_type(url)
        
        if challenge_type:
            type_hint = f" (Specified as: {challenge_type.value} challenge)"
        elif detected_type:
            type_hint = f" (Auto-detected as likely: {detected_type} challenge)"
        else:
            type_hint = " (Challenge type unknown - analyze to determine)"
        
        return f"""CHALLENGE ANALYSIS TASK:

Target: {url}
Flag Pattern: {flag_format}{type_hint}

Your mission is to analyze this target and determine the best approach to find the flag. 

INITIAL ANALYSIS:
You need to understand what type of challenge this is and what approach to take. Consider:

1. CHALLENGE TYPE ANALYSIS:
   - Look at the URL structure and any hints
   - What does the initial response tell you?
   - What type of challenge does this appear to be?

2. RECONNAISSANCE STRATEGY:
   - What information do you need to gather first?
   - What tools are most appropriate for this type of challenge?
   - How can you efficiently explore the attack surface?

3. GOAL ORIENTATION:
   - Remember you're looking for: {flag_format}
   - Where might such a flag typically be hidden?
   - What vulnerabilities or techniques might reveal it?

Start with commands that will help you understand the challenge type and gather initial intelligence. Be strategic - each command should give you valuable information to guide your next steps.

Provide your first set of reconnaissance commands with clear reasoning for each."""

    def _build_iteration_prompt(self, actions: List[AgentAction], flag_format: str, iteration: int) -> str:
        """Build prompt for iterative solving."""
        import re  # For regex operations
        
        # Use more actions for context, but intelligently summarize
        recent_actions = actions[-8:] if len(actions) > 8 else actions
        all_commands = [action.command for action in actions if action.command]
        
        # Build context with more intelligent summarization
        context = "PREVIOUS FINDINGS:\n"
        key_findings = []
        successful_commands = []
        failed_commands = []
        important_discoveries = []
        
        for action in recent_actions:
            if action.success:
                successful_commands.append(f"✓ {action.command}")
                if action.output and len(action.output.strip()) > 0:
                    # Extract STDOUT content for analysis
                    stdout_match = re.search(r'STDOUT:\n(.*?)(?:\n\nSTDERR:|$)', action.output, re.DOTALL)
                    if stdout_match:
                        stdout_content = stdout_match.group(1).strip()
                        if stdout_content:
                            # Check if this is an important discovery
                            is_important = any(keyword in stdout_content.lower() for keyword in [
                                'password', 'flag', 'error', 'code:', '$_get', '$_post', 'php', 'sql', 
                                'admin', 'login', 'token', 'key', 'secret', 'hash', 'md5', 'sha', 
                                'injection', 'xss', 'csrf', 'directory', 'file', 'path', 'include'
                            ])
                            
                            # Provide more context for important findings, less for routine output
                            if len(stdout_content) > 500:
                                # For long output, provide first 400 chars and last 100 chars
                                output_preview = stdout_content[:400] + "\n...[truncated]...\n" + stdout_content[-100:]
                            else:
                                output_preview = stdout_content
                            
                            if is_important:
                                important_discoveries.append(f"IMPORTANT - From '{action.command}':\n{output_preview}")
                            else:
                                key_findings.append(f"From '{action.command}':\n{output_preview}")
                    else:
                        # Fallback to original truncation
                        output_preview = action.output[:300] + "..." if len(action.output) > 300 else action.output
                        key_findings.append(f"From '{action.command}': {output_preview}")
            else:
                failed_commands.append(f"✗ {action.command}")
        
        # Prioritize important discoveries
        if important_discoveries:
            context += "\nIMPORTANT DISCOVERIES:\n" + "\n".join(important_discoveries[-2:]) + "\n"
        
        context += "\nSuccessful commands:\n" + "\n".join(successful_commands[-4:]) + "\n"
        if failed_commands:
            context += "\nFailed commands:\n" + "\n".join(failed_commands[-3:]) + "\n"
        
        if key_findings:
            context += "\nOther findings:\n" + "\n".join(key_findings[-2:]) + "\n"
        
        # Add command history to prevent repetition
        if len(all_commands) > 0:
            context += f"\nCOMMANDS ALREADY TRIED ({len(all_commands)} total):\n"
            context += "\n".join([f"- {cmd}" for cmd in all_commands[-10:]])  # Show last 10 commands
            context += "\n\nIMPORTANT: Do not repeat these exact commands. Try new approaches or variations.\n"
        
        # Determine what phase we're in
        if iteration == 0:
            phase = "RECONNAISSANCE"
            guidance = "You should be gathering initial information about the target."
        elif iteration < 3:
            phase = "ANALYSIS"
            guidance = "You should be analyzing the information you've gathered and forming hypotheses."
        elif iteration < 7:
            phase = "EXPLOITATION"
            guidance = "You should be testing specific vulnerabilities or attack vectors."
        else:
            phase = "DEEP INVESTIGATION"
            guidance = "Try alternative approaches or look deeper into previous findings."
        
        return f"""ITERATION {iteration + 1}/10 - PHASE: {phase}

{context}

TARGET FLAG PATTERN: {flag_format}

CURRENT SITUATION:
{guidance}

ANALYSIS REQUIRED:
Look carefully at the command outputs above. What do they reveal? Focus on the IMPORTANT DISCOVERIES section.

NEXT ACTIONS:
Based on your findings so far, what should you do next? Consider:
1. What have you learned from the command outputs above?
2. What vulnerabilities or attack vectors do the outputs suggest?
3. If you found code or error messages, how can you exploit them?
4. Do the outputs contain any parameters, URLs, or hints you should investigate?

SPECIFIC GUIDANCE:
- If you see PHP code with password checks, analyze the logic for bypasses
- If you see error messages, look for information disclosure
- If you see forms or parameters, test for injection vulnerabilities
- If you see file paths or directories, explore them further

AVOID REPETITION:
- Review the "COMMANDS ALREADY TRIED" section above
- Do NOT repeat the exact same commands
- If you need to retry something, modify the approach (different parameters, tools, etc.)
- If you're stuck, try a completely different angle or tool

PROGRESS CHECK:
- Iteration {iteration + 1}/10 - Are you making progress toward finding the flag?
- If you've found important code or vulnerabilities, focus on exploiting them
- If you're repeating similar reconnaissance, move to exploitation phase

If you're not making progress, consider:
- Analyzing the exact logic in any code you've found
- Testing specific parameter values based on the code logic
- Looking for type confusion or loose comparison vulnerabilities
- Exploring any hints in error messages or comments

Provide your next set of strategic commands."""

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
