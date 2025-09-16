#!/usr/bin/env python3
"""
Simple test script to verify CTF Agent setup
"""

import requests
import json
import time
import sys
import os
from urllib.parse import urljoin

API_BASE = "http://localhost:8000"

def test_api_health():
    """Test if the API is running and healthy"""
    try:
        response = requests.get(f"{API_BASE}/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"✅ API Health: {data}")
            return True
        else:
            print(f"❌ API Health check failed: {response.status_code}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"❌ API connection failed: {e}")
        return False

def test_challenge_start():
    """Test starting a simple challenge"""
    try:
        # Use httpbin.org for a simple test
        test_request = {
            "url": "https://httpbin.org/json",
            "flag_format": "test{.*}",
            "challenge_type": "web",
            "description": "Simple test challenge using httpbin",
            "timeout": 60
        }
        
        print("🧪 Starting test challenge...")
        response = requests.post(f"{API_BASE}/challenges/start", 
                               json=test_request, 
                               timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            session_id = data.get("session_id")
            print(f"✅ Challenge started: {session_id}")
            
            # Wait a bit and check status
            time.sleep(3)
            status_response = requests.get(f"{API_BASE}/challenges/{session_id}")
            if status_response.status_code == 200:
                status_data = status_response.json()
                print(f"📊 Session status: {status_data.get('status')}")
                
                # Stop the session
                stop_response = requests.post(f"{API_BASE}/challenges/{session_id}/stop")
                if stop_response.status_code == 200:
                    print("🛑 Session stopped successfully")
                
                return True
            else:
                print(f"❌ Failed to get session status: {status_response.status_code}")
                return False
        else:
            print(f"❌ Failed to start challenge: {response.status_code}")
            print(f"Response: {response.text}")
            return False
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Challenge test failed: {e}")
        return False

def main():
    """Run all tests"""
    print("🔍 Testing CTF Agent Setup...")
    print("=" * 50)
    
    # Check if API is running
    if not test_api_health():
        print("\n❌ API is not running. Please start the backend first.")
        print("Run: python -m backend.main")
        sys.exit(1)
    
    print("\n🧪 Running integration test...")
    
    # Test challenge functionality
    if test_challenge_start():
        print("\n✅ All tests passed! CTF Agent is working correctly.")
    else:
        print("\n❌ Some tests failed. Check the logs for details.")
        sys.exit(1)

if __name__ == "__main__":
    main()
