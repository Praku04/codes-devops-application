// Video Analysis chat — auto-search all indexed reports (no manual selection)

let videoAnalysisChatState = {
    sessionId: null,
    namespaceUserId: null,
    targetUserId: null,
    indexedReports: [],
    sessions: [],
    // Search term for the session list, so a long history can be narrowed instead of scrolled.
    sessionFilter: '',
};

let vaMarkdownConverter = null;

function getVaMarkdownConverter() {
    if (!vaMarkdownConverter && typeof showdown !== 'undefined') {
        vaMarkdownConverter = new showdown.Converter({
            tables: true,
            strikethrough: true,
            ghCodeBlocks: true,
            simplifiedAutoLink: true,
            openLinksInNewWindow: true,
            simpleLineBreaks: false,
            requireSpaceBeforeHeadingText: true,
        });
    }
    return vaMarkdownConverter;
}

function vaStripInventedImages(text) {
    if (!text) return '';
    let t = String(text);
    t = t.replace(/\[IMAGE:\s*user_id\/[^\]]+\]/gi, '');
    t = t.replace(/!\[[^\]]*\]\(\s*[^)]*user_id\/[^)]+\s*\)/gi, '');
    t = t.replace(/\[IMAGE_URL:\s*[^\]]*user_id\/[^\]]+\]/gi, '');
    return t;
}

function vaIsNoMatchResponse(text) {
    const t = String(text || '').toLowerCase();
    if (!t.trim()) return false;
    return (
        t.includes("couldn't find anything related") ||
        t.includes('could not find anything related') ||
        t.includes('nothing related was found') ||
        t.includes('indexed reports do not cover') ||
        t.includes('indexed reports do not contain') ||
        t.includes('do not contain any information related') ||
        t.includes('do not cover that topic') ||
        t.includes('cannot provide any relevant details') ||
        t.includes('no relevant information found') ||
        t.includes('not contain any information')
    );
}

function vaNoMatchDisplayText() {
    return "I couldn't find anything related to that in your indexed meeting reports. Please try asking about topics covered in those reports.";
}

function vaStripNoMatchExtras(text) {
    let t = String(text || '');
    if (!vaIsNoMatchResponse(t)) return t;
    return vaNoMatchDisplayText();
}

function vaStripAgentContent(text) {
    if (!text) return '';
    let t = String(text);
    t = t.replace(/<thinking>[\s\S]*?<\/thinking>/gi, '');
    t = t.replace(/<thinking>[\s\S]*$/gi, '');
    t = t.replace(/<\/thinking>/gi, '');
    const toolDone = /^[\s\S]*?(?:completed in \d+(?:\.\d+)?s\.)\s*/i;
    const m = t.match(toolDone);
    if (m) t = t.slice(m[0].length);
    t = t.replace(/^search_video_analysis_reports\([^)]*\)\s*/gim, '');
    t = t.replace(/^Found the following information from video analysis reports:\s*/i, '');
    t = vaStripNoMatchExtras(t);
    t = t.replace(/\n+(?:#{1,3}\s*)?\*{0,2}Sources\*{0,2}:?\s*\n[\s\S]*$/i, '');
    t = vaStripInventedImages(t);
    return t.trim();
}

function vaStripForDisplay(text) {
    let cleaned = vaStripAgentContent(text);
    if (typeof window.stripAgentThinking === 'function') {
        cleaned = window.stripAgentThinking(cleaned);
        cleaned = vaStripInventedImages(cleaned);
    }
    return cleaned;
}

function vaRenderStreamHtml(text, container) {
    const cleaned = vaStripForDisplay(text);
    if (typeof window.renderMarkdownWithImages === 'function') {
        return window.renderMarkdownWithImages(cleaned, container);
    }
    const conv = getVaMarkdownConverter();
    if (conv) return conv.makeHtml(cleaned);
    return escapeHtmlVa(cleaned).replace(/\n/g, '<br>');
}

function vaRenderChatHtml(text, container) {
    const cleaned = vaStripForDisplay(text);
    if (typeof window.renderMarkdownWithImages === 'function') {
        return window.renderMarkdownWithImages(cleaned, container);
    }
    const conv = getVaMarkdownConverter();
    if (conv) return conv.makeHtml(cleaned);
    return escapeHtmlVa(cleaned).replace(/\n/g, '<br>');
}

function vaScheduleChatImages(container) {
    if (!container) return;
    if (typeof window.scheduleMarkdownContainerImages === 'function') {
        window.scheduleMarkdownContainerImages(container);
    } else if (typeof window.loadAuthenticatedImages === 'function') {
        window.loadAuthenticatedImages(container);
    }
}

function vaCapChatImages(container, maxImages = 3) {
    if (!container) return;
    const imgs = Array.from(container.querySelectorAll('img'));
    imgs.forEach((img, index) => {
        if (index < maxImages) return;
        const block = img.closest('p, li, figure, .figure-marker') || img;
        if (block === img) {
            img.remove();
        } else if (block.querySelectorAll('img').length <= 1) {
            block.remove();
        } else {
            img.remove();
        }
    });
}

function vaForceLoadChatImages(container) {
    if (!container) return;
    vaCapChatImages(container, 3);
    vaScheduleChatImages(container);
    if (typeof window.loadAuthenticatedImages === 'function') {
        setTimeout(() => window.loadAuthenticatedImages(container), 250);
        setTimeout(() => window.loadAuthenticatedImages(container), 900);
    }
}

/**
 * True for an internal storage key: "va_<history uuid>" or a randomized "<32 hex>.<ext>" upload
 * name (VAPT #4). Neither means anything to the person reading the panel, so neither is shown.
 */
function isVaStorageKey(value) {
    const v = String(value || '').trim();
    if (/^va_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(v)) return true;
    return /^[0-9a-f]{32}(\.[A-Za-z0-9]+)?$/i.test(v);
}

function parseVaSourceLabel(raw) {
    const s = String(raw || '').trim();
    const m = s.match(/^(.+?)\s*\(([^)]+)\)\s*$/);
    if (m) {
        const doc = m[2].trim();
        // Sessions saved before the source label dropped its storage key still carry it.
        return { title: m[1].trim(), doc: isVaStorageKey(doc) ? null : doc };
    }
    return { title: s || 'Report', doc: null };
}

function initVideoAnalysisChatUI() {
    document.getElementById('va-tab-analyze')?.addEventListener('click', () => switchVideoAnalysisModuleTab('analyze'));
    document.getElementById('va-tab-chat')?.addEventListener('click', () => switchVideoAnalysisModuleTab('chat'));
    document.getElementById('va-new-chat-btn')?.addEventListener('click', startNewVideoAnalysisChat);
    document.getElementById('va-chat-input-form')?.addEventListener('submit', (e) => {
        e.preventDefault();
        handleVideoAnalysisChatSubmit();
    });
    document.getElementById('va-chat-input')?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleVideoAnalysisChatSubmit();
        }
    });
    // Filter the session list in place. Re-renders from loaded state - no API call per keystroke.
    document.getElementById('va-chat-session-search')?.addEventListener('input', (e) => {
        videoAnalysisChatState.sessionFilter = e.target.value || '';
        renderVideoAnalysisSessionRows();
    });
    document.getElementById('va-chat-sessions-list')?.addEventListener('click', (e) => {
        const delBtn = e.target.closest('[data-va-delete-session]');
        if (delBtn) {
            e.preventDefault();
            e.stopPropagation();
            const sessionId = delBtn.getAttribute('data-va-delete-session');
            if (sessionId) deleteVideoAnalysisChatSession(sessionId);
            return;
        }
        const selectBtn = e.target.closest('.va-session-select');
        const item = e.target.closest('.va-chat-session-item');
        if (selectBtn && item?.dataset?.sessionId) {
            loadVideoAnalysisChatSession(item.dataset.sessionId);
        }
    });
}

function navigateToVideoAnalysisView() {
    if (typeof window.switchView === 'function') {
        window.switchView('video-analysis');
        return;
    }
    document.querySelectorAll('.view').forEach((v) => v.classList.add('hidden'));
    document.getElementById('video-analysis-view')?.classList.remove('hidden');
    document.querySelectorAll('.nav-item').forEach((n) => n.classList.remove('active'));
    document.querySelector('.nav-item[data-view="video-analysis"]')?.classList.add('active');
    try {
        sessionStorage.setItem('lastActiveView', 'video-analysis');
    } catch (_) {
        /* ignore */
    }
}

function switchVideoAnalysisModuleTab(tab) {
    const isChat = tab === 'chat';
    const isAnalyze = tab === 'analyze';
    document.getElementById('video-analysis-workflow')?.classList.toggle('hidden', !isAnalyze);
    document.getElementById('video-analysis-chat-panel')?.classList.toggle('hidden', !isChat);
    const analyzeTab = document.getElementById('va-tab-analyze');
    const chatTab = document.getElementById('va-tab-chat');
    analyzeTab?.classList.toggle('active', isAnalyze);
    chatTab?.classList.toggle('active', isChat);
    analyzeTab?.setAttribute('aria-selected', isAnalyze ? 'true' : 'false');
    chatTab?.setAttribute('aria-selected', isChat ? 'true' : 'false');

    if (isChat) {
        if (!videoAnalysisChatState.targetUserId) {
            const hint = document.getElementById('va-chat-scope-hint');
            if (hint) hint.style.display = 'none';
        }
        loadVideoAnalysisIndexedReports(videoAnalysisChatState.targetUserId).then(() => {
            loadVideoAnalysisChatSessions(videoAnalysisChatState.targetUserId);
        });
    }
}

function onVideoAnalysisViewActivated(options = {}) {
    const openChat = Boolean(options.openChat);
    const keepTrainerScope = Boolean(options.keepTrainerScope);

    if (!keepTrainerScope) {
        videoAnalysisChatState.targetUserId = null;
        videoAnalysisChatState.namespaceUserId = null;
        const hint = document.getElementById('va-chat-scope-hint');
        if (hint) hint.style.display = 'none';
    } else if (options.trainerEmail) {
        const hint = document.getElementById('va-chat-scope-hint');
        if (hint) {
            hint.textContent = `Searching reports uploaded by ${String(options.trainerEmail).replace(/[\r\n\t]/g, ' ').slice(0, 120)}`;
            hint.style.display = 'block';
        }
    }

    startNewVideoAnalysisChat();

    if (typeof window.returnToVideoAnalysisMainScreen === 'function') {
        window.returnToVideoAnalysisMainScreen({ clearSession: false, notify: false });
    } else {
        switchVideoAnalysisModuleTab('analyze');
    }

    if (openChat) {
        switchVideoAnalysisModuleTab('chat');
    }

    loadVideoAnalysisIndexedReports(videoAnalysisChatState.targetUserId).then(() => {
        loadVideoAnalysisChatSessions(videoAnalysisChatState.targetUserId);
    });
}

function openVideoAnalysisChatForTrainer(userId, userEmail) {
    if (!userId) {
        if (typeof showNotification === 'function') {
            showNotification('Could not open chat: missing trainer user id.', 'error');
        }
        return;
    }
    videoAnalysisChatState.targetUserId = userId;
    videoAnalysisChatState.namespaceUserId = userId;
    if (typeof window.switchView === 'function') {
        window.switchView('video-analysis', {
            openChat: true,
            keepTrainerScope: true,
            trainerEmail: userEmail || 'this trainer',
        });
        return;
    }
    navigateToVideoAnalysisView();
    onVideoAnalysisViewActivated({
        openChat: true,
        keepTrainerScope: true,
        trainerEmail: userEmail || 'this trainer',
    });
}

function openVideoAnalysisChatTab() {
    if (typeof window.switchView === 'function') {
        window.switchView('video-analysis', { openChat: true });
        return;
    }
    navigateToVideoAnalysisView();
    onVideoAnalysisViewActivated({ openChat: true });
}

async function loadVideoAnalysisIndexedReports(targetUserId) {
    const listEl = document.getElementById('va-chat-documents-list');
    if (!listEl) return;

    listEl.innerHTML = '<p class="va-chat-loading"><i class="fas fa-spinner fa-spin"></i> Loading indexed reports...</p>';

    try {
        const params = targetUserId ? `?target_user_id=${encodeURIComponent(targetUserId)}` : '';
        const token = window.authToken || localStorage.getItem('authToken');
        const res = await fetch(`/video-analysis/embeddable-reports${params}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        if (!data.success) throw new Error('Failed to load reports');

        videoAnalysisChatState.indexedReports = data.data || [];
        videoAnalysisChatState.namespaceUserId = data.namespace_user_id || targetUserId || null;

        if (!videoAnalysisChatState.indexedReports.length) {
            const countEl = document.getElementById('va-indexed-count');
            if (countEl) countEl.textContent = '';
            const panelEl = document.getElementById('va-chat-documents-panel');
            if (panelEl) panelEl.open = true;
            listEl.innerHTML = [
                '<div class="va-chat-empty-state">',
                '<i class="fas fa-folder-open"></i>',
                '<p><strong>No indexed reports yet.</strong></p>',
                '<p>Open <button type="button" class="btn btn-sm btn-primary" onclick="switchVideoAnalysisModuleTab(\'analyze\'); openReportsModal();">View All Reports</button>',
                ' and use <em>Index for Chat</em> on each export.</p>',
                '</div>',
            ].join('');
            updateVideoChatInputState();
            return;
        }

        const countEl = document.getElementById('va-indexed-count');
        if (countEl) {
            countEl.textContent = `(${videoAnalysisChatState.indexedReports.length})`;
        }
        const panelEl = document.getElementById('va-chat-documents-panel');
        // Keep chat readable when many reports are indexed — expand on demand.
        if (panelEl) {
            panelEl.open = videoAnalysisChatState.indexedReports.length <= 3;
        }

        listEl.innerHTML = videoAnalysisChatState.indexedReports.map((r) => {
            // escapeHtmlVa already turns " into &quot;, so its output is safe in an attribute
            // as-is — the tooltip carries the full title the chip itself may wrap.
            const title = escapeHtmlVa(r.pdf_title || 'Meeting Documentation');
            const displayFile = typeof window.vaDisplayVideoFilename === 'function'
                ? window.vaDisplayVideoFilename(r.video_filename, r.pdf_title)
                : (r.video_filename || '');
            const fileText = escapeHtmlVa(displayFile);
            const file = displayFile
                ? `<span class="va-chip-file" title="${fileText}">${fileText}</span>`
                : '';
            return [
                '<div class="va-chat-doc-chip">',
                '<i class="fas fa-file-alt"></i>',
                `<span class="va-chip-title" title="${title}">${title}</span>`,
                file,
                '<span class="va-embed-badge indexed">Indexed</span>',
                '</div>',
            ].join('');
        }).join('');

        if (!videoAnalysisChatState.sessionId) {
            startNewVideoAnalysisChat();
        }
        updateVideoChatInputState();
    } catch (err) {
        console.error('[VA CHAT] load indexed:', err);
        listEl.innerHTML = '<p class="va-chat-empty">Error loading indexed reports.</p>';
    }
}

function updateVideoChatInputState() {
    const input = document.getElementById('va-chat-input');
    const sendBtn = document.getElementById('va-chat-send-btn');
    const enabled = videoAnalysisChatState.indexedReports.length > 0;
    if (input) input.disabled = !enabled;
    if (sendBtn) sendBtn.disabled = !enabled;
}

async function indexVideoReportForChat(historyId, isReindex = false) {
    try {
        showNotification(
            isReindex ? 'Reindexing report for chat (embeddings + frames)...' : 'Generating embeddings for chat...',
            'info',
        );
        const token = window.authToken || localStorage.getItem('authToken');
        const res = await fetch(`/video-analysis/index/${historyId}`, {
            method: 'POST',
            headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Indexing failed');
        const warning = data.data?.warning;
        const synced = data.data?.frame_cache_synced;
        let msg = isReindex
            ? 'Report reindexed — chat search and images refreshed.'
            : 'Report indexed — you can chat with it now.';
        if (typeof synced === 'number' && synced > 0) {
            msg += ` (${synced} frame(s) synced.)`;
        }
        showNotification(warning || msg, warning ? 'warning' : 'success');
        if (typeof loadVideoAnalysisHistory === 'function') loadVideoAnalysisHistory();
        const chatPanel = document.getElementById('video-analysis-chat-panel');
        if (chatPanel && !chatPanel.classList.contains('hidden')) {
            await loadVideoAnalysisIndexedReports(videoAnalysisChatState.targetUserId);
        }
    } catch (err) {
        showNotification(`Indexing error: ${err.message}`, 'error');
    }
}

function startNewVideoAnalysisChat() {
    videoAnalysisChatState.sessionId = `va-chat-${crypto.randomUUID()}`;
    const win = document.getElementById('va-chat-window');
    if (win) {
        win.innerHTML = [
            '<div class="chat-placeholder">',
            '<i class="fas fa-comments"></i>',
            '<h2>New conversation</h2>',
            '<p>Ask a question about your indexed meeting reports.</p>',
            '</div>',
        ].join('');
    }
    document.querySelectorAll('.va-chat-session-item').forEach((el) => el.classList.remove('active'));
    updateVideoChatInputState();
}

async function loadVideoAnalysisChatSessions(targetUserId) {
    const list = document.getElementById('va-chat-sessions-list');
    if (!list) return;
    try {
        const params = targetUserId ? `?target_user_id=${encodeURIComponent(targetUserId)}` : '';
        const token = window.authToken || localStorage.getItem('authToken');
        const res = await fetch(`/video-analysis/chat/sessions${params}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        videoAnalysisChatState.sessions = data.data || [];
        if (!videoAnalysisChatState.sessions.length) {
            list.innerHTML = '<p class="va-chat-empty">No past conversations</p>';
            if (!videoAnalysisChatState.sessionId) startNewVideoAnalysisChat();
            return;
        }
        renderVideoAnalysisSessionRows();
    } catch (err) {
        console.error('[VA CHAT] sessions:', err);
        list.innerHTML = '<p class="va-chat-empty">Could not load sessions</p>';
    }
}

/**
 * Paint the session rows, honouring the search box.
 *
 * Split out of the fetch so filtering re-renders from already-loaded state instead of
 * re-hitting the API on every keystroke. Rows are built with createElement/textContent, so
 * session names (user-supplied) can never become markup (CodeQL/Sonar XSS).
 */
function renderVideoAnalysisSessionRows() {
    const list = document.getElementById('va-chat-sessions-list');
    if (!list) return;
    const term = (videoAnalysisChatState.sessionFilter || '').trim().toLowerCase();
    const all = videoAnalysisChatState.sessions || [];
    const sessions = term
        ? all.filter((s) => (s.session_name || 'Chat').toLowerCase().includes(term))
        : all;

    const countEl = document.getElementById('va-chat-session-count');
    if (countEl) {
        countEl.textContent = term
            ? `${sessions.length} of ${all.length}`
            : `${all.length} conversation${all.length === 1 ? '' : 's'}`;
    }

    list.innerHTML = '';
    if (!sessions.length) {
        const empty = document.createElement('p');
        empty.className = 'va-chat-empty';
        empty.textContent = term ? 'No conversations match that search' : 'No past conversations';
        list.appendChild(empty);
        return;
    }

    let activeRow = null;
    sessions.forEach((s) => {
            const row = document.createElement('div');
            const isActive = s.id === videoAnalysisChatState.sessionId;
            row.className = 'va-chat-session-item' + (isActive ? ' active' : '');
            if (isActive) activeRow = row;
            row.dataset.sessionId = s.id;

            const selectBtn = document.createElement('button');
            selectBtn.type = 'button';
            selectBtn.className = 'va-session-select';
            selectBtn.title = 'Open conversation';
            const nameSpan = document.createElement('span');
            nameSpan.className = 'va-session-name';
            nameSpan.textContent = s.session_name || 'Chat';
            const dateSmall = document.createElement('small');
            // The fallback pins the zone too: script.js normally supplies formatDateIST, but a bare
            // toLocaleDateString() on the API's zone-less UTC string renders the machine's own zone.
            dateSmall.textContent = typeof formatDateIST === 'function'
                ? formatDateIST(s.created_at)
                : new Date(`${s.created_at}Z`).toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata' });
            selectBtn.appendChild(nameSpan);
            selectBtn.appendChild(dateSmall);

            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'va-session-delete';
            delBtn.title = 'Delete conversation';
            delBtn.setAttribute('data-va-delete-session', s.id);
            delBtn.innerHTML = '<i class="fas fa-trash-alt"></i>';

            row.appendChild(selectBtn);
            row.appendChild(delBtn);
            list.appendChild(row);
    });

    // Bring the open conversation into view so you are not left scrolling a long list to find
    // where you are. Only scrolls the list itself, never the page.
    if (activeRow && typeof activeRow.scrollIntoView === 'function') {
        activeRow.scrollIntoView({ block: 'nearest' });
    }
}

async function deleteVideoAnalysisChatSession(sessionId) {
    if (!sessionId) return;
    if (!confirm('Delete this conversation?')) return;
    try {
        const token = window.authToken || localStorage.getItem('authToken');
        // POST, not DELETE (VAPT: HTTP DELETE Method Enabled) — raw fetch, so the
        // /delete suffix is spelled out rather than mapped by apiCall.
        const res = await fetch(`/video-analysis/chat/sessions/${encodeURIComponent(sessionId)}/delete`, {
            method: 'POST',
            headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        if (!res.ok || !data.success) throw new Error('Delete failed');
        if (videoAnalysisChatState.sessionId === sessionId) {
            videoAnalysisChatState.sessionId = null;
            startNewVideoAnalysisChat();
        }
        await loadVideoAnalysisChatSessions(videoAnalysisChatState.targetUserId);
        showNotification('Conversation deleted.', 'success');
    } catch (err) {
        showNotification(err.message || 'Could not delete conversation', 'error');
    }
}

async function loadVideoAnalysisChatSession(sessionId) {
    videoAnalysisChatState.sessionId = sessionId;
    try {
        const token = window.authToken || localStorage.getItem('authToken');
        const res = await fetch(`/video-analysis/chat/sessions/${sessionId}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        if (!data.success) throw new Error('Failed to load session');
        const session = data.data;
        if (session.kb_namespace_user_id) {
            videoAnalysisChatState.namespaceUserId = session.kb_namespace_user_id;
        }
        renderVideoAnalysisChatMessages(session.messages || []);
        await loadVideoAnalysisIndexedReports(videoAnalysisChatState.targetUserId);
        loadVideoAnalysisChatSessions(videoAnalysisChatState.targetUserId);
        updateVideoChatInputState();
    } catch (err) {
        showNotification(err.message, 'error');
    }
}

function buildVaSourcesBlock(sources) {
    if (!sources?.length) return null;
    const details = document.createElement('details');
    details.className = 'context-details va-context-details';
    const summary = document.createElement('summary');
    summary.innerHTML = `<i class="fas fa-bookmark"></i> Sources & metadata (${sources.length})`;
    details.appendChild(summary);
    sources.forEach((s, i) => {
        const item = document.createElement('div');
        item.className = 'context-item';
        const meta = parseVaSourceLabel(s.source);
        const title = document.createElement('strong');
        title.textContent = `Source ${i + 1}: ${meta.title || 'Report'}`;
        item.appendChild(title);
        if (meta.doc) {
            const doc = document.createElement('span');
            doc.className = 'va-source-doc';
            doc.textContent = meta.doc;
            item.appendChild(doc);
        }
        if (s.content) {
            const p = document.createElement('p');
            const text = String(s.content);
            p.textContent = text.length > 400 ? `${text.slice(0, 400)}…` : text;
            item.appendChild(p);
        }
        details.appendChild(item);
    });
    return details;
}

function renderVideoAnalysisChatMessages(messages) {
    const win = document.getElementById('va-chat-window');
    if (!win) return;
    win.innerHTML = '';
    if (!messages.length) {
        win.innerHTML = '<div class="chat-placeholder"><p>No messages yet.</p></div>';
        return;
    }
    messages.forEach((m) => {
        const role = m.role === 'ai' ? 'assistant' : 'user';
        const wrap = document.createElement('div');
        wrap.className = `va-chat-message va-chat-${role}`;
        const bubble = document.createElement('div');
        bubble.className = 'va-chat-bubble' + (role === 'assistant' ? ' va-markdown-body' : '');
        if (role === 'user') {
            bubble.textContent = m.content || '';
        } else {
            bubble.innerHTML = vaRenderChatHtml(m.content || '', bubble);
            vaForceLoadChatImages(bubble);
        }
        wrap.appendChild(bubble);
        if (role === 'assistant' && m.sources?.length && !vaIsNoMatchResponse(m.content)) {
            const src = buildVaSourcesBlock(m.sources);
            if (src) wrap.appendChild(src);
        }
        win.appendChild(wrap);
    });
    win.scrollTop = win.scrollHeight;
}

async function refreshLastAssistantMessageContent(bubble) {
    if (!videoAnalysisChatState.sessionId || !bubble) return;
    try {
        const token = window.authToken || localStorage.getItem('authToken');
        const res = await fetch(`/video-analysis/chat/sessions/${videoAnalysisChatState.sessionId}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        if (!data.success) return;
        const lastAi = [...(data.data.messages || [])].reverse().find((m) => m.role === 'ai');
        if (lastAi?.content) {
            bubble.innerHTML = vaRenderChatHtml(lastAi.content, bubble);
            vaForceLoadChatImages(bubble);
        }
    } catch (e) {
        console.warn('[VA CHAT] refresh message:', e);
    }
}

async function handleVideoAnalysisChatSubmit() {
    const input = document.getElementById('va-chat-input');
    const sendBtn = document.getElementById('va-chat-send-btn');
    const query = input?.value?.trim();
    if (!query) return;

    if (!videoAnalysisChatState.indexedReports.length) {
        showNotification('Index reports in View All Reports before chatting.', 'warning');
        return;
    }

    if (!videoAnalysisChatState.sessionId) {
        startNewVideoAnalysisChat();
    }

    appendVideoChatMessage('user', query);
    input.value = '';
    input.disabled = true;
    if (sendBtn) sendBtn.disabled = true;

    const ai = appendVideoChatMessage('assistant', '');
    const aiContent = ai.contentEl;
    ai.wrap.classList.add('va-streaming');

    try {
        const token = window.authToken || localStorage.getItem('authToken');
        const body = { query, session_id: videoAnalysisChatState.sessionId };
        if (videoAnalysisChatState.targetUserId) {
            body.target_user_id = videoAnalysisChatState.targetUserId;
        }

        const res = await fetch('/video-analysis/chat/query', {
            method: 'POST',
            headers: {
                Authorization: `Bearer ${token}`,
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || `Chat failed (${res.status})`);
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let aiText = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            aiText += decoder.decode(value, { stream: true });
            aiContent.innerHTML = vaRenderStreamHtml(aiText, aiContent) + '<span class="va-typing-cursor"></span>';
            vaScheduleChatImages(aiContent);
            const win = document.getElementById('va-chat-window');
            if (win) win.scrollTop = win.scrollHeight;
        }
        ai.wrap.classList.remove('va-streaming');
        await refreshLastAssistantMessageContent(aiContent);
        vaForceLoadChatImages(aiContent);
        await refreshLastAssistantSources(ai.wrap);
        loadVideoAnalysisChatSessions(videoAnalysisChatState.targetUserId);
    } catch (err) {
        ai.wrap.classList.remove('va-streaming');
        aiContent.innerHTML = `<p class="va-chat-error">${escapeHtmlVa(err.message)}</p>`;
        showNotification(err.message, 'error');
    } finally {
        updateVideoChatInputState();
    }
}

async function refreshLastAssistantSources(messageWrap) {
    if (!videoAnalysisChatState.sessionId || !messageWrap) return;
    try {
        const token = window.authToken || localStorage.getItem('authToken');
        const res = await fetch(`/video-analysis/chat/sessions/${videoAnalysisChatState.sessionId}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        const data = await res.json();
        if (!data.success) return;
        const msgs = data.data.messages || [];
        const lastAi = [...msgs].reverse().find((m) => m.role === 'ai');
        messageWrap.querySelector('.va-context-details, .va-message-sources')?.remove();
        if (lastAi?.sources?.length && !vaIsNoMatchResponse(lastAi.content)) {
            const src = buildVaSourcesBlock(lastAi.sources);
            if (src) messageWrap.appendChild(src);
        }
    } catch (e) {
        console.warn('[VA CHAT] sources refresh:', e);
    }
}

function appendVideoChatMessage(role, text) {
    const win = document.getElementById('va-chat-window');
    if (!win) return null;
    win.querySelector('.chat-placeholder')?.remove();

    const wrap = document.createElement('div');
    wrap.className = `va-chat-message va-chat-${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'va-chat-bubble' + (role === 'assistant' ? ' va-markdown-body' : '');

    if (role === 'user') {
        bubble.textContent = text;
    } else if (text) {
        bubble.innerHTML = vaRenderChatHtml(text, bubble);
        vaScheduleChatImages(bubble);
    }

    wrap.appendChild(bubble);
    win.appendChild(wrap);
    win.scrollTop = win.scrollHeight;
    return { wrap, contentEl: bubble };
}

function escapeHtmlVa(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

window.indexVideoReportForChat = indexVideoReportForChat;
window.loadVideoAnalysisIndexedReports = loadVideoAnalysisIndexedReports;
window.openVideoAnalysisChatTab = openVideoAnalysisChatTab;
window.openVideoAnalysisChatForTrainer = openVideoAnalysisChatForTrainer;
window.onVideoAnalysisViewActivated = onVideoAnalysisViewActivated;
window.switchVideoAnalysisModuleTab = switchVideoAnalysisModuleTab;
window.loadVideoAnalysisChatSession = loadVideoAnalysisChatSession;
window.deleteVideoAnalysisChatSession = deleteVideoAnalysisChatSession;
