#!/usr/bin/env python3
"""
Test script to verify AI Agent improvements
"""

import asyncio
import os
import sys
from backend.ai_agent import CTFAgent
from backend.models import ChallengeType

async def test_ai_prompts():
    """Test the AI agent prompt generation"""
    # Mock API key for testing prompts (won't actually call OpenAI)
    agent = CTFAgent("test-key")
    
    print("🧪 Testing AI Agent Prompt Generation")
    print("=" * 50)
    
    # Test challenge type detection
    test_urls = [
        "https://example.com/web-challenge",
        "https://crypto.ctf.com/cipher",
        "https://ctf.com/reverse-me.exe",
        "https://forensics.challenge.com/memory.dump",
        "https://unknown.com/challenge"
    ]
    
    for url in test_urls:
        detected = agent._detect_challenge_type(url)
        print(f"URL: {url}")
        print(f"Detected Type: {detected}")
        print()
    
    # Test prompt generation
    print("📝 Testing Analysis Prompt Generation")
    print("-" * 30)
    
    analysis_prompt = agent._build_analysis_prompt(
        "https://example.com/web-challenge",
        "flag{.*}",
        ChallengeType.WEB
    )
    
    print("Analysis Prompt Preview:")
    print(analysis_prompt[:300] + "...")
    print()
    
    # Test iteration prompt
    print("📝 Testing Iteration Prompt Generation")
    print("-" * 30)
    
    from backend.models import AgentAction
    from datetime import datetime
    
    mock_actions = [
        AgentAction(
            timestamp=datetime.now(),
            action_type="command_execution",
            command="curl -v https://example.com",
            output="HTTP/1.1 200 OK\nContent-Type: text/html\n<html>...</html>",
            reasoning="Initial reconnaissance of the target",
            success=True
        )
    ]
    
    iteration_prompt = agent._build_iteration_prompt(mock_actions, "flag{.*}", 1)
    print("Iteration Prompt Preview:")
    print(iteration_prompt[:400] + "...")
    print()
    
    # Test response parsing
    print("🔧 Testing AI Response Parsing")
    print("-" * 30)
    
    test_responses = [
        # JSON format
        '''```json
        [
            {
                "command": "curl -v https://example.com",
                "reasoning": "Initial reconnaissance"
            }
        ]
        ```''',
        
        # Text format
        '''Command: nmap -sV target.com
        Reasoning: Scan for open services
        
        Command: curl -I https://target.com
        Reasoning: Check HTTP headers''',
        
        # Mixed format
        '''Let me start with basic reconnaissance:
        
        curl -v https://example.com
        
        This will help us understand the web server and response.'''
    ]
    
    for i, response in enumerate(test_responses):
        print(f"Test Response {i+1}:")
        commands = agent._parse_ai_response(response)
        print(f"Extracted {len(commands)} commands:")
        for cmd in commands:
            print(f"  - {cmd['command'][:50]}...")
        print()
    
    print("✅ All prompt tests completed successfully!")

def main():
    """Run the tests"""
    try:
        asyncio.run(test_ai_prompts())
    except Exception as e:
        print(f"❌ Test failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
