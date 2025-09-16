import React, { useState, useCallback, useEffect } from 'react';
import axios from 'axios';
import { Shield, Github, ExternalLink } from 'lucide-react';

import { ChallengeForm } from './components/ChallengeForm';
import { ActionLog } from './components/ActionLog';
import { SessionStatus } from './components/SessionStatus';
import { useWebSocket } from './hooks/useWebSocket';
import { ChallengeRequest, ChallengeSession, AgentAction, WebSocketMessage } from './types';

const API_BASE = 'http://localhost:8000';

function App() {
  const [currentSession, setCurrentSession] = useState<ChallengeSession | null>(null);
  const [actions, setActions] = useState<AgentAction[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleWebSocketMessage = useCallback((message: WebSocketMessage) => {
    console.log('WebSocket message:', message);
    
    switch (message.type) {
      case 'action':
        setActions(prev => [...prev, message.data as AgentAction]);
        break;
      case 'flag_found':
        setActions(prev => [...prev, message.data as AgentAction]);
        // Refresh session to get updated status
        if (currentSession) {
          fetchSession(currentSession.session_id);
        }
        break;
      case 'error':
        setError(message.data.error || 'An error occurred');
        break;
      case 'status':
        // Handle status updates
        if (currentSession) {
          setCurrentSession(prev => prev ? { ...prev, ...message.data } : null);
        }
        break;
    }
  }, [currentSession]);

  const { isConnected } = useWebSocket({
    sessionId: currentSession?.session_id || null,
    onMessage: handleWebSocketMessage
  });

  const fetchSession = async (sessionId: string) => {
    try {
      const response = await axios.get(`${API_BASE}/challenges/${sessionId}`);
      setCurrentSession(response.data);
    } catch (err) {
      console.error('Failed to fetch session:', err);
      setError('Failed to fetch session status');
    }
  };

  const fetchActions = async (sessionId: string) => {
    try {
      const response = await axios.get(`${API_BASE}/challenges/${sessionId}/actions`);
      setActions(response.data.actions || []);
    } catch (err) {
      console.error('Failed to fetch actions:', err);
    }
  };

  const handleStartChallenge = async (request: ChallengeRequest) => {
    setIsLoading(true);
    setError(null);
    setActions([]);
    
    try {
      const response = await axios.post(`${API_BASE}/challenges/start`, request);
      const sessionId = response.data.session_id;
      
      // Fetch initial session data
      await fetchSession(sessionId);
      
      // Poll for updates until WebSocket connects
      const pollInterval = setInterval(async () => {
        await fetchSession(sessionId);
        await fetchActions(sessionId);
      }, 2000);
      
      // Stop polling after 30 seconds (WebSocket should be connected by then)
      setTimeout(() => {
        clearInterval(pollInterval);
      }, 30000);
      
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to start challenge');
    } finally {
      setIsLoading(false);
    }
  };

  const handleStopChallenge = async () => {
    if (!currentSession) return;
    
    try {
      await axios.post(`${API_BASE}/challenges/${currentSession.session_id}/stop`);
      await fetchSession(currentSession.session_id);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to stop challenge');
    }
  };

  const handleDeleteSession = async () => {
    if (!currentSession) return;
    
    try {
      await axios.delete(`${API_BASE}/challenges/${currentSession.session_id}`);
      setCurrentSession(null);
      setActions([]);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to delete session');
    }
  };

  // Check API health on mount
  useEffect(() => {
    const checkHealth = async () => {
      try {
        await axios.get(`${API_BASE}/health`);
      } catch (err) {
        setError('Backend API is not available. Please make sure the server is running.');
      }
    };
    
    checkHealth();
  }, []);

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white shadow-sm border-b">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center py-4">
            <div className="flex items-center">
              <Shield className="w-8 h-8 text-primary-600 mr-3" />
              <div>
                <h1 className="text-2xl font-bold text-gray-900">CTF Agent</h1>
                <p className="text-sm text-gray-500">AI-powered CTF challenge solver</p>
              </div>
            </div>
            
            <div className="flex items-center space-x-4">
              <a
                href="https://github.com/your-username/ctf-agent"
                target="_blank"
                rel="noopener noreferrer"
                className="text-gray-500 hover:text-gray-700"
              >
                <Github className="w-5 h-5" />
              </a>
              <a
                href={API_BASE}
                target="_blank"
                rel="noopener noreferrer"
                className="text-gray-500 hover:text-gray-700"
              >
                <ExternalLink className="w-5 h-5" />
              </a>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {/* Error Alert */}
        {error && (
          <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg">
            <div className="flex items-center">
              <div className="text-red-800">
                <strong>Error:</strong> {error}
              </div>
              <button
                onClick={() => setError(null)}
                className="ml-auto text-red-500 hover:text-red-700"
              >
                ×
              </button>
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
          {/* Left Column */}
          <div className="space-y-6">
            <ChallengeForm 
              onSubmit={handleStartChallenge} 
              isLoading={isLoading}
            />
            
            <SessionStatus
              session={currentSession}
              onStop={handleStopChallenge}
              onDelete={handleDeleteSession}
              isConnected={isConnected}
            />
          </div>

          {/* Right Column */}
          <div>
            <ActionLog 
              actions={actions} 
              isLive={currentSession?.status === 'running' && isConnected}
            />
          </div>
        </div>

        {/* Instructions */}
        <div className="mt-12 card">
          <h3 className="text-lg font-medium mb-4">How to Use</h3>
          <div className="space-y-3 text-sm text-gray-600">
            <div className="flex items-start">
              <span className="bg-primary-100 text-primary-800 rounded-full w-6 h-6 flex items-center justify-center text-xs font-medium mr-3 mt-0.5">1</span>
              <div>
                <strong>Enter Challenge URL:</strong> Provide the URL of the CTF challenge you want to solve.
              </div>
            </div>
            <div className="flex items-start">
              <span className="bg-primary-100 text-primary-800 rounded-full w-6 h-6 flex items-center justify-center text-xs font-medium mr-3 mt-0.5">2</span>
              <div>
                <strong>Set Flag Format:</strong> Specify the regex pattern for the flag (e.g., flag{'{'}.{'*'}{'}'}, CTF{'{'}.{'*'}{'}'}).
              </div>
            </div>
            <div className="flex items-start">
              <span className="bg-primary-100 text-primary-800 rounded-full w-6 h-6 flex items-center justify-center text-xs font-medium mr-3 mt-0.5">3</span>
              <div>
                <strong>Watch the AI Work:</strong> Monitor real-time actions as the AI agent analyzes and attempts to solve the challenge.
              </div>
            </div>
            <div className="flex items-start">
              <span className="bg-primary-100 text-primary-800 rounded-full w-6 h-6 flex items-center justify-center text-xs font-medium mr-3 mt-0.5">4</span>
              <div>
                <strong>Get Results:</strong> The agent will automatically stop when it finds the flag or reaches the timeout.
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default App;
