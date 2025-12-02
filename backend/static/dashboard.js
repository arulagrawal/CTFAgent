const DASHBOARD_REFRESH_MS = 5000;
let refreshTimer = null;
const activeWriteupSessions = new Map();

(function initDashboard() {
    try {
        const refreshBtn = document.getElementById('refresh-btn');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => fetchSessions(true));
        }
        initFlagFormatSetting();
        fetchSessions();
        refreshTimer = setInterval(fetchSessions, DASHBOARD_REFRESH_MS);
    } catch (err) {
        console.error('Failed to initialize dashboard', err);
    }
})();

async function fetchSessions(isManual = false) {
    const errorEl = document.getElementById('dashboard-error');
    if (errorEl) errorEl.classList.add('hidden');

    try {
        const res = await fetch('/challenges');
        if (!res.ok) {
            throw new Error('Failed to load sessions');
        }
        const data = await res.json();
        renderSessions(data.sessions || []);
        updateWriteupBanner();
        if (isManual) {
            showTransientMessage('Manual refresh complete');
        }
    } catch (err) {
        console.error('Dashboard fetch failed', err);
        if (errorEl) {
            errorEl.textContent = err.message || 'Failed to load sessions';
            errorEl.classList.remove('hidden');
        }
    }
}

function markWriteupActive(sessionId, label) {
    if (!sessionId) return;
    const entryLabel = (label || '').trim() || `session ${sessionId.slice(0, 8)}`;
    activeWriteupSessions.set(sessionId, {
        label: entryLabel,
        startedAt: Date.now(),
    });
    updateWriteupBanner();
}

function clearWriteupActive(sessionId) {
    if (!sessionId) return;
    if (activeWriteupSessions.delete(sessionId)) {
        updateWriteupBanner();
    }
}

function isWriteupActive(sessionId) {
    return activeWriteupSessions.has(sessionId);
}

function updateWriteupBanner() {
    const statusEl = document.getElementById('dashboard-status');
    const textEl = document.getElementById('dashboard-status-text');
    if (!statusEl || !textEl) return;

    if (!activeWriteupSessions.size) {
        statusEl.classList.add('hidden');
        textEl.textContent = '';
        return;
    }

    const labels = Array.from(activeWriteupSessions.values()).map((entry) => entry.label);
    let message = '';
    if (labels.length === 1) {
        message = `Generating write-up for ${labels[0]}…`;
    } else {
        message = `Generating write-ups for ${labels.length} sessions (${labels.join(', ')})…`;
    }
    textEl.textContent = message;
    statusEl.classList.remove('hidden');
}

function cleanupWriteupState(sessions) {
    if (!sessions || !sessions.length) {
        if (activeWriteupSessions.size) {
            activeWriteupSessions.clear();
            updateWriteupBanner();
        }
        return;
    }
    const sessionMap = new Map(sessions.map((session) => [session.session_id, session]));
    let mutated = false;
    for (const sessionId of Array.from(activeWriteupSessions.keys())) {
        const session = sessionMap.get(sessionId);
        if (!session || session.status !== 'completed') {
            activeWriteupSessions.delete(sessionId);
            mutated = true;
        }
    }
    if (mutated) {
        updateWriteupBanner();
    }
}

function renderSessions(sessions) {
    const body = document.getElementById('sessions-body');
    const emptyState = document.getElementById('empty-state');
    if (!body) return;

    cleanupWriteupState(sessions);
    body.innerHTML = '';
    if (!sessions.length) {
        if (emptyState) emptyState.classList.remove('hidden');
        updateWriteupBanner();
        return;
    }
    if (emptyState) emptyState.classList.add('hidden');

    for (const session of sessions) {
        const row = document.createElement('tr');
        row.className = 'hover:bg-gray-50 transition';
        const safeSessionId = escapeHtml(session.session_id);
        const safeChallengeName = escapeHtml(session.challenge_name || '');
        const safeType = escapeHtml(session.challenge_type || 'auto');
        row.innerHTML = `
            <td class="px-4 py-3 whitespace-nowrap">
                ${renderStatusBadge(session.status)}
            </td>
            <td class="px-4 py-3 text-sm text-gray-900">
                ${safeChallengeName ? `<div class="text-sm font-semibold text-gray-900">${truncate(safeChallengeName, 32)}</div>` : ''}
                <div class="font-mono">${truncate(safeSessionId, 12)}</div>
                <div class="text-xs text-gray-500 font-mono">${safeSessionId}</div>
            </td>
            <td class="px-4 py-3">
                <div class="text-sm text-gray-900">${formatTarget(session)}</div>
                <div class="text-xs text-gray-500">${safeType}</div>
            </td>
            <td class="px-4 py-3">
                ${renderFlagCell(session)}
            </td>
            <td class="px-4 py-3 text-sm text-gray-500">
                ${formatTimestamp(session.created_at)}
            </td>
            <td class="px-4 py-3 text-sm text-gray-500">
                ${formatTimestamp(session.updated_at)}
            </td>
            <td class="px-4 py-3 text-sm text-gray-500">
                ${renderActions(session)}
            </td>
        `;
        body.appendChild(row);
    }

    wireActionButtons();
}

function renderStatusBadge(status) {
    const styles = {
        running: 'bg-green-100 text-green-800',
        completed: 'bg-blue-100 text-blue-800',
        failed: 'bg-red-100 text-red-800',
        pending: 'bg-yellow-100 text-yellow-800',
    };
    const cls = styles[status] || 'bg-gray-100 text-gray-800';
    return `<span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${cls}">${status?.toUpperCase() || 'UNKNOWN'}</span>`;
}

function renderFlagCell(session) {
    if (session.found_flag) {
        const safeFlag = escapeHtml(session.found_flag);
        return `
            <div class="flex items-center space-x-2">
                <code class="text-green-700 bg-green-50 border border-green-100 rounded px-2 py-1 text-xs break-all">${safeFlag}</code>
                <button class="copy-flag text-xs text-green-700 hover:text-green-900 underline" data-flag="${encodeURIComponent(session.found_flag)}">
                    Copy
                </button>
            </div>
        `;
    }
    return '<span class="text-xs text-gray-400">—</span>';
}

function renderActions(session) {
    const safeSessionId = escapeHtml(session.session_id);
    const viewBtn = `<button class="view-session inline-flex items-center px-3 py-1.5 border border-gray-300 rounded-md text-xs font-medium text-gray-700 hover:bg-gray-50" data-session="${safeSessionId}">View</button>`;
    const isSaving = isWriteupActive(session.session_id);
    const challengeLabel = encodeURIComponent(session.challenge_name || session.session_id);

    let actionBtn;
    if (session.status === 'running' || session.status === 'pending') {
        actionBtn = `<button class="stop-session inline-flex items-center px-3 py-1.5 border border-red-300 rounded-md text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-40" data-session="${safeSessionId}">Stop</button>`;
    } else {
        actionBtn = `<button class="delete-session inline-flex items-center px-3 py-1.5 border border-gray-300 rounded-md text-xs font-medium text-gray-700 hover:bg-red-50 hover:text-red-700 hover:border-red-300 transition" data-session="${safeSessionId}">Delete</button>`;
    }

    let saveBtn = '';
    if (session.status === 'completed') {
        const saveClasses = [
            'save-session',
            'inline-flex',
            'items-center',
            'px-3',
            'py-1.5',
            'border',
            'border-green-300',
            'rounded-md',
            'text-xs',
            'font-medium',
            'text-green-700',
            'hover:bg-green-50',
            'transition',
        ];
        if (isSaving) {
            saveClasses.push('opacity-70', 'cursor-wait');
        }
        const spinner = isSaving
            ? '<svg class="w-3 h-3 mr-1 text-green-700 animate-spin" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a12 12 0 00-12 12h4z"></path></svg>'
            : '';
        const saveLabel = isSaving ? 'Generating…' : 'Save';
        const disabledAttr = isSaving ? 'disabled data-saving="true"' : '';
        saveBtn = `<button class="${saveClasses.join(' ')}" data-session="${safeSessionId}" data-challenge="${challengeLabel}" ${disabledAttr}>${spinner}${saveLabel}</button>`;
    }

    const savingNotice = isSaving
        ? `<div class="flex items-center text-xs text-blue-600 gap-1">
                <span class="inline-flex w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse"></span>
                <span>Generating write-up… this can take a minute.</span>
           </div>`
        : '';

    return `
        <div class="flex flex-col gap-2">
            <div class="flex flex-wrap gap-2">
                ${viewBtn}
                ${saveBtn}
                ${actionBtn}
            </div>
            ${savingNotice}
        </div>
    `;
}

function formatTarget(session) {
    if (session.challenge_url) {
        const safeUrl = escapeHtml(session.challenge_url);
        return `<a href="${safeUrl}" target="_blank" rel="noopener" class="text-blue-600 hover:text-blue-800">${safeUrl}</a>`;
    }
    if (session.host && session.port) {
        return `${escapeHtml(session.host)}:${escapeHtml(String(session.port))}`;
    }
    return 'Handout only';
}

function formatTimestamp(value) {
    if (!value) return '—';
    try {
        const date = new Date(value);
        return date.toLocaleString();
    } catch {
        return value;
    }
}

function truncate(text, length) {
    if (!text || text.length <= length) return text || '';
    return `${text.slice(0, length)}…`;
}

function wireActionButtons() {
    document.querySelectorAll('.view-session').forEach((btn) => {
        btn.addEventListener('click', () => {
            const sessionId = btn.dataset.session;
            if (sessionId) {
                window.open(`/?session=${sessionId}`, '_blank', 'noopener');
            }
        });
    });

    document.querySelectorAll('.stop-session').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const sessionId = btn.dataset.session;
            if (!sessionId || btn.disabled) return;
            const confirmed = window.confirm('Stop this running session?');
            if (!confirmed) return;
            btn.disabled = true;
            try {
                const res = await fetch(`/challenges/${sessionId}/stop`, { method: 'POST' });
                if (!res.ok) {
                    throw new Error('Failed to stop session');
                }
                showTransientMessage('Session stopped');
            } catch (err) {
                console.error('Stop failed', err);
                alert(err.message || 'Failed to stop session');
            } finally {
                fetchSessions();
            }
        });
    });

    document.querySelectorAll('.delete-session').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const sessionId = btn.dataset.session;
            if (!sessionId || btn.disabled) return;
            const confirmed = window.confirm('Are you sure you want to delete this session history?');
            if (!confirmed) return;
            btn.disabled = true;
            try {
                const res = await fetch(`/challenges/${sessionId}`, { method: 'DELETE' });
                if (!res.ok) {
                    throw new Error('Failed to delete session');
                }
                showTransientMessage('Session deleted');
            } catch (err) {
                console.error('Delete failed', err);
                alert(err.message || 'Failed to delete session');
            } finally {
                fetchSessions();
            }
        });
    });

    document.querySelectorAll('.save-session').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const sessionId = btn.dataset.session;
            if (!sessionId || btn.disabled || btn.dataset.saving === 'true') return;

            const label = btn.dataset.challenge ? decodeURIComponent(btn.dataset.challenge) : sessionId;
            btn.disabled = true;
            btn.dataset.saving = 'true';
            const originalContent = btn.innerHTML;
            btn.innerHTML =
                '<svg class="w-3 h-3 mr-1 text-green-700 animate-spin" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a12 12 0 00-12 12h4z"></path></svg>Generating…';
            markWriteupActive(sessionId, label);

            try {
                const res = await fetch(`/challenges/${sessionId}/save`, { method: 'POST' });
                const data = await res.json();
                if (!res.ok) {
                    throw new Error(data.detail || 'Failed to save write-up');
                }
                showTransientMessage(`Saved to ${data.directory}`);
            } catch (err) {
                console.error('Save failed', err);
                alert(err.message || 'Failed to save write-up');
            } finally {
                clearWriteupActive(sessionId);
                btn.disabled = false;
                btn.innerHTML = originalContent;
                delete btn.dataset.saving;
                fetchSessions();
            }
        });
    });

    document.querySelectorAll('.copy-flag').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const encodedFlag = btn.dataset.flag;
            if (!encodedFlag) return;
            const flag = decodeURIComponent(encodedFlag);
            try {
                const copied = await copyTextToClipboard(flag);
                if (!copied) {
                    throw new Error('Clipboard unavailable');
                }
                showTransientMessage('Flag copied to clipboard');
            } catch (err) {
                console.error('Clipboard copy failed', err);
                alert('Unable to copy flag. Please copy manually.');
            }
        });
    });
}

function showTransientMessage(message) {
    if (!message) return;
    const container = document.createElement('div');
    container.textContent = message;
    container.className = 'fixed bottom-4 right-4 bg-gray-900 text-white text-sm px-4 py-2 rounded shadow-lg opacity-0 transition';
    document.body.appendChild(container);
    requestAnimationFrame(() => container.classList.remove('opacity-0'));
    setTimeout(() => {
        container.classList.add('opacity-0');
        container.addEventListener('transitionend', () => container.remove(), { once: true });
    }, 2000);
}

function escapeHtml(value) {
    if (value === undefined || value === null) return '';
    return String(value).replace(/[&<>"'`=\/]/g, (char) => {
        const map = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
            '`': '&#96;',
            '=': '&#61;',
            '/': '&#47;',
        };
        return map[char] || char;
    });
}

async function initFlagFormatSetting() {
    try {
        const input = document.getElementById('default-flag-format');
        const saveBtn = document.getElementById('flag-format-save-btn');
        const statusEl = document.getElementById('flag-format-status');
        const popover = document.getElementById('flag-format-popover');
        if (!input || !saveBtn) return;

        const showStatus = (text, isError = false) => {
            if (!statusEl) return;
            statusEl.textContent = text;
            statusEl.classList.toggle('text-green-600', !isError);
            statusEl.classList.toggle('text-red-600', isError);
            statusEl.classList.remove('hidden');
            setTimeout(() => statusEl.classList.add('hidden'), 2000);
        };

        const loadValue = async () => {
            try {
                const res = await fetch('/settings/flag-format');
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    throw new Error(data.detail || 'Failed to load flag format');
                }
                input.value = data.flag_format || FLAG_FORMAT_FALLBACK;
            } catch (err) {
                console.warn('Failed to load flag format', err);
                input.value = FLAG_FORMAT_FALLBACK;
                showStatus('Load failed', true);
            }
        };

        await loadValue();

        const persist = async () => {
            const value = input.value && input.value.trim() ? input.value.trim() : FLAG_FORMAT_FALLBACK;
            try {
                saveBtn.disabled = true;
                const res = await fetch('/settings/flag-format', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ flag_format: value })
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    throw new Error(data.detail || 'Failed to save flag format');
                }
                input.value = data.flag_format || value;
                showStatus('Saved!');
                if (popover && popover.hasAttribute('open')) {
                    popover.removeAttribute('open');
                }
            } catch (err) {
                console.error('Failed to save flag format', err);
                showStatus(err.message || 'Save failed', true);
            } finally {
                saveBtn.disabled = false;
            }
        };

        saveBtn.addEventListener('click', persist);
        input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                persist();
            }
        });
    } catch (err) {
        console.warn('Flag format setting disabled', err);
    }
}

async function copyTextToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
    }
    return fallbackCopyText(text);
}

function fallbackCopyText(text) {
    try {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'absolute';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.select();
        const success = document.execCommand('copy');
        document.body.removeChild(textarea);
        return success;
    } catch (err) {
        console.error('Fallback copy failed', err);
        return false;
    }
}

