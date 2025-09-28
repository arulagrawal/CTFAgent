let currentSession = null;
let websocket = null;
let startTime = null;

// Initialize the application
document.addEventListener('DOMContentLoaded', function () {
    checkApiHealth();
    setupEventListeners();
});

function setupEventListeners() {
    document.getElementById('challenge-form').addEventListener('submit', handleFormSubmit);
    document.getElementById('stop-btn').addEventListener('click', stopChallenge);
    document.getElementById('delete-btn').addEventListener('click', deleteSession);
    document.getElementById('handout-upload-btn').addEventListener('click', uploadHandouts);
    document.getElementById('target_type').addEventListener('change', onTargetTypeChange);
    document.getElementById('toggle-form-btn').addEventListener('click', toggleChallengeForm);
    const hostEl = document.getElementById('host');
    if (hostEl) {
        hostEl.addEventListener('paste', onHostPaste);
        hostEl.addEventListener('change', onHostChange);
    }
    onTargetTypeChange();
}

async function checkApiHealth() {
    try {
        const response = await fetch('/health');
        if (!response.ok) {
            showError('Backend API is not available. Please make sure the server is running.');
        }
    } catch (error) {
        showError('Backend API is not available. Please make sure the server is running.');
    }
}

function parseHostPort(text) {
    if (!text) return null;
    let cleaned = String(text).trim();
    if (!cleaned) return null;
    cleaned = cleaned.replace(/\s+/g, ' ');
    if (cleaned.includes('\n')) cleaned = cleaned.split('\n')[0];

    // Try formats like: "nc host port" (with optional flags) or "host port"
    let match = cleaned.match(/^(?:(?:nc|ncat|netcat)\s+(?:-[\w-]+\s+)*)?(\[[^\]]+\]|[A-Za-z0-9\.\-]+)\s+(\d{1,5})$/i);
    if (!match) {
        // Try "host:port" (supports IPv6 like [::1]:1337 or host:1337)
        match = cleaned.match(/^(\[[^\]]+\]|[^:\s]+):(\d{1,5})$/);
    }
    if (!match) return null;

    let host = match[1];
    const port = match[2];
    if (host.startsWith('[') && host.endsWith(']')) {
        host = host.slice(1, -1);
    }
    const portNum = parseInt(port, 10);
    if (Number.isNaN(portNum) || portNum < 1 || portNum > 65535) return null;
    return { host, port: String(portNum) };
}

function onHostPaste(event) {
    const text = (event.clipboardData || window.clipboardData).getData('text');
    const parsed = parseHostPort(text);
    if (parsed) {
        event.preventDefault();
        const hostInput = document.getElementById('host');
        const portInput = document.getElementById('port');
        if (hostInput) hostInput.value = parsed.host;
        if (portInput) portInput.value = parsed.port;
    }
}

function onHostChange(event) {
    const parsed = parseHostPort(event.target.value);
    if (parsed) {
        const hostInput = document.getElementById('host');
        const portInput = document.getElementById('port');
        if (hostInput) hostInput.value = parsed.host;
        if (portInput) portInput.value = parsed.port;
    }
}

function onTargetTypeChange() {
    const type = document.getElementById('target_type').value;
    const urlBlock = document.getElementById('url-input-block');
    const socketBlock = document.getElementById('socket-input-block');
    const urlInput = document.getElementById('url');
    const hostInput = document.getElementById('host');
    const portInput = document.getElementById('port');

    if (type === 'url') {
        urlBlock.classList.remove('hidden');
        socketBlock.classList.add('hidden');
        if (urlInput) urlInput.required = true;
        if (hostInput) hostInput.required = false;
        if (portInput) portInput.required = false;
    } else if (type === 'socket') {
        urlBlock.classList.add('hidden');
        socketBlock.classList.remove('hidden');
        if (urlInput) urlInput.required = false;
        if (hostInput) hostInput.required = true;
        if (portInput) portInput.required = true;
    } else {
        // handout-only
        urlBlock.classList.add('hidden');
        socketBlock.classList.add('hidden');
        if (urlInput) urlInput.required = false;
        if (hostInput) hostInput.required = false;
        if (portInput) portInput.required = false;
    }
}

async function handleFormSubmit(event) {
    event.preventDefault();

    const formEl = event.target;
    const formData = new FormData();
    const targetType = document.getElementById('target_type').value;
    const url = (document.getElementById('url').value || '').trim();
    const host = (document.getElementById('host')?.value || '').trim();
    const port = (document.getElementById('port')?.value || '').trim();
    const flagFormat = document.getElementById('flag_format').value;
    const challengeType = document.getElementById('challenge_type').value;
    const description = document.getElementById('description').value;
    const timeout = document.getElementById('timeout').value;
    const model = document.getElementById('model').value;
    const reasoningEffort = document.getElementById('reasoning_effort').value;
    const handoutsInput = document.getElementById('handouts');

    if (targetType === 'url') {
        if (!url) {
            showError('Please provide a URL.');
            return;
        }
        formData.append('url', url);
    } else if (targetType === 'socket') {
        if (!host || !port) {
            showError('Please provide both host and port.');
            return;
        }
        formData.append('host', host);
        formData.append('port', port);
    } else {
        // handout-only: no url/host/port
    }

    formData.append('flag_format', flagFormat);
    if (challengeType) formData.append('challenge_type', challengeType);
    if (description) formData.append('description', description);
    formData.append('timeout', timeout);
    formData.append('model', model);
    formData.append('reasoning_effort', reasoningEffort);
    if (handoutsInput && handoutsInput.files && handoutsInput.files.length > 0) {
        for (let i = 0; i < handoutsInput.files.length; i++) {
            formData.append('handouts', handoutsInput.files[i]);
        }
    }

    try {
        setLoading(true);
        hideError();

        const response = await fetch('/challenges/start', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to start challenge');
        }

        const data = await response.json();
        currentSession = { session_id: data.session_id };

        // Clear previous flag display
        document.getElementById('flag-found').classList.add('hidden');
        document.getElementById('error-info').classList.add('hidden');

        // Setup WebSocket connection for live updates
        setupWebSocket(data.session_id);

        // Initial status fetch only (WebSocket will handle live updates)
        await fetchSessionStatus(data.session_id);

        // Show session status and adjust layout
        const sessionStatusEl = document.getElementById('session-status');
        if (sessionStatusEl) sessionStatusEl.classList.remove('hidden', 'invisible');
        adjustLayoutForActiveSession();

        // Auto-minimize the form when starting a challenge
        setTimeout(() => {
            const challengeForm = document.getElementById('challenge-form');
            const toggleBtn = document.getElementById('toggle-form-btn');
            const collapseIcon = document.getElementById('collapse-icon');
            const expandIcon = document.getElementById('expand-icon');
            const container = document.getElementById('challenge-form-container');

            // Ensure toggle button is visible
            toggleBtn.classList.remove('hidden', 'invisible');

            // Auto-minimize the form with visual feedback
            challengeForm.classList.add('hidden');
            collapseIcon.classList.add('hidden');
            expandIcon.classList.remove('hidden');
            container.classList.add('opacity-75');
        }, 100);

    } catch (error) {
        showError(error.message);
        setLoading(false);
    }
}

function setupWebSocket(sessionId) {
    const wsUrl = `ws://${window.location.host}/ws/${sessionId}`;
    websocket = new WebSocket(wsUrl);

    websocket.onopen = function () {
        const conn = document.getElementById('connection-status');
        if (conn) conn.classList.add('hidden');
        console.log('WebSocket connected');
    };

    websocket.onmessage = function (event) {
        const message = JSON.parse(event.data);
        handleWebSocketMessage(message);
    };

    websocket.onclose = function () {
        const conn2 = document.getElementById('connection-status');
        if (conn2) conn2.classList.remove('hidden');
        console.log('WebSocket disconnected');
    };

    websocket.onerror = function (error) {
        console.error('WebSocket error:', error);
    };
}

function handleWebSocketMessage(message) {
    console.log('WebSocket message received:', message.type, message.data);
    switch (message.type) {
        case 'action':
            addActionToLog(message.data);
            break;
        case 'action_create':
            addActionToLog(message.data);
            break;
        case 'action_chunk':
            appendChunkToAction(message.data);
            break;
        case 'action_complete':
            finalizeAction(message.data);
            break;
        case 'flag_found':
            addActionToLog(message.data);
            if (currentSession) {
                fetchSessionStatus(currentSession.session_id);
            }
            break;
        case 'error':
            showError(message.data.error || 'An error occurred');
            break;
        case 'status':
            if (currentSession) {
                updateSessionStatus({ ...currentSession, ...message.data });
            }
            break;
        case 'ai_status':
            setAiThinking(!!(message.data && message.data.thinking));
            break;
    }
}

async function startPolling(sessionId) {
    // Initial fetch
    await fetchSessionStatus(sessionId);
    await fetchActions(sessionId);

    // Poll every 2 seconds for 30 seconds
    const pollInterval = setInterval(async () => {
        await fetchSessionStatus(sessionId);
        await fetchActions(sessionId);
    }, 2000);

    setTimeout(() => {
        clearInterval(pollInterval);
    }, 30000);
}

async function fetchSessionStatus(sessionId) {
    try {
        const response = await fetch(`/challenges/${sessionId}`);
        if (response.ok) {
            const session = await response.json();
            updateSessionStatus(session);
        }
    } catch (error) {
        console.error('Failed to fetch session status:', error);
    }
}

async function fetchActions(sessionId) {
    try {
        const response = await fetch(`/challenges/${sessionId}/actions`);
        if (response.ok) {
            const data = await response.json();
            updateActionLog(data.actions || []);
        }
    } catch (error) {
        console.error('Failed to fetch actions:', error);
    }
}

function updateSessionStatus(session) {
    currentSession = session;

    document.getElementById('session-id').textContent = session.session_id;
    const urlBlock = document.getElementById('url-status-block');
    const socketBlock = document.getElementById('socket-status-block');
    const urlEl = document.getElementById('challenge-url');
    const socketEl = document.getElementById('socket-target');

    if (session.challenge_url) {
        urlBlock.classList.remove('hidden');
        urlEl.textContent = session.challenge_url;
        urlEl.href = session.challenge_url;
    } else {
        urlBlock.classList.add('hidden');
        urlEl.textContent = '';
        urlEl.href = '#';
    }

    if (session.host && session.port) {
        socketBlock.classList.remove('hidden');
        socketEl.textContent = `${session.host}:${session.port}`;
    } else {
        socketBlock.classList.add('hidden');
        socketEl.textContent = '';
    }
    document.getElementById('flag-format').textContent = session.flag_format;
    // Estimated cost
    const costEl = document.getElementById('estimated-cost');
    if (typeof session.estimated_cost_usd === 'number') {
        costEl.textContent = `$${session.estimated_cost_usd.toFixed(6)}`;
    } else {
        costEl.textContent = '—';
    }

    // Update status badge
    const statusBadge = document.getElementById('status-badge');
    statusBadge.textContent = session.status.toUpperCase();
    statusBadge.className = `px-2 py-1 rounded-full text-xs font-medium ${getStatusClass(session.status)}`;

    // Update duration
    if (session.created_at) {
        // Parse the UTC timestamp correctly
        startTime = new Date(session.created_at);
        updateDuration();
    }

    // Show/hide buttons based on status
    if (session.status === 'running') {
        document.getElementById('stop-btn').classList.remove('hidden');
        document.getElementById('delete-btn').classList.add('hidden');
        const live = document.getElementById('live-indicator');
        if (live) live.classList.remove('hidden', 'invisible');
        adjustLayoutForActiveSession();
        setLoading(false);
    } else if (session.status === 'completed' || session.status === 'failed') {
        document.getElementById('stop-btn').classList.add('hidden');
        document.getElementById('delete-btn').classList.remove('hidden');
        const live2 = document.getElementById('live-indicator');
        if (live2) {
            live2.classList.add('invisible');
            live2.classList.remove('hidden');
        }
        resetLayoutAfterSession();
        setLoading(false);
    }

    // Show flag if found
    if (session.found_flag) {
        document.getElementById('found-flag').textContent = session.found_flag;
        document.getElementById('flag-found').classList.remove('hidden');
    }

    // Show error if present
    if (session.error_message) {
        document.getElementById('error-details').textContent = session.error_message;
        document.getElementById('error-info').classList.remove('hidden');
    }

    // Update handout list
    const listWrap = document.getElementById('handout-list');
    const listEl = document.getElementById('handout-files-ul');
    if (session.uploaded_files && session.uploaded_files.length > 0) {
        listWrap.classList.remove('hidden');
        listEl.innerHTML = '';
        session.uploaded_files.forEach(name => {
            const li = document.createElement('li');
            li.textContent = `/workspace/handouts/${name}`;
            listEl.appendChild(li);
        });
    } else {
        listWrap.classList.add('hidden');
        listEl.innerHTML = '';
    }
}

function updateActionLog(actions) {
    const logContainer = document.getElementById('action-log');

    if (actions.length === 0) {
        logContainer.innerHTML = '<div class="flex items-center justify-center flex-1"><p class="text-gray-400 text-center text-lg">No actions yet. Start a challenge to see the AI agent in action.</p></div>';
        return;
    }

    logContainer.innerHTML = '';
    actions.forEach(action => {
        addActionToLog(action);
    });

    // Scroll to bottom
    logContainer.scrollTop = logContainer.scrollHeight;
}

function addActionToLog(action) {
    const logContainer = document.getElementById('action-log');

    // Remove "no actions" message if present
    const noActionsMsg = logContainer.querySelector('p');
    if (noActionsMsg && noActionsMsg.textContent.includes('No actions yet')) {
        logContainer.innerHTML = '';
    }

    const actionDiv = document.createElement('div');
    // Highlight currently running actions
    let borderClass = 'border-gray-200';
    let backgroundClass = 'bg-transparent';

    if (action.output === undefined && action.action_type !== 'flag_found' && action.action_type !== 'ai_reasoning') {
        // This is likely a currently running command
        borderClass = 'border-blue-400';
        backgroundClass = 'bg-blue-50';
    } else if (action.action_type === 'flag_found') {
        borderClass = 'border-green-400';
        backgroundClass = 'bg-green-50';
    }

    actionDiv.className = `border-l-4 ${borderClass} ${backgroundClass} pl-4 pb-4 rounded-r-lg`;
    if (action.id) {
        actionDiv.dataset.actionId = action.id;
    }

    const timestamp = new Date(action.timestamp).toLocaleTimeString();
    const icon = getActionIcon(action);

    // Build static HTML structure; inject dynamic content via textContent
    actionDiv.innerHTML = `
        <!-- Header -->
        <div class="flex items-start justify-between mb-3">
            <div class="flex items-center">
                ${icon}
                <span class="ml-2 font-semibold text-base text-gray-800">
                    ${action.action_type.replace('_', ' ').toUpperCase()}
                </span>
            </div>
            <div class="flex items-center text-sm text-gray-500">
                <svg class="w-4 h-4 mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"></path>
                </svg>
                ${timestamp}
            </div>
        </div>
        
        <!-- Content Layout -->
        <div class="flex gap-4">
            <!-- Left Side: Command and Output -->
            <div class="flex-1 min-w-0">
                ${action.command ? `
                    <div class=\"mb-3\">
                        <div class=\"text-sm font-medium text-gray-600 mb-2\">Command:</div>
                        <code class=\"js-command bg-gray-900 text-green-400 px-3 py-2 rounded-lg text-sm block font-mono\"></code>
                    </div>
                ` : ''}
                
                ${action.output !== undefined ? `
                    <div class=\"mb-3\">
                        <div class=\"text-sm font-medium text-gray-600 mb-2\">Output:</div>
                        <pre class=\"js-output terminal text-sm max-h-64 overflow-y-auto whitespace-pre-wrap p-3 rounded-lg border\"></pre>
                    </div>
                ` : ''}
                
                ${action.action_type === 'flag_found' ? `
                    <div class=\"mt-2\">
                        <button class=\"js-copy-flag bg-green-600 hover:bg-green-700 text-white text-xs font-medium py-1 px-2 rounded transition-colors duration-200\" onclick=\"copyActionFlag(event)\" title=\"Copy flag to clipboard\">
                            <svg class=\"w-3 h-3 inline mr-1\" fill=\"none\" stroke=\"currentColor\" viewBox=\"0 0 24 24\"><path stroke-linecap=\"round\" stroke-linejoin=\"round\" stroke-width=\"2\" d=\"M5 13l4 4L19 7\"></path></svg>
                            Copy flag
                        </button>
                    </div>
                ` : ''}

                ${!action.success && action.action_type !== 'flag_found' ? `
                    <div class=\"p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700\">
                        ⚠️ This action failed
                    </div>
                ` : ''}
            </div>
            
            <!-- Right Side: Reasoning -->
            ${action.reasoning ? `
                <div class=\"w-80 flex-shrink-0\">
                    <div class=\"text-sm font-medium text-gray-600 mb-2\">Reasoning:</div>
                    <div class=\"p-3 bg-blue-50 rounded-lg text-sm text-blue-800 border-l-4 border-blue-200 h-fit\">
                        <span class=\"js-reasoning\"></span>
                    </div>
                </div>
            ` : ''}
        </div>
    `;

    logContainer.appendChild(actionDiv);
    // Safely inject dynamic text
    if (action.reasoning) {
        const r = actionDiv.querySelector('.js-reasoning');
        if (r) r.textContent = String(action.reasoning);
    }
    if (action.command) {
        const c = actionDiv.querySelector('.js-command');
        if (c) c.textContent = String(action.command);
    }
    if (action.output !== undefined) {
        const o = actionDiv.querySelector('.js-output');
        if (o) o.textContent = String(action.output || '');
    }
    logContainer.scrollTop = logContainer.scrollHeight;
}

function appendChunkToAction(data) {
    try {
        const { id, channel, chunk } = data || {};
        if (!id) return;
        const logContainer = document.getElementById('action-log');
        const el = logContainer.querySelector(`[data-action-id="${id}"] .js-output`);
        if (el) {
            el.textContent += String(chunk || '');
            el.scrollTop = el.scrollHeight;
        }
    } catch (e) {
        console.error('appendChunkToAction error', e);
    }
}

function finalizeAction(data) {
    try {
        const { id, final_output, success } = data || {};
        if (!id) return;
        const logContainer = document.getElementById('action-log');
        const wrapper = logContainer.querySelector(`[data-action-id="${id}"]`);
        if (!wrapper) return;
        const outEl = wrapper.querySelector('.js-output');
        if (outEl && typeof final_output === 'string') {
            outEl.textContent = final_output;
        }
        if (typeof success === 'boolean' && success === false) {
            // Add failure tag if not already present
            const exists = wrapper.querySelector('.js-failtag');
            if (!exists) {
                const fail = document.createElement('div');
                fail.className = 'js-failtag mt-2 p-2 bg-red-50 border border-red-200 rounded text-sm text-red-700';
                fail.textContent = '⚠️ This action failed';
                wrapper.appendChild(fail);
            }
        }
        logContainer.scrollTop = logContainer.scrollHeight;
    } catch (e) {
        console.error('finalizeAction error', e);
    }
}

function setAiThinking(thinking) {
    const badge = document.getElementById('ai-thinking');
    if (!badge) return;
    if (thinking) {
        badge.classList.remove('hidden', 'invisible');
    } else {
        badge.classList.add('invisible');
        badge.classList.remove('hidden');
    }
}

function getActionIcon(action) {
    if (action.action_type === 'flag_found') {
        return '<svg class="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>';
    }
    if (!action.success) {
        return '<svg class="w-4 h-4 text-red-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2m7-2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>';
    }
    if (action.action_type === 'ai_reasoning') {
        return '<svg class="w-4 h-4 text-yellow-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"></path></svg>';
    }
    return '<svg class="w-4 h-4 text-blue-500" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2"></path></svg>';
}

function getStatusClass(status) {
    switch (status) {
        case 'pending': return 'bg-yellow-100 text-yellow-800';
        case 'running': return 'bg-blue-100 text-blue-800';
        case 'completed': return 'bg-green-100 text-green-800';
        case 'failed': return 'bg-red-100 text-red-800';
        default: return 'bg-gray-100 text-gray-800';
    }
}

function updateDuration() {
    if (!startTime) return;

    const now = new Date();
    const duration = Math.floor((now - startTime) / 1000);
    const minutes = Math.floor(duration / 60);
    const seconds = duration % 60;

    document.getElementById('duration').textContent = `${minutes}:${seconds.toString().padStart(2, '0')}`;

    if (currentSession && currentSession.status === 'running') {
        setTimeout(updateDuration, 1000);
    }
}

async function stopChallenge() {
    if (!currentSession) return;

    try {
        const response = await fetch(`/challenges/${currentSession.session_id}/stop`, {
            method: 'POST'
        });

        if (response.ok) {
            await fetchSessionStatus(currentSession.session_id);
        } else {
            const error = await response.json();
            showError(error.detail || 'Failed to stop challenge');
        }
    } catch (error) {
        showError(error.message);
    }
}

async function deleteSession() {
    if (!currentSession) return;

    try {
        const response = await fetch(`/challenges/${currentSession.session_id}`, {
            method: 'DELETE'
        });

        if (response.ok) {
            currentSession = null;
            const sessionStatusEl = document.getElementById('session-status');
            if (sessionStatusEl) {
                sessionStatusEl.classList.add('invisible');
                sessionStatusEl.classList.remove('hidden');
            }
            document.getElementById('action-log').innerHTML = '<div class="flex items-center justify-center flex-1"><p class="text-gray-400 text-center text-lg">No actions yet. Start a challenge to see the AI agent in action.</p></div>';
            const live = document.getElementById('live-indicator');
            if (live) {
                live.classList.add('invisible');
                live.classList.remove('hidden');
            }
            resetLayoutAfterSession();
            if (websocket) {
                websocket.close();
            }
        } else {
            const error = await response.json();
            showError(error.detail || 'Failed to delete session');
        }
    } catch (error) {
        showError(error.message);
    }
}

function setLoading(loading) {
    const startBtn = document.getElementById('start-btn');
    const startText = document.getElementById('start-text');

    if (loading) {
        startBtn.disabled = true;
        startBtn.classList.add('opacity-50', 'cursor-not-allowed');
        startText.textContent = 'Starting...';
    } else {
        startBtn.disabled = false;
        startBtn.classList.remove('opacity-50', 'cursor-not-allowed');
        startText.textContent = 'Start Challenge';
    }
}

function showError(message) {
    document.getElementById('error-message').textContent = message;
    document.getElementById('error-alert').classList.remove('hidden');
}

function hideError() {
    document.getElementById('error-alert').classList.add('hidden');
}

async function uploadHandouts() {
    if (!currentSession) {
        showError('Start a session before uploading handouts.');
        return;
    }
    const input = document.getElementById('handout-input');
    const files = input.files;
    const errEl = document.getElementById('handout-upload-error');
    errEl.classList.add('hidden');
    errEl.textContent = '';
    if (!files || files.length === 0) {
        return;
    }
    try {
        for (let i = 0; i < files.length; i++) {
            const fd = new FormData();
            fd.append('file', files[i]);
            const res = await fetch(`/challenges/${currentSession.session_id}/upload`, {
                method: 'POST',
                body: fd
            });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || 'Failed to upload file');
            }
        }
        await fetchSessionStatus(currentSession.session_id);
        input.value = '';
    } catch (e) {
        errEl.textContent = e.message || 'Failed to upload file(s)';
        errEl.classList.remove('hidden');
    }
}

function copyFlag() {
    const flagText = document.getElementById('found-flag').textContent;
    if (flagText) {
        navigator.clipboard.writeText(flagText).then(() => {
            // Show brief confirmation
            const btn = event.target;
            const originalText = btn.innerHTML;
            btn.innerHTML = '<svg class="w-3 h-3 inline mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg> Copied!';
            setTimeout(() => { btn.innerHTML = originalText; }, 2000);
        }).catch(err => { console.error('Failed to copy flag:', err); });
    }
}

function copyActionFlag(event) {
    try {
        const btn = event && (event.currentTarget || event.target);
        const wrapper = btn ? btn.closest('[data-action-id]') : null;
        let text = '';
        const outEl = wrapper ? wrapper.querySelector('.js-output') : null;
        if (outEl && outEl.textContent) {
            text = String(outEl.textContent).trim();
        }
        if (!text) {
            const foundEl = document.getElementById('found-flag');
            if (foundEl && foundEl.textContent) {
                text = String(foundEl.textContent).trim();
            }
        }
        if (!text) return;
        const originalText = btn ? btn.innerHTML : '';
        navigator.clipboard.writeText(text).then(() => {
            if (btn) {
                btn.innerHTML = '<svg class="w-3 h-3 inline mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7"></path></svg> Copied!';
                setTimeout(() => { btn.innerHTML = originalText; }, 2000);
            }
        }).catch(() => { });
    } catch (e) {
        console.error('copyActionFlag error', e);
    }
}

// Layout adjustment functions
function adjustLayoutForActiveSession() {
    const mainGrid = document.getElementById('main-grid');
    const leftColumn = document.getElementById('left-column');
    const rightColumn = document.getElementById('right-column');
    const toggleBtn = document.getElementById('toggle-form-btn');

    // Show the toggle button
    toggleBtn.classList.remove('hidden', 'invisible');

    // Layout is already optimized - keep active layout by default
    mainGrid.className = 'grid grid-cols-1 xl:grid-cols-5 gap-4 flex-1';
    leftColumn.className = 'xl:col-span-1 space-y-4';
    rightColumn.className = 'xl:col-span-4';

    // Ensure proper height and flex layout for the container
    const rightColumnContainer = rightColumn.querySelector('div');
    if (rightColumnContainer) {
        rightColumnContainer.style.height = 'calc(100vh - 120px)';
        rightColumnContainer.classList.add('flex', 'flex-col');
    }
}

function resetLayoutAfterSession() {
    const mainGrid = document.getElementById('main-grid');
    const leftColumn = document.getElementById('left-column');
    const rightColumn = document.getElementById('right-column');
    const toggleBtn = document.getElementById('toggle-form-btn');
    const challengeForm = document.getElementById('challenge-form');
    const collapseIcon = document.getElementById('collapse-icon');
    const expandIcon = document.getElementById('expand-icon');

    // Keep active layout to avoid jank when sessions start/stop
    mainGrid.className = 'grid grid-cols-1 xl:grid-cols-5 gap-4 flex-1';
    leftColumn.className = 'xl:col-span-1 space-y-4';
    rightColumn.className = 'xl:col-span-4';

    // Hide the toggle button and ensure form is expanded
    toggleBtn.classList.add('hidden');
    challengeForm.classList.remove('hidden');
    collapseIcon.classList.remove('hidden');
    expandIcon.classList.add('hidden');

    // Reset visual state
    const container = document.getElementById('challenge-form-container');
    if (container) {
        container.classList.remove('opacity-75');
    }

    // Ensure proper height and flex layout for the container
    const rightColumnContainer = rightColumn.querySelector('div');
    if (rightColumnContainer) {
        rightColumnContainer.style.height = 'calc(100vh - 120px)';
        rightColumnContainer.classList.add('flex', 'flex-col');
    }
}

function toggleChallengeForm() {
    const challengeForm = document.getElementById('challenge-form');
    const collapseIcon = document.getElementById('collapse-icon');
    const expandIcon = document.getElementById('expand-icon');
    const container = document.getElementById('challenge-form-container');

    if (challengeForm.classList.contains('hidden')) {
        // Expand form
        challengeForm.classList.remove('hidden');
        collapseIcon.classList.remove('hidden');
        expandIcon.classList.add('hidden');
        container.classList.remove('opacity-75');
    } else {
        // Collapse form
        challengeForm.classList.add('hidden');
        collapseIcon.classList.add('hidden');
        expandIcon.classList.remove('hidden');
        container.classList.add('opacity-75');
    }
}
