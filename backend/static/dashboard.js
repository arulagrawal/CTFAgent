const DASHBOARD_REFRESH_MS = 5000;
let refreshTimer = null;

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
    console.log('fetchSessions', isManual);
    const errorEl = document.getElementById('dashboard-error');
    if (errorEl) errorEl.classList.add('hidden');

    try {
        const res = await fetch('/challenges');
        if (!res.ok) {
            throw new Error('Failed to load sessions');
        }
        const data = await res.json();
        renderSessions(data.sessions || []);
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

function renderSessions(sessions) {
    const body = document.getElementById('sessions-body');
    const emptyState = document.getElementById('empty-state');
    if (!body) return;

    body.innerHTML = '';
    if (!sessions.length) {
        if (emptyState) emptyState.classList.remove('hidden');
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
    const stopBtn = `<button class="stop-session inline-flex items-center px-3 py-1.5 border border-red-300 rounded-md text-xs font-medium text-red-600 hover:bg-red-50 disabled:opacity-40" data-session="${safeSessionId}" ${session.status === 'running' ? '' : 'disabled'}>Stop</button>`;

    return `
        <div class="flex flex-wrap gap-2">
            ${viewBtn}
            ${stopBtn}
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

