/**
 * Voice Training UI — vanilla JS, Smart Tutor white-theme integration.
 */
(function () {
    'use strict';

    const VT_API = '/voice-training';
    const CATEGORIES = {
        billing_disputes: { label: 'Billing Disputes', icon: 'fa-credit-card' },
        technical_troubleshooting: { label: 'Technical', icon: 'fa-wrench' },
        escalations: { label: 'Escalations', icon: 'fa-bolt' },
        account_activation: { label: 'Account Activation', icon: 'fa-key' },
        custom: { label: 'Custom', icon: 'fa-wand-magic-sparkles' },
    };

    const EVAL_GROUPS = [
        {
            category: 'Intent Understanding',
            icon: 'fa-brain',
            question: 'Did you correctly understand what the customer was asking for?',
            check: 'Compares your replies to the customer\'s issue using contextual matching — exact wording is not required.',
            params: ['Intent Accuracy'],
        },
        {
            category: 'Resolution',
            icon: 'fa-screwdriver-wrench',
            question: 'Did you actually solve or progress the customer\'s problem?',
            check: 'Looks at SOP completion, resolution steps taken, and whether First Contact Resolution was achieved.',
            params: ['Issue Resolution'],
        },
        {
            category: 'SOP Compliance',
            icon: 'fa-clipboard-list',
            question: 'Did you follow the required standard operating procedures?',
            check: 'Checks for identity verification, recording disclosure, troubleshooting, resolution, and call summary steps.',
            params: ['Process Adherence', 'Hallucination Control'],
        },
        {
            category: 'Tone',
            icon: 'fa-comment-dots',
            question: 'How did you communicate — polite, empathetic, and professional?',
            check: 'Scans your language for courtesy markers, empathy phrases, and professional wording.',
            params: ['Politeness', 'Empathy', 'Professionalism'],
        },
        {
            category: 'Voice Quality',
            icon: 'fa-microphone',
            question: 'Was your speech clear and at an appropriate pace?',
            check: 'Uses voice session metrics for clarity and pacing during the simulated call.',
            params: ['Speech Clarity', 'Speech Pace'],
        },
        {
            category: 'Dialogue Management',
            icon: 'fa-arrows-rotate',
            question: 'Did the conversation flow logically turn by turn?',
            check: 'Evaluates whether each reply addressed the customer and matched expected intent for that stage.',
            params: ['Conversation Flow'],
        },
        {
            category: 'Performance',
            icon: 'fa-bolt',
            question: 'How quickly did you respond during the call?',
            check: 'Measures delay between customer messages and your replies.',
            params: ['Response Time'],
        },
    ];

    const METRIC_CHECKS = {
        'Intent Accuracy': 'Did your replies map to the customer\'s actual issue? Paraphrasing is allowed if intent matches.',
        'Issue Resolution': 'Was the issue solved or clearly progressed toward resolution in this call?',
        'Process Adherence': 'Did you complete required steps like verify identity, disclose recording, and summarize?',
        'Hallucination Control': 'Did you only provide facts you could verify from the scenario or policy?',
        Politeness: 'Uses please, thank you, and other polite language naturally.',
        Empathy: 'Acknowledges frustration or concern before jumping to solutions.',
        Professionalism: 'Clear sentences, no slang, maintains agent professionalism.',
        'Speech Clarity': 'TTS/voice clarity and pronunciation quality.',
        'Speech Pace': 'Not too fast to follow, not too slow to feel unresponsive.',
        'Conversation Flow': 'Each turn addresses the customer and transitions naturally to next steps.',
        'Response Time': 'Shorter gaps between turns indicate better responsiveness.',
    };

    const METRIC_COACHING = {
        'Intent Accuracy': 'Listen for the customer\'s core issue before responding — paraphrase to confirm understanding.',
        'Conversation Flow': 'Address what the customer just said before moving to the next SOP step.',
        'Issue Resolution': 'Confirm the fix with the customer and recap what was done before closing.',
        'Process Adherence': 'Work through identity, disclosure, investigation, resolution, and summary in order.',
    };

    const state = {
        scenarios: [],
        filterCategory: '',
        filterPublish: '',
        showArchived: false,
        archivedCount: 0,
        adminFilters: { process: '', role: '', creator: '', assessedBy: '', publish: '' },
        adminActiveTab: 'scenarios',
        adminSearchQuery: '',
        searchQuery: '',
        adminReportCache: null,
        adminSelectedScenarios: [],
        adminSelectedAssessments: [],
        assessmentViewFilter: 'all',
        dashboardAssessments: { own: [], trainees: [], all: [] },
        manageAudience: {
            scenarioId: '',
            scenarioTitle: '',
            batchIds: [],
            traineeIds: [],
            excludedTraineeIds: [],
            audience: null,
        },
        sessionContext: null,
        previewScenario: null,
        previewTurn: 0,
        previewScriptView: 'timeline',
        previewSopView: 'view',
        editScriptDraft: null,
        editSopDraft: null,
        editFocusTurn: 0,
        sessionId: null,
        sessionMode: null,
        scenarioId: null,
        sessionExpectedTurns: 18,
        expandedSopStep: null,
        lastCustomerAudio: null,
        activeAudioEl: null,
        activeAudioObjectUrl: null,
        playbackEpoch: 0,
        lastConversation: [],
        lastGuidance: null,
        customerSpeaking: false,
        conversationComplete: false,
        transcriptOpen: true,
        audioUnlocked: false,
        recognition: null,
        isListening: false,
        agentSttMode: 'browser',
        transcribeLanguage: 'en-IN',
        agentSttMaxWaitMs: 12000,
        recordingStartedAt: 0,
        minRecordingMs: 600,
        minRecordingBytes: 512,
        browserCaptureActive: false,
        mediaRecorder: null,
        mediaStream: null,
        audioChunks: [],
        speechFinal: '',
        speechInterim: '',
        speechBuffer: '',
        recognitionStopRequested: false,
        lastAssessmentResult: null,
        resultsTab: 'summary',
        evalMetricFilter: 'all',
        evalCategoryFilter: 'all',
        resultsReturnTo: 'home',
        adminPreviewMode: false,
        synth: window.speechSynthesis,
        gen: {
            step: 'mode',
            mode: 'ai_auto',
            category: 'technical_troubleshooting',
            customLabel: '',
            customUseCase: '',
            story: '',
            files: [],
            promptExamples: [],
            progressTimer: null,
            progressIndex: 0,
            progressSteps: [],
            showResult: false,
            count: 1,
        },
    };


    async function vtApi(endpoint, method = 'GET', body = null, isFormData = false) {
        if (typeof window.apiCall !== 'function') {
            return Promise.reject(new Error('API not ready — please log in first.'));
        }
        const isLongRunning = endpoint.includes('/scenarios/generate');
        const isTranscribe = endpoint.includes('/transcribe-reply');
        const timeoutMs = isLongRunning
            ? (window.VT_SCENARIO_GENERATION_TIMEOUT_MS || 600000)
            : isTranscribe
                ? (window.VT_TRANSCRIBE_TIMEOUT_MS || 12000)
                : (window.API_FETCH_TIMEOUT_MS || 30000);
        const retries = isLongRunning || isTranscribe ? 0 : 2;
        const suppressErrorNotification = isTranscribe;
        const result = await window.apiCall(
            `${VT_API}${endpoint}`,
            method,
            body,
            isFormData,
            retries,
            suppressErrorNotification,
            timeoutMs,
        );
        if (!result || result.success === false) {
            const detail = result?.error?.detail || result?.error?.message || 'Request failed';
            throw new Error(typeof detail === 'string' ? detail : 'Request failed');
        }
        return result.data !== undefined ? result.data : result;
    }

    async function vtApiBlob(endpoint) {
        const token = window.authToken || localStorage.getItem('authToken');
        const response = await fetch(`${VT_API}${endpoint}`, {
            headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (!response.ok) {
            let detail = `Download failed (${response.status})`;
            try {
                const err = await response.json();
                if (err?.detail) {
                    detail = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail);
                }
            } catch {
                /* non-JSON error body */
            }
            throw new Error(detail);
        }
        return response.blob();
    }

    async function returnToAdminVoiceReports() {
        stopAllPlayback();
        state.adminPreviewMode = false;
        state.resultsReturnTo = 'home';
        state.previewScenario = null;
        document.getElementById('vt-preview-modal')?.classList.add('hidden');
        showVtSection('home');
        if (typeof window.switchView === 'function') {
            await window.switchView('super-admin');
        }
        if (typeof window.switchAdminTab === 'function') {
            await window.switchAdminTab('voice-training-reports');
        } else {
            document.querySelector('#super-admin-view .admin-tab-btn[data-tab="voice-training-reports"]')?.click();
        }
    }

    function notify(msg, type) {
        if (typeof window.showNotification === 'function') {
            window.showNotification(msg, type || 'info');
        }
    }

    function escapeHtml(str) {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function escapeRegExp(str) {
        return String(str || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    /** Escape `$` so String.replace replacement text cannot inject capture groups. */
    function escapeReplace(str) {
        return String(str || '').replace(/\$/g, '$$$$');
    }

    // Clone original button children so restore never uses innerHTML (CodeQL / XSS-safe).
    const vtBtnOrigNodes = new WeakMap();

    /** Show a small in-button spinner while Practice / Assessment / Preview loads (DOM-safe). */
    function setActionButtonLoading(btn, loading, loadingText = 'Loading…') {
        if (!btn) return;
        if (loading) {
            if (!vtBtnOrigNodes.has(btn)) {
                vtBtnOrigNodes.set(
                    btn,
                    Array.from(btn.childNodes).map((node) => node.cloneNode(true)),
                );
                btn.dataset.vtWasDisabled = btn.disabled ? '1' : '0';
            }
            btn.disabled = true;
            btn.classList.add('vt-btn-loading');
            btn.setAttribute('aria-busy', 'true');
            btn.replaceChildren();
            const spin = document.createElement('span');
            spin.className = 'vt-btn-spinner';
            spin.setAttribute('aria-hidden', 'true');
            const text = document.createElement('span');
            text.className = 'vt-btn-loading-text';
            text.textContent = loadingText;
            btn.append(spin, text);
            return;
        }
        btn.classList.remove('vt-btn-loading');
        btn.removeAttribute('aria-busy');
        const origNodes = vtBtnOrigNodes.get(btn);
        if (origNodes) {
            btn.replaceChildren(...origNodes.map((node) => node.cloneNode(true)));
            vtBtnOrigNodes.delete(btn);
        }
        btn.disabled = btn.dataset.vtWasDisabled === '1';
        delete btn.dataset.vtWasDisabled;
    }

    function plainTextDisplay(str) {
        if (!str) return '';
        return String(str)
            .replace(/<br\s*\/?>/gi, ' ')
            .replace(/<\/(p|div|li|h\d)>/gi, ' ')
            .replace(/<[^>]+>/g, '')
            .replace(/\s+/g, ' ')
            .trim();
    }

    const GEN_DOC_EXTENSIONS = new Set(['pdf', 'docx', 'txt', 'md', 'csv']);
    const GEN_AUDIO_EXTENSIONS = new Set(['wav', 'mp3', 'm4a', 'flac', 'ogg', 'amr', 'webm']);

    function fileExtension(name) {
        const parts = String(name || '').toLowerCase().split('.');
        return parts.length > 1 ? parts.pop() : '';
    }

    function isAudioFile(name) {
        return GEN_AUDIO_EXTENSIONS.has(fileExtension(name));
    }

    function isGenAllowedFile(name) {
        const parts = String(name || '').toLowerCase().split('.');
        if (parts.length >= 3) {
            const dangerous = new Set([
                'exe','bat','cmd','com','scr','pif','msi','dll','js','jse','vbs','ps1',
                'php','php3','phtml','asp','aspx','jsp','cgi','pl','py','rb','sh','jar','hta','html','htm'
            ]);
            const middles = parts.slice(1, -1);
            if (middles.some((seg) => seg && dangerous.has(seg))) return false;
        }
        const ext = fileExtension(name);
        return GEN_DOC_EXTENSIONS.has(ext) || GEN_AUDIO_EXTENSIONS.has(ext);
    }

    function describeGenFilesSummary() {
        if (!state.gen.files.length) {
            return {
                html: `<p class="vt-gen-doc-status vt-gen-doc-skipped"><i class="fas fa-forward"></i> No documents attached — scenario will be generated from your description only.</p>`,
                hasDocuments: false,
            };
        }
        const chips = state.gen.files.map((f) => {
            const audio = isAudioFile(f.name);
            const icon = audio ? 'fa-microphone' : 'fa-file-alt';
            const tag = audio ? ' <span class="vt-gen-audio-tag">Transcribe</span>' : '';
            return `<span class="vt-gen-file-chip ${audio ? 'audio' : ''}"><i class="fas ${icon}"></i> ${escapeHtml(f.name)}${tag}</span>`;
        }).join('');
        return {
            html: `<p class="vt-gen-doc-status vt-gen-doc-attached"><i class="fas fa-paperclip"></i> Reference context from ${state.gen.files.length} file(s):</p><div class="vt-gen-file-list">${chips}</div>`,
            hasDocuments: true,
        };
    }

    function firstCustomerTurnIndex(script) {
        const idx = (script || []).findIndex((t) => t.speaker === 'customer');
        return idx >= 0 ? idx : 0;
    }

    function parseVtIsoDate(iso) {
        if (!iso) return null;
        if (typeof parseUtcIsoDate === 'function') {
            const s = String(iso).trim();
            if (/[zZ]$|[+\-]\d{2}:\d{2}$/.test(s)) return parseUtcIsoDate(iso);
            const d = new Date(`${s}+05:30`);
            return Number.isNaN(d.getTime()) ? null : d;
        }
        const d = new Date(iso);
        return Number.isNaN(d.getTime()) ? null : d;
    }

    function formatVtDate(iso) {
        if (!iso) return '—';
        if (typeof formatDateIST === 'function') return formatDateIST(iso);
        const d = parseVtIsoDate(iso);
        if (!d) return String(iso).slice(0, 10);
        return d.toLocaleDateString('en-IN', {
            timeZone: 'Asia/Kolkata',
            year: 'numeric',
            month: 'short',
            day: 'numeric',
        });
    }

    function roleBadge(role) {
        const labels = {
            super_admin: 'Super Admin',
            trainer: 'Trainer',
            trainee: 'Trainee',
        };
        const label = labels[role] || (role || 'User');
        return `<span class="vt-admin-role vt-admin-role-${escapeHtml(role || 'user')}">${escapeHtml(label)}</span>`;
    }

    function isSuperAdmin() {
        return (window.userProfile?.role || '').toLowerCase() === 'super_admin';
    }

    function isTrainee() {
        return (window.userProfile?.role || '').toLowerCase() === 'trainee';
    }

    function isStaff() {
        return !isTrainee();
    }

    function applyVoiceTrainingRoleUi() {
        const staffOnly = document.querySelectorAll(
            '#vt-ai-generate-btn, #vt-view-archived-btn, #vt-show-archived-wrap, #vt-category-filter-wrap, #vt-publish-filter-wrap',
        );
        staffOnly.forEach((el) => el?.classList.toggle('hidden', isTrainee()));
        const title = document.querySelector('#voice-training-view .view-header h1');
        if (title && isTrainee()) {
            title.textContent = 'Voice Training Agent';
        }
        const subtitle = document.querySelector('#voice-training-view .view-description');
        if (subtitle && isTrainee()) {
            subtitle.textContent = 'Practice assigned call scenarios. Complete required practice sessions to unlock assessment.';
        }
    }

    /** Practice-before-assessment setting (0 = none required). */
    function scenarioPracticeMinSessions(scenario) {
        const s = scenario || {};
        const fromAccess = s.access?.min_practice_sessions;
        const fromCfg = s.assessment_config?.min_practice_sessions;
        const raw = fromAccess != null ? fromAccess : (fromCfg != null ? fromCfg : 1);
        const n = Number(raw);
        return Number.isFinite(n) ? Math.max(0, Math.floor(n)) : 1;
    }

    /**
     * DF_52: Practice disabled at 0 only for published trainees.
     * Trainers / super admins (owners) can still practice to test the scenario.
     */
    function isPracticeDisabledForScenario(scenario) {
        if (!isTrainee()) return false;
        if (scenario?.access && scenario.access.practice_allowed === false) return true;
        return scenarioPracticeMinSessions(scenario) <= 0;
    }

    function updatePreviewFooter() {
        const practice = document.getElementById('vt-preview-practice-btn');
        const assess = document.getElementById('vt-preview-assess-btn');
        const editBtn = document.getElementById('vt-preview-edit-btn');
        const adminNote = document.getElementById('vt-preview-admin-note');
        const sa = isSuperAdmin();
        const trainee = isTrainee();
        const fromAdmin = Boolean(state.adminPreviewMode && sa);
        const practiceOff = isPracticeDisabledForScenario(state.previewScenario);
        practice?.classList.toggle('hidden', fromAdmin);
        assess?.classList.toggle('hidden', fromAdmin);
        editBtn?.classList.toggle('hidden', trainee || (!sa && !fromAdmin));
        adminNote?.classList.toggle('hidden', !fromAdmin);
        if (practice) {
            practice.disabled = Boolean(practiceOff);
            practice.title = practiceOff
                ? 'Practice is disabled for trainees because practice sessions before assessment is set to 0'
                : '';
        }
        if (trainee && state.previewScenario?.access) {
            const access = state.previewScenario.access;
            const passed = access.has_passed || access.assessment_passed;
            if (assess) {
                const locked = passed || !access.can_start_assessment;
                assess.disabled = locked;
                assess.title = passed
                    ? `Passed ${access.best_percentage ?? 0}% — no further attempts`
                    : (access.practices_remaining
                        ? `Complete ${access.practices_remaining} more practice session(s)`
                        : (locked ? 'Assessment not available' : ''));
            }
        } else if (assess) {
            assess.disabled = false;
            assess.title = '';
        }
    }

    function updateResultsBackButton() {
        const btn = document.getElementById('vt-results-back-btn');
        if (!btn) return;
        if (state.resultsReturnTo === 'admin') {
            btn.innerHTML = '<i class="fas fa-arrow-left"></i> Admin reports';
        } else {
            btn.innerHTML = '<i class="fas fa-arrow-left"></i> Scenarios';
        }
    }

    function categoryMeta(s) {
        const key = s.category || 'custom';
        const meta = CATEGORIES[key] || CATEGORIES.custom;
        return {
            key,
            label: s.custom_category_label || meta.label,
            icon: meta.icon,
        };
    }

    function traineeTurnCount(conversation) {
        return (conversation || []).filter((t) => t.speaker === 'trainee').length;
    }

    // Mirrors the server-side gate: an opening greeting on its own is not a call, so the
    // assessment can only be scored once the agent has replied to something the customer said.
    function customerWasAnswered(conversation) {
        const turns = conversation || [];
        const firstCustomer = turns.findIndex((t) => t.speaker === 'customer');
        if (firstCustomer < 0) return false;
        return turns.slice(firstCustomer + 1).some((t) => t.speaker === 'trainee');
    }

    function isPlaybackCurrent(epoch) {
        return epoch === state.playbackEpoch;
    }

    function stopActiveAudioElement() {
        const el = state.activeAudioEl;
        const objUrl = state.activeAudioObjectUrl;
        state.activeAudioEl = null;
        state.activeAudioObjectUrl = null;
        if (el) {
            try {
                el.pause();
                el.currentTime = 0;
                el.removeAttribute('src');
                el.load();
            } catch { /* ignore */ }
        }
        if (objUrl) {
            try {
                URL.revokeObjectURL(objUrl);
            } catch { /* ignore */ }
        }
    }

    function stopAllPlayback() {
        state.playbackEpoch += 1;
        try {
            state.synth?.cancel?.();
        } catch { /* ignore */ }
        stopActiveAudioElement();
        setCustomerSpeaking(false);
    }

    function stopSessionActivity() {
        stopAllPlayback();
        if (state.isListening && state.mediaRecorder && state.mediaRecorder.state !== 'inactive') {
            try {
                state.mediaRecorder.stop();
            } catch { /* ignore */ }
        }
        if (state.mediaStream) {
            state.mediaStream.getTracks().forEach((t) => t.stop());
            state.mediaStream = null;
        }
        if (state.isListening && state.recognition) {
            state.recognitionStopRequested = true;
            try {
                state.recognition.stop();
            } catch { /* ignore */ }
        }
        state.browserCaptureActive = false;
        resetVoiceUiState();
    }

    function updateSessionHeaderActions(mode) {
        const cancelBtn = document.getElementById('vt-cancel-assessment-btn');
        const endBtn = document.getElementById('vt-end-session-btn');
        const isAssessment = mode === 'assessment';
        cancelBtn?.classList.toggle('hidden', !isAssessment);
        if (endBtn) {
            if (isAssessment) {
                endBtn.innerHTML = '<i class="fas fa-chart-bar"></i> Submit for evaluation';
                endBtn.disabled = false;
                endBtn.title = '';
            } else {
                const ready = Boolean(state.conversationComplete);
                endBtn.innerHTML = ready
                    ? '<i class="fas fa-check"></i> Submit practice'
                    : 'End Session';
                endBtn.disabled = !ready;
                endBtn.title = ready
                    ? 'Submit this completed practice session'
                    : 'Complete the full practice call before submitting';
            }
        }
    }

    async function cancelAssessment() {
        if (!state.sessionId) {
            showVtSection('home');
            return;
        }
        if (!window.confirm('Cancel this assessment? You will return to Voice Training Agent with no score recorded.')) {
            return;
        }
        const sessionId = state.sessionId;
        stopSessionActivity();
        state.sessionId = null;
        state.sessionMode = null;
        state.scenarioId = null;
        state.lastConversation = [];
        state.conversationComplete = false;
        hideSessionCompleteBanner();
        showVtSection('home');
        try {
            await vtApi(`/sessions/${encodeURIComponent(sessionId)}/cancel`, 'POST');
        } catch {
            /* session may already be gone — still return home */
        }
        notify('Assessment cancelled — no score recorded.', 'info');
    }

    async function cancelPractice() {
        if (!state.sessionId) {
            showVtSection('home');
            return;
        }
        if (!window.confirm(
            'Leave practice without submitting? This session will not count toward unlocking assessment.',
        )) {
            return;
        }
        const sessionId = state.sessionId;
        stopSessionActivity();
        state.sessionId = null;
        state.sessionMode = null;
        state.scenarioId = null;
        state.lastConversation = [];
        state.conversationComplete = false;
        hideSessionCompleteBanner();
        showVtSection('home');
        try {
            await vtApi(`/sessions/${encodeURIComponent(sessionId)}/cancel`, 'POST');
        } catch {
            /* session may already be gone — still return home */
        }
        loadScenarios();
    }

    function showVtSection(name) {
        if (name !== 'session') {
            stopAllPlayback();
        }
        document.getElementById('vt-home-view')?.classList.toggle('hidden', name !== 'home');
        document.getElementById('vt-session-view')?.classList.toggle('hidden', name !== 'session');
        document.getElementById('vt-results-view')?.classList.toggle('hidden', name !== 'results');
    }

    function sortAssessmentsNewestFirst(rows) {
        return [...(rows || [])].sort((a, b) => {
            const da = new Date(a.created_at || 0).getTime();
            const db = new Date(b.created_at || 0).getTime();
            return db - da;
        });
    }

    function computeAssessmentStats(rows) {
        const list = Array.isArray(rows) ? rows : [];
        const empathyScores = [];
        const sopRates = [];
        const mastered = new Set();
        list.forEach((a) => {
            (a.metrics || []).forEach((m) => {
                if (m.parameter === 'Empathy') empathyScores.push(Number(m.score || 0));
            });
            sopRates.push(Number(a.sop_compliance_rate || 0));
            if (Number(a.final_percentage || 0) >= 80) {
                mastered.add(a.scenario_id || a.scenario_title || a.assessment_id);
            }
        });
        return {
            scenarios_mastered: mastered.size,
            total_assessments: list.length,
            average_empathy_score: empathyScores.length
                ? Math.round((empathyScores.reduce((s, v) => s + v, 0) / empathyScores.length) * 100) / 100
                : 0,
            sop_compliance_rate: sopRates.length
                ? Math.round((sopRates.reduce((s, v) => s + v, 0) / sopRates.length) * 10) / 10
                : 0,
        };
    }

    function getFilteredAssessments() {
        if (isTrainee()) return state.dashboardAssessments.own || [];
        const filter = state.assessmentViewFilter || 'all';
        if (filter === 'own') return state.dashboardAssessments.own || [];
        if (filter === 'trainees') return state.dashboardAssessments.trainees || [];
        return state.dashboardAssessments.all || [];
    }

    function updateAssessmentViewFilterUi() {
        const wrap = document.getElementById('vt-assessment-view-filter-wrap');
        const select = document.getElementById('vt-assessment-view-filter');
        if (!wrap || !select) return;
        const show = isStaff() && (
            (state.dashboardAssessments.own || []).length > 0
            || (state.dashboardAssessments.trainees || []).length > 0
        );
        wrap.classList.toggle('hidden', !show);
        if (show) select.value = state.assessmentViewFilter || 'all';
    }

    function renderDashboardStats(rows) {
        const statsEl = document.getElementById('vt-dashboard-stats');
        if (!statsEl) return;
        const stats = computeAssessmentStats(rows);
        statsEl.innerHTML = `
            <div class="vt-stat-card"><span class="vt-stat-value">${stats.scenarios_mastered || 0}</span><span class="vt-stat-label">Mastered (≥80%)</span></div>
            <button type="button" class="vt-stat-card vt-stat-card-clickable" id="vt-assessments-stat-card" title="View graded assessments">
                <span class="vt-stat-value">${stats.total_assessments || 0}</span><span class="vt-stat-label">Assessments</span>
            </button>
            <div class="vt-stat-card"><span class="vt-stat-value">${stats.average_empathy_score || 0}</span><span class="vt-stat-label">Avg Empathy</span></div>
            <div class="vt-stat-card"><span class="vt-stat-value">${stats.sop_compliance_rate || 0}%</span><span class="vt-stat-label">SOP Compliance</span></div>
        `;
        document.getElementById('vt-assessments-stat-card')?.addEventListener('click', () => {
            document.getElementById('vt-assessments-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    }

    function applyAssessmentViewFilter() {
        updateAssessmentViewFilterUi();
        const rows = getFilteredAssessments();
        renderDashboardStats(rows);
        renderMyAssessments(rows);
    }

    async function loadDashboard() {
        const statsEl = document.getElementById('vt-dashboard-stats');
        const heroEl = document.getElementById('vt-hero-stats');
        const role = (window.userProfile?.role || '').toLowerCase();
        try {
            const [data, archivedList, ownAssessments, traineeAssessments] = await Promise.all([
                vtApi('/dashboard'),
                isStaff() ? vtApi('/scenarios?archived_only=true').catch(() => []) : Promise.resolve([]),
                vtApi('/assessments').catch(() => []),
                role === 'trainer' || role === 'super_admin'
                    ? vtApi('/trainer/trainee-assessments').catch(() => [])
                    : Promise.resolve([]),
            ]);
            state.archivedCount = Array.isArray(archivedList) ? archivedList.length : 0;
            const scenarioCount = state.showArchived ? state.archivedCount : state.scenarios.filter((s) => !s.archived).length;
            const catCount = new Set(state.scenarios.map((s) => s.category)).size;
            if (heroEl && !isTrainee()) {
                heroEl.innerHTML = `
                    <div class="vt-hero-stat"><span class="vt-hero-stat-val">${scenarioCount}</span><span class="vt-hero-stat-lbl">${state.showArchived ? 'Archived' : 'Active scenarios'}</span></div>
                    <div class="vt-hero-stat"><span class="vt-hero-stat-val">${catCount}</span><span class="vt-hero-stat-lbl">Categories</span></div>
                    <button type="button" id="vt-hero-archived-btn" class="vt-hero-stat vt-hero-stat-archive ${state.showArchived ? 'active' : ''}" title="View archived scenarios">
                        <span class="vt-hero-stat-val">${state.archivedCount}</span>
                        <span class="vt-hero-stat-lbl"><i class="fas fa-box-archive"></i> Archived</span>
                    </button>
                `;
                document.getElementById('vt-hero-archived-btn')?.addEventListener('click', toggleArchivedView);
            } else if (heroEl && isTrainee()) {
                heroEl.innerHTML = '';
            }
            const own = sortAssessmentsNewestFirst(Array.isArray(ownAssessments) ? ownAssessments : []);
            const trainees = sortAssessmentsNewestFirst(
                (Array.isArray(traineeAssessments) ? traineeAssessments : []).map((row) => ({
                    ...row,
                    is_trainee_result: true,
                })),
            );
            state.dashboardAssessments = {
                own,
                trainees,
                all: sortAssessmentsNewestFirst([...own, ...trainees]),
            };
            if (!isStaff()) {
                state.assessmentViewFilter = 'own';
            } else if (!['all', 'own', 'trainees'].includes(state.assessmentViewFilter)) {
                state.assessmentViewFilter = 'all';
            }
            applyAssessmentViewFilter();
        } catch {
            if (statsEl) statsEl.innerHTML = '<p class="placeholder-text">Could not load dashboard stats.</p>';
        }
    }

    function renderMyAssessments(assessments) {
        const section = document.getElementById('vt-assessments-section');
        const list = document.getElementById('vt-assessments-list');
        const heading = document.getElementById('vt-assessments-heading');
        if (!section || !list) return;
        const rows = Array.isArray(assessments) ? assessments : [];
        const filter = state.assessmentViewFilter || 'all';
        const showTraineeCol = isStaff() && (
            filter === 'trainees'
            || (filter === 'all' && rows.some((r) => r.is_trainee_result))
        );
        const showAttemptCol = rows.some((r) => (r.attempt_total || 0) > 1 || r.attempt_number);
        const sub = document.querySelector('#vt-assessments-section .vt-assessments-sub');
        if (heading) {
            if (isTrainee()) {
                heading.innerHTML = '<i class="fas fa-chart-bar"></i> My Completed Assessments';
            } else if (filter === 'own') {
                heading.innerHTML = '<i class="fas fa-chart-bar"></i> My Assessments';
            } else if (filter === 'trainees') {
                heading.innerHTML = '<i class="fas fa-chart-bar"></i> Trainee Assessments';
            } else {
                heading.innerHTML = '<i class="fas fa-chart-bar"></i> Graded Assessments <small class="vt-assessments-sub-inline">(all attempts)</small>';
            }
        }
        if (sub && isStaff()) {
            if (filter === 'own') {
                sub.textContent = 'Your own voice assessment attempts across scenarios.';
            } else if (filter === 'trainees') {
                sub.textContent = 'Completed assessments from trainees in your published batches.';
            } else {
                sub.textContent = 'All assessments — yours and your batch trainees. Use the filter to separate views.';
            }
        }
        if (!rows.length) {
            if (isStaff() && (
                (state.dashboardAssessments.own || []).length
                || (state.dashboardAssessments.trainees || []).length
            )) {
                section.classList.remove('hidden');
                const emptyMsg = filter === 'own'
                    ? 'You have not completed any voice assessments yet.'
                    : filter === 'trainees'
                    ? 'No trainee assessments yet for your published scenarios.'
                    : 'No assessments to show.';
                list.innerHTML = `<p class="placeholder-text">${emptyMsg}</p>`;
                return;
            }
            section.classList.add('hidden');
            list.innerHTML = '';
            return;
        }
        section.classList.remove('hidden');
        list.innerHTML = `
            <table class="vt-assessments-table">
                <thead>
                    <tr>
                        <th>Date</th>
                        ${showTraineeCol ? '<th>Trainee</th>' : ''}
                        <th>Scenario</th>
                        ${showAttemptCol ? '<th>Attempt</th>' : ''}
                        <th>Grade</th>
                        <th>Score</th>
                        <th>SOP</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows.map((a) => `
                        <tr class="${a.passed ? 'vt-assessment-passed-row' : ''}">
                            <td>${formatVtDate(a.created_at)}</td>
                            ${showTraineeCol ? `<td>${escapeHtml(a.user_email || (a.is_trainee_result ? 'Trainee' : '—'))}</td>` : ''}
                            <td><strong>${escapeHtml(a.scenario_title || 'Assessment')}</strong></td>
                            ${showAttemptCol ? `<td>${a.attempt_number ? `#${a.attempt_number}${(a.attempt_total || 0) > 1 ? ` / ${a.attempt_total}` : ''}` : '—'}${a.passed ? ' <span class="vt-pass-tag">Pass</span>' : ''}</td>` : ''}
                            <td><span class="vt-grade-pill grade-${escapeHtml(a.grade || '')}">${escapeHtml(a.grade || '—')}</span></td>
                            <td>${a.final_percentage || 0}%</td>
                            <td>${a.sop_compliance_rate || 0}%</td>
                            <td class="vt-assessments-actions">
                                <button type="button" class="btn btn-primary btn-sm vt-my-view-assessment" data-id="${escapeHtml(a.assessment_id || '')}">
                                    <i class="fas fa-chart-bar"></i> View results
                                </button>
                                ${a.assessment_id ? `<button type="button" class="btn btn-secondary btn-sm vt-my-download-pdf" data-id="${escapeHtml(a.assessment_id)}">
                                    <i class="fas fa-file-pdf"></i> PDF
                                </button>` : ''}
                                ${a.assessment_id && isStaff() && !a.is_trainee_result ? `<button type="button" class="btn btn-danger btn-sm vt-my-delete-assessment" data-id="${escapeHtml(a.assessment_id)}" data-title="${escapeHtml(a.scenario_title || 'Assessment')}" title="Delete assessment">
                                    <i class="fas fa-trash"></i>
                                </button>` : ''}
                            </td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        `;
        list.querySelectorAll('.vt-my-view-assessment').forEach((btn) => {
            btn.addEventListener('click', () => viewMyAssessment(btn.dataset.id));
        });
        list.querySelectorAll('.vt-my-download-pdf').forEach((btn) => {
            btn.addEventListener('click', () => downloadAssessmentPdfById(btn.dataset.id));
        });
        list.querySelectorAll('.vt-my-delete-assessment').forEach((btn) => {
            btn.addEventListener('click', () => deleteMyAssessment(btn.dataset.id, btn.dataset.title));
        });
    }

    async function deleteMyAssessment(assessmentId, title) {
        if (!assessmentId) return;
        const label = title || 'this assessment';
        if (!window.confirm(`Delete "${label}"?\n\nThis permanently removes the graded report and PDF. This cannot be undone.`)) return;
        try {
            await vtApi(`/assessments/${encodeURIComponent(assessmentId)}`, 'DELETE');
            notify('Assessment deleted.', 'success');
            await loadDashboard();
        } catch (err) {
            notify(err?.message || 'Could not delete assessment.', 'error');
        }
    }

    async function viewMyAssessment(assessmentId) {
        if (!assessmentId) return;
        try {
            if (typeof window.showLoader === 'function') window.showLoader('Loading assessment...');
            const data = await vtApi(`/assessments/${encodeURIComponent(assessmentId)}`);
            state.resultsReturnTo = 'home';
            showResults(data);
        } catch (err) {
            notify(err?.message || 'Could not load assessment results.', 'error');
        } finally {
            if (typeof window.hideLoader === 'function') window.hideLoader();
        }
    }

    function toggleArchivedView() {
        state.showArchived = !state.showArchived;
        const cb = document.getElementById('vt-show-archived');
        const btn = document.getElementById('vt-view-archived-btn');
        if (cb) cb.checked = state.showArchived;
        if (btn) btn.classList.toggle('active', state.showArchived);
        loadScenarios();
    }

    function bindArchivedControls() {
        const cb = document.getElementById('vt-show-archived');
        const btn = document.getElementById('vt-view-archived-btn');
        if (cb) cb.checked = state.showArchived;
        if (btn) btn.classList.toggle('active', state.showArchived);
    }

    /** Clear category / status / search (and archived view) so new scenarios are visible. */
    function resetScenarioListFilters() {
        state.filterCategory = '';
        state.filterPublish = '';
        state.searchQuery = '';
        if (state.showArchived) {
            state.showArchived = false;
            const cb = document.getElementById('vt-show-archived');
            const btn = document.getElementById('vt-view-archived-btn');
            if (cb) cb.checked = false;
            if (btn) btn.classList.remove('active');
        }
        const cat = document.getElementById('vt-category-filter');
        const pub = document.getElementById('vt-publish-filter');
        const search = document.getElementById('vt-scenario-search');
        if (cat) cat.value = '';
        if (pub) pub.value = '';
        if (search) search.value = '';
    }

    function filteredScenarios() {
        let list = state.scenarios;
        if (state.filterCategory) {
            list = list.filter((s) => s.category === state.filterCategory);
        }
        if (state.filterPublish === 'published') {
            list = list.filter((s) => Boolean(s.publish?.is_published));
        } else if (state.filterPublish === 'unpublished') {
            list = list.filter((s) => !s.publish?.is_published);
        } else if (state.filterPublish === 'configured') {
            list = list.filter((s) => Boolean(s.assessment_config?.configured));
        } else if (state.filterPublish === 'not_configured') {
            list = list.filter((s) => !s.assessment_config?.configured);
        }
        const q = (state.searchQuery || '').trim().toLowerCase();
        if (!q) return list;
        return list.filter((s) => {
            const cat = categoryMeta(s);
            const haystack = [
                s.title,
                s.description,
                s.customer_persona,
                s.category,
                cat.label,
            ].filter(Boolean).join(' ').toLowerCase();
            return haystack.includes(q);
        });
    }

    function applyScenarioListFilters() {
        const list = document.getElementById('vt-scenarios-list');
        if (!list) return;
        if (!state.scenarios.length) {
            loadScenarios();
            return;
        }
        const visible = filteredScenarios();
        if (!visible.length) {
            list.innerHTML = `<div class="vt-empty-state">
                <i class="fas fa-search"></i>
                <h3>No matching scenarios</h3>
                <p>Try a different search term or clear the category filter.</p>
            </div>`;
            return;
        }
        list.innerHTML = visible.map(renderScenarioCard).join('');
        bindScenarioCardEvents(list);
    }

    function renderCardTopActions(s, archived) {
        if (!isStaff()) return '';
        const id = escapeHtml(s.id);
        const title = escapeHtml(s.title || '');
        if (archived) {
            return `<div class="vt-card-archived-actions">
                <button type="button" class="vt-restore-btn" data-id="${id}" title="Restore scenario"><i class="fas fa-rotate-left"></i> Restore</button>
                <button type="button" class="vt-permanent-delete-btn" data-id="${id}" data-title="${title}" title="Delete permanently"><i class="fas fa-trash"></i> Delete</button>
            </div>`;
        }
        return `<div class="vt-card-top-actions">
            <button type="button" class="vt-archive-btn" data-id="${id}" title="Archive scenario"><i class="fas fa-box-archive"></i></button>
        </div>`;
    }

    function renderTraineeAssessmentResults(results, passPercentage, compact = false) {
        const rows = Array.isArray(results) ? results : [];
        if (!rows.length) {
            return compact
                ? '<p class="vt-card-tab-empty"><i class="fas fa-user-clock"></i> No trainee attempts yet.</p>'
                : '';
        }
        const completed = rows.filter((r) => (r.completed_count || 0) > 0).length;
        const passPct = passPercentage || rows[0]?.pass_percentage || 70;
        return `
            <div class="vt-trainee-results ${compact ? 'compact' : ''}">
                <div class="vt-trainee-results-head">
                    <strong><i class="fas fa-user-graduate"></i> Trainee assessments</strong>
                    <span class="vt-trainee-results-meta">${completed}/${rows.length} completed · pass ${passPct}%</span>
                </div>
                <div class="vt-trainee-results-list">
                    ${rows.map((t) => `
                        <div class="vt-trainee-result-row">
                            <div class="vt-trainee-result-user">
                                <span>${escapeHtml(t.trainee_email || 'Trainee')}</span>
                                ${t.has_passed ? '<span class="vt-pass-tag">Passed</span>' : ''}
                            </div>
                            <div class="vt-trainee-result-attempts">
                                ${(t.attempts || []).length
                                    ? (t.attempts || []).map((a) => `
                                        <button type="button" class="vt-attempt-pill ${a.passed ? 'passed' : 'failed'} vt-view-trainee-attempt" data-id="${escapeHtml(a.assessment_id || '')}" title="View attempt ${a.attempt_number}">
                                            Attempt ${a.attempt_number}: ${a.final_percentage || 0}% (${escapeHtml(a.grade || '—')})
                                        </button>
                                    `).join('')
                                    : '<span class="vt-attempt-pill pending">Not attempted</span>'}
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    }

    function renderScenarioCardStaffTabs(s, assessConfigured, audience) {
        const scenarioId = escapeHtml(s.id);
        const scenarioTitle = escapeHtml(s.title || '');
        const isPublished = Boolean(s.publish?.is_published);
        const resultsHtml = renderTraineeAssessmentResults(
            s.trainee_assessment_results,
            s.publish?.pass_percentage,
            true,
        );

        const statusPanel = `
            <div class="vt-card-status-row">
                <span class="vt-config-status ${assessConfigured ? 'configured' : 'pending'}"><i class="fas ${assessConfigured ? 'fa-check-circle' : 'fa-circle-exclamation'}"></i> ${assessConfigured ? 'Assessment configured' : 'Assessment not configured'}</span>
                ${isPublished
                    ? '<span class="vt-published-tag"><i class="fas fa-share-square"></i> Published</span>'
                    : '<span class="vt-draft-tag"><i class="fas fa-lock"></i> Not published</span>'}
            </div>
            <p class="vt-card-tab-hint">${isPublished
                ? `<i class="fas fa-users"></i> Live for <strong>${audience.trainee_count || 0}</strong> trainee${(audience.trainee_count || 0) === 1 ? '' : 's'}. Use the Audience and Results tabs for details.`
                : '<i class="fas fa-circle-info"></i> Configure assessment, then publish to assign trainees.'}</p>
            <button type="button" class="btn btn-warning btn-sm vt-config-assess-btn" data-id="${scenarioId}" data-title="${scenarioTitle}"><i class="fas fa-clipboard-check"></i> ${assessConfigured ? 'Edit Assessment Config' : 'Configure Assessment'}</button>
        `;

        const audiencePanel = isPublished
            ? `<div class="vt-audience-summary compact">
                <div class="vt-audience-summary-text">
                    <i class="fas fa-users"></i>
                    <span>${audience.batch_names?.length
                        ? `Batches: <strong>${escapeHtml(audience.batch_names.join(', '))}</strong>`
                        : (audience.publication_type === 'all_process' ? 'All trainees in process' : 'No batches selected')}</span>
                    <span class="vt-audience-count-pill">${audience.trainee_count || 0} trainee${(audience.trainee_count || 0) === 1 ? '' : 's'}</span>
                </div>
                <button type="button" class="btn btn-secondary btn-sm vt-manage-audience-btn" data-id="${scenarioId}" data-title="${scenarioTitle}">
                    <i class="fas fa-user-gear"></i> Manage trainees
                </button>
               </div>`
            : '<p class="vt-card-tab-empty"><i class="fas fa-lock"></i> Publish this scenario to assign batches and trainees.</p>';

        const resultsPanel = isPublished
            ? resultsHtml
            : '<p class="vt-card-tab-empty"><i class="fas fa-chart-simple"></i> Trainee assessment results appear here after publishing.</p>';

        return `
            <div class="vt-card-tabs-wrap" data-card-id="${scenarioId}">
                <div class="vt-card-tabs" role="tablist" aria-label="Scenario details for ${scenarioTitle}">
                    <button type="button" class="vt-card-tab active" role="tab" aria-selected="true" data-tab="status" id="vt-tab-status-${scenarioId}" aria-controls="vt-panel-status-${scenarioId}">Status</button>
                    <button type="button" class="vt-card-tab" role="tab" aria-selected="false" data-tab="audience" id="vt-tab-audience-${scenarioId}" aria-controls="vt-panel-audience-${scenarioId}">Audience</button>
                    <button type="button" class="vt-card-tab" role="tab" aria-selected="false" data-tab="results" id="vt-tab-results-${scenarioId}" aria-controls="vt-panel-results-${scenarioId}">Results</button>
                </div>
                <div class="vt-card-tab-panels">
                    <div class="vt-card-tab-panel active" data-panel="status" role="tabpanel" id="vt-panel-status-${scenarioId}" aria-labelledby="vt-tab-status-${scenarioId}">${statusPanel}</div>
                    <div class="vt-card-tab-panel" data-panel="audience" role="tabpanel" id="vt-panel-audience-${scenarioId}" aria-labelledby="vt-tab-audience-${scenarioId}" hidden>${audiencePanel}</div>
                    <div class="vt-card-tab-panel" data-panel="results" role="tabpanel" id="vt-panel-results-${scenarioId}" aria-labelledby="vt-tab-results-${scenarioId}" hidden>${resultsPanel}</div>
                </div>
            </div>
        `;
    }

    function renderTraineeProgressPanel(access) {
        const minPracticeRaw = Number(access.min_practice_sessions ?? 1);
        const minPractice = Number.isFinite(minPracticeRaw) ? Math.max(0, Math.floor(minPracticeRaw)) : 1;
        const practiceCount = access.practice_count || 0;
        const assessmentUnlocked = Boolean(access.assessment_unlocked);
        const passed = Boolean(access.has_passed || access.assessment_passed);
        const passPct = access.pass_percentage ?? 70;
        const bestPct = access.best_percentage ?? 0;
        const attemptsLeft = access.assessment_attempts_left ?? 0;
        const maxAttempts = access.max_assessment_attempts ?? 5;
        const assessmentCount = access.assessment_count || 0;
        const scenarioAssessments = access.scenario_assessments || [];
        const practiceDisabled = minPractice <= 0 || access.practice_allowed === false;
        const practiceComplete = !practiceDisabled && practiceCount >= minPractice;
        const passTargetLabel = `${passPct}% required to pass`;

        let practiceChip;
        if (practiceDisabled) {
            practiceChip = '<span class="vt-trainee-stat-chip muted">Disabled</span>';
        } else if (practiceComplete || passed) {
            practiceChip = '<span class="vt-trainee-stat-chip success">Complete</span>';
        } else if (practiceCount > 0) {
            practiceChip = '<span class="vt-trainee-stat-chip info">In progress</span>';
        } else {
            practiceChip = '<span class="vt-trainee-stat-chip muted">Not started</span>';
        }

        let assessmentValue;
        let assessmentChip;
        if (passed) {
            assessmentValue = `Best score ${bestPct}% · ${passTargetLabel}`;
            assessmentChip = '<span class="vt-trainee-stat-chip success"><i class="fas fa-check"></i> Passed</span>';
        } else if (scenarioAssessments.length) {
            const latest = scenarioAssessments[scenarioAssessments.length - 1];
            const latestPct = latest.final_percentage || 0;
            const latestGrade = latest.grade || '—';
            assessmentValue = `${assessmentCount}/${maxAttempts} attempts · latest ${latestPct}% (${escapeHtml(latestGrade)}) · ${passTargetLabel}`;
            assessmentChip = latest.passed
                ? '<span class="vt-trainee-stat-chip success">Pass</span>'
                : '<span class="vt-trainee-stat-chip fail">Below pass</span>';
        } else if (assessmentUnlocked) {
            assessmentValue = `Ready to start · ${passTargetLabel} · ${attemptsLeft} attempt${attemptsLeft === 1 ? '' : 's'} left`;
            assessmentChip = '<span class="vt-trainee-stat-chip info">Unlocked</span>';
        } else {
            assessmentValue = `Locked until practice is done (${practiceCount}/${minPractice}) · ${passTargetLabel}`;
            assessmentChip = '<span class="vt-trainee-stat-chip warn">Locked</span>';
        }

        const attemptPills = scenarioAssessments.length
            ? `<div class="vt-trainee-attempt-stats">
                ${scenarioAssessments.map((a) => `
                    <span class="vt-trainee-attempt-stat ${a.passed ? 'passed' : 'failed'}">
                        Attempt ${a.attempt_number || 1}: ${a.final_percentage || 0}% (${escapeHtml(a.grade || '—')})${a.passed ? ' · Pass' : ' · Fail'}
                    </span>
                `).join('')}
               </div>`
            : '';

        return `
            <div class="vt-card-trainee-panel">
                <div class="vt-trainee-stats">
                    <div class="vt-trainee-stat-row">
                        <div class="vt-trainee-stat-main">
                            <span class="vt-trainee-stat-label"><i class="fas fa-bullseye"></i> Practice</span>
                            <span class="vt-trainee-stat-value">${practiceDisabled
                                ? 'Not required for you — Practice is disabled (trainer set practice sessions to 0)'
                                : `${practiceCount}/${minPractice} session${minPractice === 1 ? '' : 's'} complete · available`}</span>
                        </div>
                        ${practiceChip}
                    </div>
                    <div class="vt-trainee-stat-row">
                        <div class="vt-trainee-stat-main">
                            <span class="vt-trainee-stat-label"><i class="fas fa-chart-bar"></i> Assessment</span>
                            <span class="vt-trainee-stat-value">${assessmentValue}</span>
                        </div>
                        ${assessmentChip}
                    </div>
                    ${attemptPills}
                    ${scenarioAssessments.length ? '<p class="vt-trainee-stats-hint"><i class="fas fa-circle-info"></i> View full results and PDF in <strong>My Completed Assessments</strong> above.</p>' : ''}
                </div>
            </div>
        `;
    }

    function renderPublisherNote(s) {
        if (!isTrainee() || !s.publish?.is_published) return '';
        const pub = s.publisher || {};
        const email = pub.email || pub.author_email || 'your trainer';
        const role = pub.role_label || 'Trainer';
        const when = pub.published_at ? formatVtDate(pub.published_at) : '';
        return `<p class="vt-publisher-note">
            <i class="fas fa-share-square"></i>
            Shared by <strong>${escapeHtml(email)}</strong>
            <span class="vt-publisher-role">${escapeHtml(role)}</span>
            ${when ? `<span class="vt-publisher-when">· ${when}</span>` : ''}
        </p>`;
    }

    function renderScenarioCard(s) {
        const cat = categoryMeta(s);
        const mood = (s.opening_sentiment || 'neutral').toLowerCase();
        const turns = s.turn_count || 0;
        const sop = s.sop_count || 0;
        const persona = plainTextDisplay(s.customer_persona) || 'Customer persona will appear after preview.';
        const description = plainTextDisplay(s.description || '');
        const archived = Boolean(s.archived);
        const access = s.access || {};
        const assessLocked = isTrainee() && access.assessment_unlocked === false;
        const assessPassed = isTrainee() && (access.has_passed || access.assessment_passed);
        const assessDisabled = isTrainee() && (access.can_start_assessment === false || assessPassed);
        const assessTitle = assessPassed
            ? `Passed (${access.best_percentage ?? 0}% — required ${access.pass_percentage ?? 70}%). Assessment locked.`
            : assessLocked
            ? `Complete ${access.practices_remaining ?? 0} more practice session(s) first`
            : (assessDisabled ? 'No assessment attempts remaining' : 'Start assessment');
        const assessCfg = s.assessment_config || {};
        const assessConfigured = Boolean(assessCfg.configured);
        const audience = s.publish_audience || {};
        const scenarioId = escapeHtml(s.id);
        const scenarioTitle = escapeHtml(s.title || '');
        const practiceDisabled = isPracticeDisabledForScenario(s);
        const practiceTitle = practiceDisabled
            ? 'Practice is disabled for trainees because practice sessions before assessment is set to 0'
            : 'Start practice';

        const traineePanel = isTrainee() && !archived
            ? renderTraineeProgressPanel(access)
            : '';

        const staffTabs = isStaff() && !archived
            ? renderScenarioCardStaffTabs(s, assessConfigured, audience)
            : (traineePanel || '<div class="vt-card-panel-spacer" aria-hidden="true"></div>');

        const staffPublish = isStaff() && !archived
            ? `<button type="button" class="btn btn-secondary btn-sm vt-publish-btn" data-id="${scenarioId}" data-title="${scenarioTitle}" data-configured="${assessConfigured ? '1' : '0'}" ${assessConfigured ? '' : 'disabled title="Configure assessment before publishing"'}><i class="fas fa-share-square"></i> ${s.publish?.is_published ? 'Republish' : 'Publish'}</button>`
            : '';

        const previewBtn = archived ? '' : `<button type="button" class="vt-preview-btn" data-id="${scenarioId}"><i class="fas fa-eye"></i> Preview scenario</button>`;

        const actionBlock = archived ? '' : `
            ${staffTabs}
            <div class="vt-card-footer">
                ${previewBtn}
                <div class="vt-card-actions ${isStaff() ? 'vt-card-actions-staff' : ''}">
                    ${staffPublish}
                    <button type="button" class="btn btn-secondary btn-sm vt-practice-btn" data-id="${scenarioId}" ${practiceDisabled ? 'disabled' : ''} title="${escapeHtml(practiceTitle)}"><i class="fas fa-bullseye"></i> Practice</button>
                    <button type="button" class="btn btn-primary btn-sm vt-assess-btn" data-id="${scenarioId}" ${assessDisabled || assessLocked ? 'disabled' : ''} title="${escapeHtml(assessTitle)}"><i class="fas fa-chart-bar"></i> Assessment</button>
                </div>
            </div>`;

        return `
            <article class="vt-scenario-card ${archived ? 'archived' : ''}" data-id="${scenarioId}" tabindex="0" role="button" aria-label="Preview ${scenarioTitle}">
                <div class="vt-card-top">
                    <div class="vt-card-top-badges">
                        <span class="vt-cat-badge"><i class="fas ${cat.icon}"></i> ${escapeHtml(cat.label)}</span>
                        ${archived ? '<span class="vt-archived-tag">Archived</span>' : ''}
                        <span class="vt-mood-badge ${mood}">${escapeHtml(mood)}</span>
                    </div>
                    ${archived ? '' : renderCardTopActions(s, archived)}
                </div>
                ${archived ? renderCardTopActions(s, archived) : ''}
                <h3 class="vt-card-title">${escapeHtml(s.title)}</h3>
                <p class="vt-card-desc">${escapeHtml(description)}</p>
                ${renderPublisherNote(s)}
                <div class="vt-persona-box">
                    <span class="vt-persona-label">Customer</span>
                    <p>${escapeHtml(persona)}</p>
                </div>
                <div class="vt-card-meta">
                    <span><i class="fas fa-comments"></i> ${turns} turns</span>
                    <span><i class="fas fa-list-check"></i> ${sop} SOP steps</span>
                </div>
                ${actionBlock}
            </article>
        `;
    }

    async function archiveScenario(scenarioId) {
        if (!scenarioId) return;
        if (!window.confirm('Archive this scenario? You can restore it later from Show archived.')) return;
        try {
            await vtApi(`/scenarios/${encodeURIComponent(scenarioId)}`, 'DELETE');
            notify('Scenario archived.', 'success');
            await loadScenarios();
        } catch {
            notify('Could not archive scenario.', 'error');
        }
    }

    async function restoreScenario(scenarioId) {
        if (!scenarioId) return;
        try {
            await vtApi(`/scenarios/${encodeURIComponent(scenarioId)}/restore`, 'POST');
            notify('Scenario restored.', 'success');
            await loadScenarios();
        } catch {
            notify('Could not restore scenario.', 'error');
        }
    }

    async function permanentDeleteScenario(scenarioId, title) {
        if (!scenarioId) return;
        const label = title || 'this scenario';
        if (!window.confirm(`Permanently delete "${label}"?\n\nThis removes the scenario and all related practice sessions and assessments. This cannot be undone.`)) return;
        try {
            await vtApi(`/scenarios/${encodeURIComponent(scenarioId)}/permanent`, 'DELETE');
            notify('Scenario permanently deleted.', 'success');
            await loadScenarios();
        } catch (err) {
            notify(err?.message || 'Could not delete scenario.', 'error');
        }
    }

    function normalizeTrainerBatchesResponse(res) {
        if (!res) return [];
        if (Array.isArray(res)) return res;
        if (Array.isArray(res.batches)) return res.batches;
        if (Array.isArray(res.data?.batches)) return res.data.batches;
        return [];
    }

    function updateVtPublishBatchVisibility() {
        const pubType = document.querySelector('input[name="vt-publication-type"]:checked')?.value || 'batch';
        const singleGroup = document.getElementById('vt-batch-selection-group');
        const multiGroup = document.getElementById('vt-multi-batch-selection-group');
        if (singleGroup) singleGroup.classList.toggle('hidden', pubType !== 'batch');
        if (multiGroup) multiGroup.classList.toggle('hidden', pubType !== 'multi_batch');
    }

    function renderVtPublishConfigSummary(assessmentConfig) {
        const el = document.getElementById('vt-publish-config-summary');
        if (!el) return;
        const cfg = assessmentConfig || {};
        el.innerHTML = `
            <h4><i class="fas fa-clipboard-check"></i> Assessment settings</h4>
            <ul>
                <li>Practice sessions before assessment: <strong>${cfg.min_practice_sessions ?? 1}</strong></li>
                <li>Max assessment attempts: <strong>${cfg.max_assessment_attempts ?? 3}</strong></li>
                <li>Pass percentage: <strong>${cfg.pass_percentage ?? 70}%</strong></li>
            </ul>`;
    }

    function closeVtAssessmentConfigModal() {
        document.getElementById('vt-assessment-config-modal')?.classList.add('hidden');
    }

    async function openVtAssessmentConfigModal(scenarioId, scenarioTitle) {
        if (!scenarioId || !isStaff()) return;
        const modal = document.getElementById('vt-assessment-config-modal');
        if (!modal) return;
        document.getElementById('vt-assessment-config-scenario-id').value = scenarioId;
        document.getElementById('vt-assessment-config-scenario').textContent = scenarioTitle || 'Scenario';
        try {
            const data = await vtApi(`/scenarios/${encodeURIComponent(scenarioId)}/assessment-config`);
            const cfg = data?.assessment_config || {};
            document.getElementById('vt-min-practice-sessions').value = String(cfg.min_practice_sessions ?? 1);
            document.getElementById('vt-max-assessment-attempts').value = String(cfg.max_assessment_attempts ?? 3);
            document.getElementById('vt-pass-percentage').value = String(cfg.pass_percentage ?? 70);
            if (data?.scenario_title) {
                document.getElementById('vt-assessment-config-scenario').textContent = data.scenario_title;
            }
        } catch (err) {
            notify(err?.message || 'Could not load assessment configuration.', 'error');
            return;
        }
        modal.classList.remove('hidden');
    }

    async function submitVtAssessmentConfig(e) {
        e.preventDefault();
        const scenarioId = document.getElementById('vt-assessment-config-scenario-id')?.value;
        if (!scenarioId) return;
        const payload = {
            min_practice_sessions: Number(document.getElementById('vt-min-practice-sessions')?.value || 1),
            max_assessment_attempts: Number(document.getElementById('vt-max-assessment-attempts')?.value || 3),
            pass_percentage: Number(document.getElementById('vt-pass-percentage')?.value || 70),
        };
        try {
            const res = await vtApi(`/scenarios/${encodeURIComponent(scenarioId)}/assessment-config`, 'POST', payload);
            notify(res.message || 'Assessment configuration saved.', 'success');
            closeVtAssessmentConfigModal();
            await loadScenarios();
        } catch (err) {
            notify(err?.message || 'Could not save assessment configuration.', 'error');
        }
    }

    function closeVtPublishModal() {
        document.getElementById('vt-publish-modal')?.classList.add('hidden');
    }

    async function openVtPublishModal(scenarioId, scenarioTitle) {
        if (!scenarioId || !isStaff()) return;
        const scenario = state.scenarios.find((s) => s.id === scenarioId);
        if (scenario && !scenario.assessment_config?.configured) {
            notify('Configure assessment before publishing.', 'warning');
            openVtAssessmentConfigModal(scenarioId, scenarioTitle || scenario.title);
            return;
        }
        const modal = document.getElementById('vt-publish-modal');
        if (!modal) return;
        document.getElementById('vt-publish-scenario-id').value = scenarioId;
        document.getElementById('vt-publish-scenario-name').textContent = scenarioTitle || scenario?.title || 'Scenario';
        renderVtPublishConfigSummary(scenario?.assessment_config || scenario?.publish);
        const batchSelect = document.getElementById('vt-publish-batch-select');
        const multiContainer = document.getElementById('vt-multi-batch-checkboxes');
        if (batchSelect) batchSelect.innerHTML = '<option value="">Loading batches...</option>';
        if (multiContainer) multiContainer.innerHTML = '<p class="placeholder-text">Loading batches...</p>';

        const allProcessWrap = document.getElementById('vt-publish-all-process-wrap');
        if (allProcessWrap) {
            allProcessWrap.classList.toggle('hidden', !isSuperAdmin());
        }

        modal.classList.remove('hidden');
        updateVtPublishBatchVisibility();

        try {
            const batchesRes = await window.apiCall('/trainer/batches');
            if (batchesRes?.success === false) {
                throw new Error(batchesRes?.error?.detail || 'Could not load batches.');
            }
            const batches = normalizeTrainerBatchesResponse(batchesRes);
            if (!batches.length) {
                if (batchSelect) batchSelect.innerHTML = '<option value="">No batches — create one in Trainer Dashboard</option>';
                if (multiContainer) multiContainer.innerHTML = '<p class="text-muted">No batches available. Create a batch first.</p>';
                return;
            }
            if (batchSelect) {
                batchSelect.innerHTML = '<option value="">Select a batch...</option>';
                batches.forEach((batch) => {
                    const option = document.createElement('option');
                    option.value = batch.id;
                    const name = batch.batch_name || batch.name || 'Unnamed batch';
                    const count = batch.member_count || batch.trainee_count || 0;
                    option.textContent = `${name} (${count} trainee${count === 1 ? '' : 's'})`;
                    batchSelect.appendChild(option);
                });
            }
            if (multiContainer) {
                multiContainer.innerHTML = batches.map((batch) => {
                    const name = escapeHtml(batch.batch_name || batch.name || 'Unnamed batch');
                    const count = batch.member_count || batch.trainee_count || 0;
                    return `<label class="batch-checkbox-item"><input type="checkbox" name="vt-batch-checkbox" value="${escapeHtml(batch.id)}"><span>${name} <small>(${count} trainee${count === 1 ? '' : 's'})</small></span></label>`;
                }).join('');
            }
        } catch (err) {
            notify(err?.message || 'Could not load batches.', 'error');
        }
    }

    async function submitVtPublish(e) {
        e.preventDefault();
        const scenarioId = document.getElementById('vt-publish-scenario-id')?.value;
        if (!scenarioId) return;
        const publicationType = document.querySelector('input[name="vt-publication-type"]:checked')?.value || 'batch';
        let batchIds = [];
        if (publicationType === 'batch') {
            const batchId = document.getElementById('vt-publish-batch-select')?.value;
            if (!batchId) {
                notify('Please select a batch.', 'warning');
                return;
            }
            batchIds = [batchId];
        } else if (publicationType === 'multi_batch') {
            batchIds = Array.from(document.querySelectorAll('input[name="vt-batch-checkbox"]:checked')).map((cb) => cb.value);
            if (!batchIds.length) {
                notify('Select at least one batch.', 'warning');
                return;
            }
        }
        const scenario = state.scenarios.find((s) => s.id === scenarioId);
        if (scenario?.publish?.is_published) {
            if (!window.confirm(
                'Republishing will reset practice and assessment progress for all trainees in the selected batch(es). Continue?',
            )) {
                return;
            }
        }
        try {
            const res = await vtApi(`/scenarios/${encodeURIComponent(scenarioId)}/publish`, 'POST', {
                publication_type: publicationType,
                batch_ids: batchIds,
            });
            notify(res.message || 'Scenario published.', 'success');
            closeVtPublishModal();
            await loadScenarios();
        } catch (err) {
            notify(err?.message || 'Could not publish scenario.', 'error');
        }
    }

    async function publishScenario(scenarioId, scenarioTitle) {
        await openVtPublishModal(scenarioId, scenarioTitle);
    }

    function closeVtAudienceModal() {
        document.getElementById('vt-audience-modal')?.classList.add('hidden');
    }

    function buildVtAudienceAvailableList(ma, audience) {
        const activeIds = new Set((audience.active_trainees || []).map((t) => t.id));
        const availableMap = new Map();
        (audience.available_trainees || []).forEach((t) => {
            availableMap.set(t.id, t);
        });
        const allBatches = normalizeTrainerBatchesResponse(ma._allBatchesRes || []);
        allBatches.forEach((batch) => {
            const batchName = batch.batch_name || batch.name || 'batch';
            (batch.members || []).forEach((m) => {
                if (!m?.id || activeIds.has(m.id)) return;
                if (!availableMap.has(m.id)) {
                    availableMap.set(m.id, {
                        id: m.id,
                        email: m.email,
                        batch_name: batchName,
                        label: `${m.email} (${batchName})`,
                    });
                }
            });
        });
        (ma.excludedTraineeIds || []).forEach((tid) => {
            if (activeIds.has(tid) || availableMap.has(tid)) return;
            const fromActive = (audience.active_trainees || []).find((t) => t.id === tid);
            const fromAvail = (audience.available_trainees || []).find((t) => t.id === tid);
            const email = fromAvail?.email || fromActive?.email || tid;
            availableMap.set(tid, {
                id: tid,
                email,
                batch_name: fromAvail?.batch_name || null,
                label: `${email} — removed, click Add to restore`,
                readd: true,
            });
        });
        return Array.from(availableMap.values()).sort((a, b) => (a.email || '').localeCompare(b.email || ''));
    }

    function renderVtAudienceModal() {
        const ma = state.manageAudience;
        const audience = ma.audience || {};
        const batchesEl = document.getElementById('vt-audience-batches');
        const traineesEl = document.getElementById('vt-audience-trainees');
        const countEl = document.getElementById('vt-audience-count');
        const addSelect = document.getElementById('vt-audience-add-select');
        if (!batchesEl || !traineesEl || !addSelect) return;

        const allBatchesRes = normalizeTrainerBatchesResponse(ma._allBatchesRes || []);
        const selectedBatchIds = new Set(ma.batchIds || []);
        if (allBatchesRes.length) {
            batchesEl.innerHTML = allBatchesRes.map((batch) => {
                const checked = selectedBatchIds.has(batch.id) ? 'checked' : '';
                const name = escapeHtml(batch.batch_name || batch.name || 'Unnamed batch');
                const count = batch.member_count || batch.trainee_count || 0;
                return `<label class="batch-checkbox-item"><input type="checkbox" name="vt-audience-batch" value="${escapeHtml(batch.id)}" ${checked}><span>${name} <small>(${count} trainee${count === 1 ? '' : 's'})</small></span></label>`;
            }).join('');
        } else {
            batchesEl.innerHTML = '<p class="text-muted">No batches available.</p>';
        }

        const active = audience.active_trainees || [];
        if (countEl) countEl.textContent = `(${active.length})`;
        traineesEl.innerHTML = active.length
            ? active.map((t) => `
                <div class="vt-audience-trainee-row" data-trainee-id="${escapeHtml(t.id)}">
                    <div>
                        <strong>${escapeHtml(t.email)}</strong>
                        <small>${t.source === 'direct' ? 'Added directly' : `Via ${escapeHtml(t.batch_name || 'batch')}`}</small>
                    </div>
                    <button type="button" class="btn btn-danger btn-sm vt-audience-remove-trainee" data-trainee-id="${escapeHtml(t.id)}" data-source="${escapeHtml(t.source || 'batch')}" title="Remove trainee">
                        <i class="fas fa-user-minus"></i>
                    </button>
                </div>`).join('')
            : '<p class="placeholder-text">No trainees assigned yet. Select batches above or add trainees individually.</p>';

        const available = buildVtAudienceAvailableList(ma, audience);
        addSelect.innerHTML = '<option value="">Select a trainee to add...</option>'
            + (available.length
                ? available.map((t) => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.label || `${t.email} (${t.batch_name || 'Same process'})`)}</option>`).join('')
                : '<option value="" disabled>No more trainees available — add trainees to your batch or process first</option>');

        batchesEl.querySelectorAll('input[name="vt-audience-batch"]').forEach((cb) => {
            cb.addEventListener('change', () => {
                ma.batchIds = Array.from(document.querySelectorAll('input[name="vt-audience-batch"]:checked')).map((x) => x.value);
                recomputeVtAudiencePreview();
            });
        });
        traineesEl.querySelectorAll('.vt-audience-remove-trainee').forEach((btn) => {
            btn.addEventListener('click', () => removeVtAudienceTrainee(btn.dataset.traineeId, btn.dataset.source));
        });
    }

    function buildVtBatchMemberMap(batchesRes) {
        const batches = normalizeTrainerBatchesResponse(batchesRes);
        const map = {};
        batches.forEach((batch) => {
            map[batch.id] = {
                id: batch.id,
                name: batch.batch_name || batch.name || 'Unnamed batch',
                members: (batch.members || []).map((m) => ({
                    id: m.id,
                    email: m.email,
                })),
            };
        });
        return map;
    }

    function recomputeVtAudiencePreview() {
        const ma = state.manageAudience;
        const audience = ma.audience || {};
        const batchMap = ma._batchMemberMap || {};
        (audience.batches || []).forEach((b) => {
            if (!batchMap[b.id]) {
                batchMap[b.id] = {
                    id: b.id,
                    name: b.name,
                    members: b.members || [],
                };
            }
        });
        const activeMap = {};
        (ma.batchIds || []).forEach((batchId) => {
            const batch = batchMap[batchId];
            if (!batch) return;
            (batch.members || []).forEach((m) => {
                if (!ma.excludedTraineeIds.includes(m.id)) {
                    activeMap[m.id] = {
                        id: m.id,
                        email: m.email,
                        source: 'batch',
                        batch_id: batchId,
                        batch_name: batch.name,
                    };
                }
            });
        });
        (ma.traineeIds || []).forEach((tid) => {
            if (ma.excludedTraineeIds.includes(tid) || activeMap[tid]) return;
            const fromAvailable = (audience.available_trainees || []).find((t) => t.id === tid)
                || (audience.active_trainees || []).find((t) => t.id === tid);
            if (fromAvailable) {
                activeMap[tid] = {
                    id: tid,
                    email: fromAvailable.email,
                    source: 'direct',
                    batch_id: fromAvailable.batch_id || null,
                    batch_name: fromAvailable.batch_name || null,
                };
            }
        });
        ma.audience = {
            ...audience,
            active_trainees: Object.values(activeMap).sort((a, b) => a.email.localeCompare(b.email)),
            trainee_count: Object.keys(activeMap).length,
            available_trainees: buildVtAudienceAvailableList(ma, {
                ...audience,
                active_trainees: Object.values(activeMap),
            }),
        };
        renderVtAudienceModal();
    }

    function removeVtAudienceTrainee(traineeId, source) {
        const ma = state.manageAudience;
        if (!traineeId) return;
        if (source === 'direct') {
            ma.traineeIds = (ma.traineeIds || []).filter((id) => id !== traineeId);
        } else if (!ma.excludedTraineeIds.includes(traineeId)) {
            ma.excludedTraineeIds = [...(ma.excludedTraineeIds || []), traineeId];
        }
        recomputeVtAudiencePreview();
    }

    function addVtAudienceTrainee() {
        const ma = state.manageAudience;
        const select = document.getElementById('vt-audience-add-select');
        const traineeId = select?.value;
        if (!traineeId) {
            notify('Select a trainee to add.', 'warning');
            return;
        }
        ma.excludedTraineeIds = (ma.excludedTraineeIds || []).filter((id) => id !== traineeId);
        if (!ma.traineeIds.includes(traineeId)) {
            ma.traineeIds = [...(ma.traineeIds || []), traineeId];
        }
        if (select) select.value = '';
        recomputeVtAudiencePreview();
    }

    async function openVtAudienceModal(scenarioId, scenarioTitle) {
        if (!scenarioId || !isStaff()) return;
        const modal = document.getElementById('vt-audience-modal');
        if (!modal) return;
        document.getElementById('vt-audience-scenario-id').value = scenarioId;
        document.getElementById('vt-audience-scenario-name').textContent = scenarioTitle || 'Scenario';
        try {
            const [audience, batchesRes] = await Promise.all([
                vtApi(`/scenarios/${encodeURIComponent(scenarioId)}/publish-audience`),
                window.apiCall('/trainer/batches'),
            ]);
            const publish = state.scenarios.find((s) => s.id === scenarioId)?.publish || {};
            state.manageAudience = {
                scenarioId,
                scenarioTitle: scenarioTitle || '',
                batchIds: [...(audience.batches || []).map((b) => b.id)],
                traineeIds: [...(audience.direct_trainee_ids || [])],
                excludedTraineeIds: [...(audience.excluded_trainee_ids || [])],
                audience,
                _allBatchesRes: batchesRes,
                _batchMemberMap: buildVtBatchMemberMap(batchesRes),
            };
            if (!state.manageAudience.batchIds.length && publish.batch_ids?.length) {
                state.manageAudience.batchIds = [...publish.batch_ids];
            }
            renderVtAudienceModal();
            modal.classList.remove('hidden');
        } catch (err) {
            notify(err?.message || 'Could not load publish audience.', 'error');
        }
    }

    async function saveVtAudience() {
        const ma = state.manageAudience;
        if (!ma.scenarioId) return;
        ma.batchIds = Array.from(document.querySelectorAll('input[name="vt-audience-batch"]:checked')).map((x) => x.value);
        const directTrainees = ma.traineeIds || [];
        if (!ma.batchIds.length && !directTrainees.length) {
            if (!window.confirm(
                'Unpublish this scenario? All trainees will lose access and their practice/assessment progress for this scenario will be reset.',
            )) {
                return;
            }
        }
        try {
            const res = await vtApi(`/scenarios/${encodeURIComponent(ma.scenarioId)}/publish-audience`, 'PUT', {
                batch_ids: ma.batchIds,
                trainee_ids: ma.traineeIds || [],
                excluded_trainee_ids: ma.excludedTraineeIds || [],
            });
            notify(res.message || 'Publish audience updated.', 'success');
            closeVtAudienceModal();
            await loadScenarios();
        } catch (err) {
            notify(err?.message || 'Could not update publish audience.', 'error');
        }
    }

    async function loadScenarios() {
        const list = document.getElementById('vt-scenarios-list');
        if (!list) return;
        list.innerHTML = '<p class="placeholder-text"><i class="fas fa-spinner fa-spin"></i> Loading scenarios...</p>';
        try {
            const scenarios = isTrainee()
                ? await vtApi('/trainee/scenarios')
                : await vtApi(`/scenarios${state.showArchived ? '?archived_only=true' : ''}`);
            state.scenarios = Array.isArray(scenarios) ? scenarios : [];
            await loadDashboard();
            const visible = filteredScenarios();
            if (!visible.length) {
                list.innerHTML = `<div class="vt-empty-state">
                    <i class="fas fa-microphone-alt"></i>
                    <h3>${state.showArchived ? 'No archived scenarios' : (isTrainee() ? 'No assigned scenarios' : 'No scenarios yet')}</h3>
                    <p>${state.showArchived
                        ? 'Archived scenarios will appear here after you delete them from the main list.'
                        : (isTrainee()
                            ? 'Your trainer has not published any voice training scenarios to your batch yet.'
                            : 'Click <strong>AI Generate Scenarios</strong> to create training calls with Bedrock.')}</p>
                    ${state.showArchived || isTrainee() ? '' : '<button type="button" class="btn btn-primary" id="vt-empty-generate-btn"><i class="fas fa-wand-magic-sparkles"></i> AI Generate Scenarios</button>'}
                </div>`;
                document.getElementById('vt-empty-generate-btn')?.addEventListener('click', openGeneratorWizard);
                return;
            }
            list.innerHTML = visible.map(renderScenarioCard).join('');
            bindScenarioCardEvents(list);
            bindArchivedControls();
        } catch {
            list.innerHTML = '<p class="placeholder-text">Failed to load scenarios.</p>';
            notify('Failed to load voice training scenarios.', 'error');
        }
    }

    function bindScenarioCardEvents(root) {
        root.querySelectorAll('.vt-scenario-card').forEach((card) => {
            card.addEventListener('click', (e) => {
                if (e.target.closest('button, .vt-card-top-actions, .vt-card-tabs-wrap, .vt-card-footer')) return;
                if (card.classList.contains('archived')) return;
                openPreview(card.dataset.id);
            });
            card.addEventListener('keydown', (e) => {
                if (card.classList.contains('archived')) return;
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    openPreview(card.dataset.id);
                }
            });
        });
        root.querySelectorAll('.vt-preview-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                setActionButtonLoading(btn, true, 'Loading…');
                try {
                    await openPreview(btn.dataset.id, btn);
                } finally {
                    setActionButtonLoading(btn, false);
                }
            });
        });
        root.querySelectorAll('.vt-practice-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (btn.disabled) {
                    notify(
                        'Practice is disabled for trainees because practice sessions before assessment is set to 0.',
                        'warning',
                    );
                    return;
                }
                const scenario = state.scenarios.find((row) => row.id === btn.dataset.id);
                if (isPracticeDisabledForScenario(scenario)) {
                    notify(
                        'Practice is disabled for trainees because practice sessions before assessment is set to 0.',
                        'warning',
                    );
                    return;
                }
                setActionButtonLoading(btn, true, 'Starting…');
                try {
                    await startSession(btn.dataset.id, 'practice', btn);
                } finally {
                    setActionButtonLoading(btn, false);
                }
            });
        });
        root.querySelectorAll('.vt-assess-btn').forEach((btn) => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (btn.disabled) return;
                setActionButtonLoading(btn, true, 'Starting…');
                try {
                    await startSession(btn.dataset.id, 'assessment', btn);
                } finally {
                    setActionButtonLoading(btn, false);
                }
            });
        });
        root.querySelectorAll('.vt-publish-btn').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (btn.disabled) {
                    notify('Configure assessment before publishing.', 'warning');
                    openVtAssessmentConfigModal(btn.dataset.id, btn.dataset.title);
                    return;
                }
                publishScenario(btn.dataset.id, btn.dataset.title);
            });
        });
        root.querySelectorAll('.vt-config-assess-btn').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                openVtAssessmentConfigModal(btn.dataset.id, btn.dataset.title);
            });
        });
        root.querySelectorAll('.vt-manage-audience-btn').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                openVtAudienceModal(btn.dataset.id, btn.dataset.title);
            });
        });
        root.querySelectorAll('.vt-view-trainee-attempt').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                viewMyAssessment(btn.dataset.id);
            });
        });
        root.querySelectorAll('.vt-card-view-results').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                viewMyAssessment(btn.dataset.id);
            });
        });
        root.querySelectorAll('.vt-card-download-pdf').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                downloadAssessmentPdfById(btn.dataset.id);
            });
        });
        root.querySelectorAll('.vt-archive-btn').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                archiveScenario(btn.dataset.id);
            });
        });
        root.querySelectorAll('.vt-restore-btn').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                restoreScenario(btn.dataset.id);
            });
        });
        root.querySelectorAll('.vt-permanent-delete-btn').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                permanentDeleteScenario(btn.dataset.id, btn.dataset.title);
            });
        });
        root.querySelectorAll('.vt-card-tab').forEach((tab) => {
            tab.addEventListener('click', (e) => {
                e.stopPropagation();
                const wrap = tab.closest('.vt-card-tabs-wrap');
                if (!wrap) return;
                const tabName = tab.dataset.tab;
                wrap.querySelectorAll('.vt-card-tab').forEach((t) => {
                    const active = t.dataset.tab === tabName;
                    t.classList.toggle('active', active);
                    t.setAttribute('aria-selected', active ? 'true' : 'false');
                });
                wrap.querySelectorAll('.vt-card-tab-panel').forEach((panel) => {
                    const active = panel.dataset.panel === tabName;
                    panel.classList.toggle('active', active);
                    if (active) {
                        panel.removeAttribute('hidden');
                    } else {
                        panel.setAttribute('hidden', '');
                    }
                });
            });
        });
    }

    /* ── Preview modal ── */
    async function openPreview(scenarioId, triggerBtn = null) {
        setActionButtonLoading(triggerBtn, true, 'Loading…');
        try {
            if (!triggerBtn && typeof window.showLoader === 'function') {
                window.showLoader('Loading scenario...');
            }
            const detail = await vtApi(`/scenarios/${encodeURIComponent(scenarioId)}`);
            if (Array.isArray(detail.success_script)) {
                detail.success_script = repairScriptNameRoles(
                    detail.success_script,
                    detail.customer_persona,
                    detail.description
                );
            }
            state.previewScenario = detail;
            state.previewTurn = firstCustomerTurnIndex(coalesceScriptTurns(detail.success_script));
            state.previewScriptView = 'timeline';
            state.previewSopView = 'view';
            state.editScriptDraft = null;
            state.editSopDraft = null;
            state.scenarioId = scenarioId;
            const cat = categoryMeta(detail);
            const mood = (detail.opening_sentiment || 'neutral').toLowerCase();
            document.getElementById('vt-preview-category').innerHTML = `<i class="fas ${cat.icon}"></i> ${escapeHtml(cat.label)}`;
            document.getElementById('vt-preview-title').textContent = detail.title || '';
            const moodEl = document.getElementById('vt-preview-mood');
            if (moodEl) {
                moodEl.className = `vt-mood-badge ${mood}`;
                moodEl.textContent = mood;
            }
            switchPreviewTab('overview');
            updateScriptTabCount((detail.success_script || []).length);
            document.getElementById('vt-preview-modal')?.classList.remove('hidden');
            updatePreviewFooter();
        } catch {
            notify('Could not load scenario preview.', 'error');
        } finally {
            setActionButtonLoading(triggerBtn, false);
            if (!triggerBtn && typeof window.hideLoader === 'function') window.hideLoader();
        }
    }

    async function openPreviewForEdit(scenarioId, triggerBtn = null) {
        await openPreview(scenarioId, triggerBtn);
        switchPreviewTab('script');
        const script = coalesceScriptTurns(state.previewScenario?.success_script || []);
        state.previewScriptView = 'edit';
        state.editScriptDraft = JSON.parse(JSON.stringify(script));
        state.editFocusTurn = firstCustomerTurnIndex(script);
        renderPreviewScript();
    }

    async function openAdminScenarioPreview(scenarioId, editMode = false, triggerBtn = null) {
        setActionButtonLoading(triggerBtn, true, 'Loading…');
        try {
            if (typeof window.switchView === 'function') {
                await window.switchView('voice-training');
            }
            state.adminPreviewMode = true;
            if (editMode) await openPreviewForEdit(scenarioId, triggerBtn);
            else await openPreview(scenarioId, triggerBtn);
        } catch {
            notify('Could not open scenario preview.', 'error');
        } finally {
            setActionButtonLoading(triggerBtn, false);
        }
    }

    function closePreview(options = {}) {
        const keepForSession = Boolean(options.keepForSession);
        stopAllPlayback();
        document.getElementById('vt-preview-modal')?.classList.add('hidden');
        if (state._previewKeyHandler) {
            document.removeEventListener('keydown', state._previewKeyHandler);
            state._previewKeyHandler = null;
        }
        state.previewScenario = null;
        if (state.adminPreviewMode && !keepForSession) {
            returnToAdminVoiceReports();
        } else if (!keepForSession) {
            state.adminPreviewMode = false;
        }
    }

    function switchPreviewTab(tab) {
        document.querySelectorAll('.vt-preview-tab').forEach((t) => {
            t.classList.toggle('active', t.dataset.tab === tab);
        });
        document.getElementById('vt-preview-panel-overview')?.classList.toggle('hidden', tab !== 'overview');
        document.getElementById('vt-preview-panel-script')?.classList.toggle('hidden', tab !== 'script');
        document.getElementById('vt-preview-panel-sop')?.classList.toggle('hidden', tab !== 'sop');
        renderPreviewPanel(tab);
    }

    function renderPreviewPanel(tab) {
        const s = state.previewScenario;
        if (!s) return;
        if (tab === 'overview') {
            const turns = (s.success_script || []).length;
            const sop = (s.sop_activities || []).length;
            const mood = (s.opening_sentiment || 'neutral').toLowerCase();
            const script = coalesceScriptTurns(s.success_script || []);
            const facts = extractFactsFromText(script, s.description, s.customer_persona);
            const summaryTurn = script.find((t) => isSummaryTurn(t.text));
            const closingTurn = summaryTurn?.text || script.slice(-3).map((t) => t.text).join(' ');
            document.getElementById('vt-preview-panel-overview').innerHTML = `
                <p class="vt-preview-desc">${escapeHtml(s.description || '')}</p>
                ${formatFactsChips(facts)}
                <div class="vt-preview-stat-row">
                    <div class="vt-preview-stat"><i class="fas fa-comments"></i><strong>${turns}</strong><span>Turns</span></div>
                    <div class="vt-preview-stat"><i class="fas fa-list-check"></i><strong>${sop}</strong><span>SOP steps</span></div>
                    <div class="vt-preview-stat mood"><i class="fas fa-face-meh"></i><strong class="vt-mood-text ${mood}">${mood}</strong><span>Opening mood</span></div>
                </div>
                <div class="vt-persona-box vt-preview-persona">
                    <span class="vt-persona-label">Customer persona</span>
                    <p><em>${escapeHtml(s.customer_persona || '')}</em></p>
                </div>
                <div class="vt-preview-flow-card">
                    <h4><i class="fas fa-route"></i> How this call flows</h4>
                    <ol class="vt-preview-flow-steps">
                        <li><strong>Practice:</strong> You greet first → customer speaks (TTS) → suggested agent line below.</li>
                        <li><strong>Assessment:</strong> Same call flow — you greet first, but <strong>no hints</strong>; respond in your own words.</li>
                        <li><strong>Close:</strong> Agent gives a concrete summary → customer thanks → goodbye.</li>
                        <li><strong>Consistency:</strong> Names, card digits, and amounts above stay the same in practice &amp; assessment.</li>
                    </ol>
                </div>
                ${closingTurn ? `<div class="vt-preview-closing"><i class="fas fa-flag-checkered"></i> <span>Script closes with:</span> <em>"${escapeHtml(closingTurn.slice(0, 120))}${closingTurn.length > 120 ? '…' : ''}"</em></div>` : ''}
                <div class="vt-preview-quick-nav">
                    <button type="button" class="btn btn-secondary btn-sm vt-preview-goto-tab" data-tab="script"><i class="fas fa-comments"></i> Read full script</button>
                    <button type="button" class="btn btn-secondary btn-sm vt-preview-goto-tab" data-tab="sop"><i class="fas fa-list-check"></i> View SOP steps</button>
                </div>
            `;
            document.querySelectorAll('.vt-preview-goto-tab').forEach((btn) => {
                btn.addEventListener('click', () => switchPreviewTab(btn.dataset.tab));
            });
        } else if (tab === 'script') {
            renderPreviewScript();
        } else if (tab === 'sop') {
            renderPreviewSop();
        }
    }

    function renderPreviewSop() {
        const s = state.previewScenario;
        const steps = s?.sop_activities || [];
        const panel = document.getElementById('vt-preview-panel-sop');
        if (!panel) return;
        if (!steps.length) {
            panel.innerHTML = '<p class="placeholder-text">No SOP steps defined.</p>';
            return;
        }

        const view = state.previewSopView || 'view';
        if (view === 'edit') {
            renderSopEdit(panel);
            return;
        }

        const script = coalesceScriptTurns(s.success_script || []);
        const cues = s.sop_script_cues || [];
        const firstCustomer = firstCustomerTurnIndex(script);
        panel.innerHTML = `
            <div class="vt-sop-preview-interactive">
            <div class="vt-script-mode-bar">
                <button type="button" class="vt-script-mode-btn active" data-sop-view="view"><i class="fas fa-list-check"></i> View steps</button>
                ${isStaff() ? '<button type="button" class="vt-script-mode-btn" data-sop-view="edit"><i class="fas fa-pen"></i> Edit SOP</button>' : ''}
            </div>
            <div class="vt-sop-preview-intro">
                <i class="fas fa-list-check"></i>
                <div>
                    <strong>${steps.length} steps to complete</strong>
                    <p>Each step maps to an agent line in the script timeline. During practice and assessment, the left checklist tracks live progress.</p>
                </div>
            </div>
            <div class="vt-sop-preview-steps">
                ${steps.map((st, i) => {
                    const cue = cues.find((c) => c.sop_id === st.id) || cues[i];
                    const snippet = cue?.script_line
                        ? `"${escapeHtml(cue.script_line.slice(0, 72))}${cue.script_line.length > 72 ? '…' : ''}"`
                        : '';
                    return `
                    <button type="button" class="vt-sop-step-card" data-step="${i}" aria-expanded="false">
                        <span class="vt-sop-step-num">${i + 1}</span>
                        <div class="vt-sop-step-body">
                            <strong>${escapeHtml(st.name)}</strong>
                            <p class="vt-sop-step-desc">${escapeHtml(st.description || '')}</p>
                            ${snippet ? `<p class="vt-sop-step-script"><i class="fas fa-link"></i> Script cue: ${snippet}</p>` : ''}
                        </div>
                        <i class="fas fa-chevron-down vt-sop-step-chevron" aria-hidden="true"></i>
                    </button>`;
                }).join('')}
            </div>
            ${firstCustomer >= 0 ? `<p class="vt-sop-preview-foot"><i class="fas fa-microphone"></i> Practice flow: <strong>You greet first</strong> → customer replies by voice (turn #${firstCustomer + 1}) → you respond using the suggested line.</p>` : ''}
            </div>`;
        panel.querySelectorAll('[data-sop-view]').forEach((btn) => {
            btn.addEventListener('click', () => {
                if (btn.dataset.sopView === 'edit') {
                    state.editSopDraft = JSON.parse(JSON.stringify(steps));
                    state.previewSopView = 'edit';
                    renderPreviewSop();
                }
            });
        });
        panel.querySelectorAll('.vt-sop-step-card').forEach((card) => {
            card.addEventListener('click', () => {
                const open = card.classList.toggle('open');
                card.setAttribute('aria-expanded', open ? 'true' : 'false');
                panel.querySelectorAll('.vt-sop-step-card').forEach((other) => {
                    if (other !== card) {
                        other.classList.remove('open');
                        other.setAttribute('aria-expanded', 'false');
                    }
                });
            });
        });
    }

    function syncEditSopFromDom() {
        const draft = state.editSopDraft;
        if (!draft) return;
        document.querySelectorAll('.vt-edit-sop-name').forEach((input) => {
            const i = Number(input.dataset.idx);
            if (draft[i]) draft[i].name = input.value.trim();
        });
        document.querySelectorAll('.vt-edit-sop-desc').forEach((ta) => {
            const i = Number(ta.dataset.idx);
            if (draft[i]) draft[i].description = ta.value.trim();
        });
    }

    function renderSopEdit(panel) {
        const draft = state.editSopDraft || state.previewScenario?.sop_activities || [];
        panel.innerHTML = `
            <div class="vt-sop-edit-layout">
            <div class="vt-script-mode-bar">
                <button type="button" class="vt-script-mode-btn" data-sop-view="view"><i class="fas fa-list-check"></i> View steps</button>
                <button type="button" class="vt-script-mode-btn active" data-sop-view="edit"><i class="fas fa-pen"></i> Edit SOP</button>
            </div>
            <div class="vt-script-studio-bar">
                <span><i class="fas fa-list-check"></i> SOP Editor · ${draft.length} steps</span>
                <div class="vt-studio-bar-actions">
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-sop-edit-cancel">Cancel</button>
                    <button type="button" class="btn btn-primary btn-sm" id="vt-sop-edit-save">Save changes</button>
                </div>
            </div>
            <p class="vt-script-hint">Edit step names and descriptions. These drive the practice/assessment checklist.</p>
            <div class="vt-sop-edit-steps">
                ${draft.map((st, i) => `
                    <div class="vt-sop-edit-card" data-idx="${i}">
                        <div class="vt-sop-edit-card-head">
                            <span class="vt-sop-step-num">${i + 1}</span>
                            <div class="vt-studio-actions">
                                <button type="button" class="vt-studio-icon-btn" data-sop-action="up" title="Move up" ${i === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                                <button type="button" class="vt-studio-icon-btn" data-sop-action="down" title="Move down" ${i >= draft.length - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                                <button type="button" class="vt-studio-icon-btn" data-sop-action="add" title="Insert below"><i class="fas fa-plus"></i></button>
                                <button type="button" class="vt-studio-icon-btn danger" data-sop-action="del" title="Delete" ${draft.length <= 1 ? 'disabled' : ''}><i class="fas fa-trash"></i></button>
                            </div>
                        </div>
                        <label class="vt-sop-edit-label">Step name</label>
                        <input type="text" class="form-control vt-edit-sop-name" data-idx="${i}" value="${escapeHtml(st.name || '')}" placeholder="e.g. Verify Identity" />
                        <label class="vt-sop-edit-label">Description</label>
                        <textarea class="form-control vt-edit-sop-desc" data-idx="${i}" rows="2" placeholder="What the agent should do in this step…">${escapeHtml(st.description || '')}</textarea>
                    </div>
                `).join('')}
            </div>
            <div class="vt-studio-footer">
                <button type="button" class="btn btn-secondary btn-sm" id="vt-sop-edit-add-end"><i class="fas fa-plus"></i> Add step at end</button>
            </div>
            </div>
        `;

        panel.querySelector('[data-sop-view="view"]')?.addEventListener('click', () => {
            state.editSopDraft = null;
            state.previewSopView = 'view';
            renderPreviewSop();
        });

        document.getElementById('vt-sop-edit-cancel')?.addEventListener('click', () => {
            state.editSopDraft = null;
            state.previewSopView = 'view';
            renderPreviewSop();
        });
        document.getElementById('vt-sop-edit-save')?.addEventListener('click', saveEditedSop);

        const applySopMutation = (fn) => {
            syncEditSopFromDom();
            const d = state.editSopDraft;
            if (!d) return;
            fn(d);
            renderPreviewSop();
        };

        panel.querySelectorAll('[data-sop-action]').forEach((btn) => {
            btn.addEventListener('click', () => {
                const row = btn.closest('.vt-sop-edit-card');
                if (!row) return;
                const idx = Number(row.dataset.idx);
                const action = btn.dataset.sopAction;
                if (action === 'up' && idx > 0) {
                    applySopMutation((d) => { [d[idx - 1], d[idx]] = [d[idx], d[idx - 1]]; });
                } else if (action === 'down' && idx < (state.editSopDraft?.length || 0) - 1) {
                    applySopMutation((d) => { [d[idx], d[idx + 1]] = [d[idx + 1], d[idx]]; });
                } else if (action === 'add') {
                    applySopMutation((d) => {
                        const newId = `sop-${Date.now()}-${idx}`;
                        d.splice(idx + 1, 0, { id: newId, name: '', description: '', required: true, completed: false });
                    });
                } else if (action === 'del' && (state.editSopDraft?.length || 0) > 1) {
                    applySopMutation((d) => { d.splice(idx, 1); });
                }
            });
        });

        document.getElementById('vt-sop-edit-add-end')?.addEventListener('click', () => {
            applySopMutation((d) => {
                d.push({
                    id: `sop-${Date.now()}-end`,
                    name: '',
                    description: '',
                    required: true,
                    completed: false,
                });
            });
        });
    }

    async function saveEditedSop() {
        const draft = state.editSopDraft;
        if (!draft || !state.scenarioId) return;
        syncEditSopFromDom();
        const cleaned = draft.map((st, i) => ({
            id: st.id || `sop-${i + 1}`,
            name: (st.name || '').trim() || `Step ${i + 1}`,
            description: (st.description || '').trim(),
            required: st.required !== false,
            completed: false,
        }));
        if (cleaned.some((st) => !st.name)) {
            notify('Every SOP step needs a name.', 'warning');
            return;
        }
        try {
            if (typeof window.showLoader === 'function') window.showLoader('Saving SOP steps...');
            const updated = await vtApi(`/scenarios/${encodeURIComponent(state.scenarioId)}`, 'PUT', {
                sop_activities: cleaned,
            });
            state.previewScenario = updated;
            state.editSopDraft = null;
            state.previewSopView = 'view';
            notify(isSuperAdmin() ? 'SOP saved. The scenario owner has been notified.' : 'SOP steps saved.', 'success');
            await loadScenarios();
            renderPreviewSop();
        } catch {
            notify('Could not save SOP steps.', 'error');
        } finally {
            if (typeof window.hideLoader === 'function') window.hideLoader();
        }
    }

    function coalesceScriptTurns(script) {
        const result = [];
        (script || []).forEach((turn) => {
            const text = (turn.text || '').trim();
            if (!text) return;
            if (result.length && result[result.length - 1].speaker === turn.speaker) {
                result[result.length - 1].text = `${result[result.length - 1].text} ${text}`.trim();
            } else {
                result.push({ speaker: turn.speaker, text });
            }
        });
        return result;
    }

    const INVALID_CUSTOMER_NAME_WORDS = new Set([
        'from', 'the', 'a', 'an', 'your', 'our', 'this', 'that', 'sure', 'yes',
        'okay', 'alright', 'please', 'well', 'oh', 'um', 'so', 'and', 'for',
        'quite', 'very', 'really', 'having', 'calling', 'wondering', 'experiencing',
        'not', 'here', 'there', 'still', 'already', 'also', 'just', 'about', 'because',
        'urgent', 'critical', 'priority', 'important', 'immediate', 'asap', 'severe',
        'frustrated', 'angry', 'impatient', 'concerned', 'upset', 'annoyed',
        'disappointed', 'confused', 'worried', 'stressed', 'escalated',
        'ridiculous', 'unacceptable', 'absurd', 'crazy', 'insane', 'horrible',
        'terrible', 'awful', 'outrageous', 'unbelievable', 'frustrating',
        'annoying', 'pathetic', 'ludicrous', 'nonsense', 'enough', 'finally',
        'irate', 'livid', 'furious', 'fuming', 'hostile', 'desperate', 'distraught',
        'unhappy', 'dissatisfied', 'unsatisfied', 'displeased', 'exasperated',
        'indignant', 'aggravated', 'irritated', 'agitated', 'anxious', 'panicked',
        'overwhelmed', 'inconvenienced', 'mad', 'tense', 'neutral', 'calm', 'polite',
        'satisfied', 'pleased', 'fed',
        'great', 'good', 'fine', 'sorry', 'happy', 'glad', 'able', 'unable',
        'trying', 'looking', 'getting', 'going', 'being', 'doing',
        'installing', 'updating', 'restarting', 'resetting', 'connecting', 'checking',
        'waiting', 'holding', 'done', 'forgot', 'reconnected',
    ]);

    function isPlausibleCustomerName(name) {
        const first = String(name || '').trim().split(/\s+/)[0] || '';
        if (!first || first.length <= 2) return false;
        const lower = first.toLowerCase();
        if (INVALID_CUSTOMER_NAME_WORDS.has(lower)) return false;
        // Adjective-like endings are almost never first names in support scripts
        if (/(?:ing|ed|ly|ous|ible|ical|ful|less|ish|ive)$/i.test(first)) return false;
        return true;
    }

    function parsePersonaCustomerName(persona) {
        const head = String(persona || '').trim().split(',')[0].trim();
        if (!head) return null;
        let cleaned = head;
        if (/^an?\s+/i.test(cleaned)) cleaned = cleaned.replace(/^an?\s+/i, '');
        const fromIdx = cleaned.toLowerCase().indexOf(' from ');
        if (fromIdx > 0) cleaned = cleaned.slice(0, fromIdx).trim();
        const words = cleaned.split(/\s+/).filter(Boolean);
        if (words.length >= 2 && /^[A-Z]/.test(words[0]) && /^[A-Z]/.test(words[1])) {
            return isPlausibleCustomerName(words[0]) ? words[0] : null;
        }
        if (words.length === 1 && /^[A-Z]/.test(words[0]) && isPlausibleCustomerName(words[0])) {
            return words[0];
        }
        return null;
    }

    function pickMostCommonName(names) {
        if (!names.length) return null;
        const counts = {};
        names.forEach((n) => { counts[n] = (counts[n] || 0) + 1; });
        return Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
    }

    function extractCustomerSelfNameFromScript(script) {
        const selfNames = [];
        (script || []).forEach((turn) => {
            if (turn.speaker !== 'customer') return;
            const text = turn.text || '';
            // Prefer "I'm / my name is"; "this is" must not treat "this is ridiculous!" as a name
            let m = text.match(
                /(?:my name is|i'm|i am)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)(?=\s*(?:,|\.|!|$|and\b|from\b|with\b|calling\b|here\b|at\b))/i
            );
            if (!m) {
                m = text.match(
                    /\bthis is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)(?=\s*(?:,|\.|$|and\b|from\b|with\b|calling\b|here\b|at\b))/i
                );
            }
            if (m && isPlausibleCustomerName(m[1])) {
                const captured = m[1].trim();
                selfNames.push(captured.includes(' ') ? captured.split(/\s+/)[0] : captured);
            }
        });
        return pickMostCommonName(selfNames);
    }

    function extractCustomerAddressedNameFromScript(script) {
        const addressed = [];
        (script || []).forEach((turn) => {
            if (turn.speaker !== 'trainee') return;
            const text = turn.text || '';
            const re = /(?:thank you|thanks|understand|sorry|hi|hello|dear),?\s+([A-Z][a-z]+)\b/g;
            let m;
            while ((m = re.exec(text)) !== null) {
                if (isPlausibleCustomerName(m[1])) addressed.push(m[1]);
            }
        });
        return pickMostCommonName(addressed);
    }

    function extractCustomerNameFromScript(script) {
        return extractCustomerSelfNameFromScript(script) || extractCustomerAddressedNameFromScript(script);
    }

    function extractAgentNameFromScript(script) {
        const customerFirst = (extractCustomerSelfNameFromScript(script) || '').toLowerCase();
        for (const turn of script || []) {
            if (turn.speaker !== 'trainee') continue;
            const text = turn.text || '';
            const m = text.match(
                /(?:my name is|i'?m|i am|this is)\s+([A-Z][a-z]+)(?:\s+(?:from|with|at|speaking)\b|,|\.|!|\s|$)/i
            );
            if (!m) continue;
            const name = m[1];
            if (!isPlausibleCustomerName(name)) continue;
            if (customerFirst && name.toLowerCase() === customerFirst) continue;
            return name;
        }
        return null;
    }

    // Keep in step with MOOD_AND_SEVERITY_WORDS in utils/voice_training/script_turns.py: a mood
    // word missing from the list is read as the caller's name, here and on the server.
    const FALSE_CUSTOMER_NAME_TOKENS_JS = Object.freeze([
        'urgent', 'critical', 'priority', 'important', 'immediate', 'asap', 'severe',
        'frustrated', 'angry', 'impatient', 'concerned', 'upset', 'annoyed',
        'disappointed', 'confused', 'worried', 'stressed', 'escalated',
        'ridiculous', 'unacceptable', 'absurd', 'crazy', 'insane', 'horrible',
        'terrible', 'awful', 'outrageous', 'unbelievable', 'frustrating',
        'annoying', 'pathetic', 'ludicrous', 'nonsense', 'enough', 'finally',
        'irate', 'livid', 'furious', 'fuming', 'hostile', 'desperate', 'distraught',
        'unhappy', 'dissatisfied', 'unsatisfied', 'displeased', 'exasperated',
        'indignant', 'aggravated', 'irritated', 'agitated', 'anxious', 'panicked',
        'overwhelmed', 'inconvenienced', 'mad', 'tense', 'neutral', 'calm', 'polite',
        'satisfied', 'pleased', 'fed',
    ]);

    function repairScriptNameRoles(script, persona, description) {
        /** Fix false name tokens + customer thanking themselves (text-only, no HTML). */
        const turns = (script || []).map((t) => ({
            speaker: t.speaker,
            text: String(t.text || ''),
        }));
        if (!turns.length) return turns;
        // Persona first (same as server resolve_known_customer_name) so the scenario card name sticks.
        const customerName = parsePersonaCustomerName(persona || '')
            || parsePersonaCustomerName(description || '')
            || extractCustomerSelfNameFromScript(turns);
        const agentName = extractAgentNameFromScript(turns);
        // Only allow plain alphabetic first names into regex / replacements (code-scan safe)
        const custRaw = (customerName || '').split(/\s+/)[0] || '';
        const cust = /^[A-Za-z]{2,30}$/.test(custRaw) && isPlausibleCustomerName(custRaw)
            ? custRaw
            : '';
        const agentSafe = agentName && /^[A-Za-z]{2,30}$/.test(agentName)
            ? agentName
            : '';
        const custEsc = cust ? escapeRegExp(cust) : '';
        const custRe = custEsc ? new RegExp(`\\b${custEsc}\\b`, 'i') : null;
        const agentRep = agentSafe ? escapeReplace(agentSafe) : '';
        const custRep = cust ? escapeReplace(cust) : '';

        return turns.map((turn) => {
            let text = turn.text || '';
            if (turn.speaker === 'trainee') {
                FALSE_CUSTOMER_NAME_TOKENS_JS.forEach((bad) => {
                    const badEsc = escapeRegExp(bad);
                    if (cust) {
                        text = text
                            .replace(new RegExp(`(\\bto summarize),?\\s+${badEsc}\\s*:`, 'ig'), `$1, ${custRep}:`)
                            .replace(new RegExp(`(,\\s*)${badEsc}(\\s*[:.!?]|\\s*$)`, 'ig'), `$1${custRep}$2`);
                    } else {
                        text = text
                            .replace(new RegExp(`(\\bto summarize),?\\s+${badEsc}\\s*:`, 'ig'), '$1:')
                            .replace(new RegExp(`,\\s*${badEsc}(\\s*[:.!?]|\\s*$)`, 'ig'), '$1');
                    }
                });
                if (cust) {
                    // Unify other person names in address slots to the scenario customer name.
                    text = text.replace(
                        /(,\s*)([A-Z][a-z]{1,29})(\s*[:.!]|\s*$)/g,
                        (full, pre, name, post) => {
                            const low = String(name).toLowerCase();
                            if (low === cust.toLowerCase()) return full;
                            if (agentSafe && low === agentSafe.toLowerCase()) return full;
                            if (!isPlausibleCustomerName(name) && !FALSE_CUSTOMER_NAME_TOKENS_JS.includes(low)) {
                                return full;
                            }
                            return `${pre}${custRep}${post}`;
                        },
                    );
                    // No full-pattern `i` flag: [A-Z][a-z] must stay case-sensitive.
                    text = text.replace(
                        /(\b[Tt]o [Ss]ummarize),?\s+([A-Z][a-z]{1,29})(\s*:)/g,
                        (full, pre, name, post) => {
                            const low = String(name).toLowerCase();
                            if (low === cust.toLowerCase()) return full;
                            if (agentSafe && low === agentSafe.toLowerCase()) return full;
                            if (!isPlausibleCustomerName(name) && !FALSE_CUSTOMER_NAME_TOKENS_JS.includes(low)) {
                                return full;
                            }
                            return `To summarize, ${custRep}${post}`;
                        },
                    );
                }
                if (custEsc) {
                    const thanksPat = new RegExp(
                        `\\b(?:thank you|thanks)\\b[^.!?]*\\b${custEsc}\\b`,
                        'ig',
                    );
                    const hits = text.match(thanksPat) || [];
                    if (hits.length >= 2) {
                        text = text
                            .replace(new RegExp(`\\bThank you,\\s*${custEsc}\\.\\s*`, 'i'), '')
                            .replace(new RegExp(`\\bThanks,\\s*${custEsc}\\.\\s*`, 'i'), '');
                    }
                }
            }
            if (turn.speaker === 'customer' && cust && custRe && custRe.test(text)) {
                if (agentSafe) {
                    text = text
                        .replace(new RegExp(`\\b(that sounds great),?\\s+${custEsc}\\b`, 'ig'), `$1, ${agentRep}`)
                        .replace(new RegExp(`\\b(thank you(?: so much)?),?\\s+${custEsc}\\b`, 'ig'), `$1, ${agentRep}`)
                        .replace(new RegExp(`\\b(thanks),?\\s+${custEsc}\\b`, 'ig'), `$1, ${agentRep}`);
                } else {
                    text = text
                        .replace(new RegExp(`\\b(that sounds great),?\\s+${custEsc}\\b`, 'ig'), '$1')
                        .replace(new RegExp(`\\b(thank you(?: so much)?),?\\s+${custEsc}\\b`, 'ig'), '$1')
                        .replace(new RegExp(`\\b(thanks),?\\s+${custEsc}\\b`, 'ig'), '$1');
                }
            }
            return { speaker: turn.speaker, text: text.replace(/\s{2,}/g, ' ').trim() };
        });
    }

    function extractFactsFromText(...parts) {
        const script = parts.length && Array.isArray(parts[0]) ? parts[0] : null;
        const rest = script ? parts.slice(1) : parts;
        const blob = script
            ? script.map((t) => t.text).join(' ')
            : rest.filter(Boolean).join(' ');
        const facts = {};
        // All scenario types: self-intro → persona/description → agent address
        let name = script ? extractCustomerSelfNameFromScript(script) : null;
        if (!name) {
            for (const part of rest) {
                const personaName = parsePersonaCustomerName(part);
                if (personaName) {
                    name = personaName;
                    break;
                }
            }
        }
        if (!name && script) name = extractCustomerAddressedNameFromScript(script);
        if (name) facts.customer_name = name;
        const card = blob.match(/(?:card|ending in|last four|last 4)[^\d]*(\d{4})/i);
        if (card) {
            const idx = blob.indexOf(card[1]);
            const window = blob.slice(Math.max(0, idx - 60), idx + card[1].length + 30).toLowerCase();
            const birthContext = /born|birth|dob|date of birth|birthday/.test(window)
                || /(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{4}/i.test(window);
            if (!birthContext) facts.card_last4 = card[1];
        }
        // Bounded repetition, not open-ended `+` (Sonar javascript:S2631). The unbounded
        // form was measurably quadratic: on a long run of word characters with no matching
        // '@' the engine retried every start offset, costing ~4.8s at 40k characters. Real
        // local parts and labels are well inside these caps, so nothing extractable is
        // lost, and the scan cost is now linear in the length of the transcript.
        const email = blob.match(/[\w.+-]{1,64}@[\w-]{1,63}\.[a-z]{2,24}/i);
        if (email) facts.email = email[0];
        const amount = blob.match(/\$[\d,]+(?:\.\d{2})?/);
        if (amount) facts.amount = amount[0];
        if (!name) {
            const nm = blob.match(
                /(?:this is|my name is|i'm|i am)\s+([A-Z][a-z]+)(?=\s*(?:,|\.|!|$|and\b|from\b|with\b|calling\b|here\b|at\b))/i
            );
            if (nm && isPlausibleCustomerName(nm[1])) facts.customer_name = nm[1];
        }
        return facts;
    }

    function formatFactsChips(facts) {
        if (!facts || !Object.keys(facts).length) return '';
        const chips = [];
        if (facts.customer_name) chips.push(`<span class="vt-fact-chip"><i class="fas fa-user"></i> ${escapeHtml(facts.customer_name)}</span>`);
        if (facts.card_last4) chips.push(`<span class="vt-fact-chip"><i class="fas fa-credit-card"></i> Card •••• ${escapeHtml(facts.card_last4)}</span>`);
        if (facts.email) chips.push(`<span class="vt-fact-chip"><i class="fas fa-envelope"></i> ${escapeHtml(facts.email)}</span>`);
        if (facts.amount) chips.push(`<span class="vt-fact-chip"><i class="fas fa-dollar-sign"></i> ${escapeHtml(facts.amount)}</span>`);
        if (!chips.length) return '';
        return `<div class="vt-scenario-facts" title="These details stay consistent in practice and assessment">${chips.join('')}</div>`;
    }

    function isSummaryTurn(text) {
        const l = (text || '').toLowerCase();
        return l.includes('to summarize') || l.includes('recap') || (l.includes('escalated') && l.includes('technician'));
    }

    function isClosingTurn(text) {
        const l = (text || '').toLowerCase();
        return isSummaryTurn(text) || ['anything else', 'great day', 'thank you for calling', "that's all"].some((w) => l.includes(w));
    }

    function isDanglingTurn(text) {
        const t = (text || '').trim();
        return /\b(with|the|our|your|a|an|to|for|and|or|in|on|at)\s*\.?\s*$/i.test(t);
    }

    function scrollActiveTimelineBubble(container) {
        const active = container?.querySelector('.vt-chat-bubble.active, .vt-timeline-bubble.active');
        if (active) active.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    function renderPreviewScript() {
        const s = state.previewScenario;
        const raw = s?.success_script || [];
        const script = coalesceScriptTurns(raw);
        const panel = document.getElementById('vt-preview-panel-script');
        if (!panel) return;
        if (!script.length) {
            panel.innerHTML = '<p class="placeholder-text">No script available.</p>';
            return;
        }

        const idx = Math.max(0, Math.min(state.previewTurn, script.length - 1));
        const view = state.previewScriptView;

        panel.innerHTML = `
            <div class="vt-script-shell">
            <div class="vt-script-mode-bar">
                <button type="button" class="vt-script-mode-btn ${view === 'timeline' ? 'active' : ''}" data-view="timeline"><i class="fas fa-stream"></i> Timeline</button>
                ${isStaff() ? `<button type="button" class="vt-script-mode-btn ${view === 'edit' ? 'active' : ''}" data-view="edit"><i class="fas fa-pen"></i> Edit script</button>` : ''}
                ${view !== 'edit' ? `
                <div class="vt-script-nav">
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-script-prev" ${idx === 0 ? 'disabled' : ''}><i class="fas fa-chevron-left"></i></button>
                    <span class="vt-script-counter">${idx + 1} / ${script.length}</span>
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-script-next" ${idx >= script.length - 1 ? 'disabled' : ''}><i class="fas fa-chevron-right"></i></button>
                </div>` : ''}
            </div>
            ${view !== 'edit' ? `<input type="range" id="vt-script-slider" class="vt-script-slider" min="0" max="${script.length - 1}" value="${idx}" aria-label="Script turn" />` : ''}
            <div id="vt-script-content-area" class="vt-script-content-area"></div>
            </div>
        `;

                panel.querySelectorAll('.vt-script-mode-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                if (isTrainee()) return;
                const next = btn.dataset.view;
                if (next === 'edit' && !state.editScriptDraft) {
                    state.editScriptDraft = JSON.parse(JSON.stringify(script));
                    state.editFocusTurn = firstCustomerTurnIndex(script);
                }
                if (next !== 'edit') state.editScriptDraft = null;
                state.previewScriptView = next === 'edit' ? 'edit' : 'timeline';
                renderPreviewScript();
            });
        });

        const content = document.getElementById('vt-script-content-area');
        const scriptView = view === 'edit' ? 'edit' : 'timeline';
        if (scriptView === 'timeline') renderScriptTimeline(content, script, idx);
        else renderScriptEdit(content);

        if (view !== 'edit') {
            document.getElementById('vt-script-prev')?.addEventListener('click', () => {
                state.previewTurn = Math.max(0, idx - 1);
                renderPreviewScript();
            });
            document.getElementById('vt-script-next')?.addEventListener('click', () => {
                state.previewTurn = Math.min(script.length - 1, idx + 1);
                renderPreviewScript();
            });
            document.getElementById('vt-script-slider')?.addEventListener('input', (e) => {
                state.previewTurn = Number(e.target.value);
                renderPreviewScript();
            });
            const onPreviewKey = (e) => {
                if (!document.getElementById('vt-preview-modal')?.classList.contains('hidden')
                    && state.previewScriptView !== 'edit'
                    && document.getElementById('vt-preview-panel-script')?.classList.contains('hidden') === false) {
                    if (e.key === 'ArrowLeft') {
                        e.preventDefault();
                        state.previewTurn = Math.max(0, state.previewTurn - 1);
                        renderPreviewScript();
                    } else if (e.key === 'ArrowRight') {
                        e.preventDefault();
                        state.previewTurn = Math.min(script.length - 1, state.previewTurn + 1);
                        renderPreviewScript();
                    }
                }
            };
            document.removeEventListener('keydown', state._previewKeyHandler || (() => {}));
            state._previewKeyHandler = onPreviewKey;
            document.addEventListener('keydown', onPreviewKey);
        }
    }

    function renderScriptTimeline(container, script, idx) {
        container.innerHTML = `
            <p class="vt-script-hint">Customer ← left · Agent → right · Click a bubble · ← → to navigate</p>
            <div class="vt-chat-timeline" id="vt-chat-timeline-scroll">
                ${script.map((t, i) => `
                    <div class="vt-chat-row ${t.speaker === 'customer' ? 'left' : 'right'}">
                        <button type="button" class="vt-chat-bubble ${t.speaker} ${i === idx ? 'active' : ''} ${isClosingTurn(t.text) ? 'closing' : ''} ${isSummaryTurn(t.text) ? 'summary' : ''} ${isDanglingTurn(t.text) ? 'dangling' : ''}" data-turn="${i}">
                            <span class="vt-chat-num">#${i + 1}</span>
                            <span class="vt-chat-role">${t.speaker === 'customer' ? 'Customer' : 'Agent (reference)'}</span>
                            ${isSummaryTurn(t.text) ? '<span class="vt-chat-tag summary">Summary</span>' : ''}
                            <p>${escapeHtml(t.text)}</p>
                        </button>
                    </div>
                `).join('')}
            </div>
        `;
        container.querySelectorAll('.vt-chat-bubble').forEach((b) => {
            b.addEventListener('click', () => { state.previewTurn = Number(b.dataset.turn); renderPreviewScript(); });
        });
        scrollActiveTimelineBubble(document.getElementById('vt-chat-timeline-scroll'));
    }

    function syncEditDraftFromDom(root) {
        const draft = state.editScriptDraft;
        if (!draft) return;
        const scope = root || document.getElementById('vt-script-content-area') || document;
        scope.querySelectorAll('.vt-edit-text').forEach((ta) => {
            const i = Number(ta.dataset.idx);
            if (draft[i]) draft[i].text = ta.value;
        });
    }

    function sanitizeScriptDraft(draft) {
        return (draft || [])
            .map((turn) => {
                const speaker = String(turn?.speaker || '').toLowerCase();
                return {
                    speaker: speaker === 'customer' ? 'customer' : 'trainee',
                    text: String(turn?.text || '').trim(),
                };
            })
            .filter((turn) => turn.text.length > 0);
    }

    function updateScriptTabCount(turnCount) {
        const scriptTab = document.getElementById('vt-preview-script-tab');
        if (!scriptTab) return;
        const span = scriptTab.querySelector('span');
        const label = turnCount ? `Full Script (${turnCount} turns)` : 'Full Script';
        if (span) span.textContent = label;
        else scriptTab.textContent = label;
    }

    function renderEditTurnCard(turn, index, total, focusIdx) {
        const side = turn.speaker === 'customer' ? 'customer' : 'trainee';
        const isAgent = side === 'trainee';
        const focused = index === focusIdx;
        return `
            <div class="vt-studio-row ${side} ${focused ? 'focused' : ''}" data-idx="${index}">
                <div class="vt-studio-card" role="group" aria-label="Turn ${index + 1}">
                    <div class="vt-studio-card-head">
                        <span class="vt-studio-turn-num">#${index + 1}</span>
                        <div class="vt-studio-role-toggle" role="group" aria-label="Speaker">
                            <button type="button" class="vt-studio-role-btn ${isAgent ? 'active' : ''}" data-action="role" data-role="trainee" title="Agent line">Agent</button>
                            <button type="button" class="vt-studio-role-btn ${!isAgent ? 'active' : ''}" data-action="role" data-role="customer" title="Customer line">Customer</button>
                        </div>
                        <div class="vt-studio-actions">
                            <button type="button" class="vt-studio-icon-btn" data-action="up" title="Move up" ${index === 0 ? 'disabled' : ''}><i class="fas fa-arrow-up"></i></button>
                            <button type="button" class="vt-studio-icon-btn" data-action="down" title="Move down" ${index >= total - 1 ? 'disabled' : ''}><i class="fas fa-arrow-down"></i></button>
                            <button type="button" class="vt-studio-icon-btn" data-action="dup" title="Duplicate"><i class="fas fa-copy"></i></button>
                            <button type="button" class="vt-studio-icon-btn" data-action="add" title="Insert below"><i class="fas fa-plus"></i></button>
                            <button type="button" class="vt-studio-icon-btn danger" data-action="del" title="Delete" ${total <= 1 ? 'disabled' : ''}><i class="fas fa-trash"></i></button>
                        </div>
                    </div>
                    <textarea class="vt-edit-text" data-idx="${index}" rows="2" placeholder="${isAgent ? 'Agent reference line…' : 'Customer line…'}">${escapeHtml(turn.text || '')}</textarea>
                    <div class="vt-studio-card-foot">
                        <span>${isAgent ? 'Agent line' : 'Customer line'}</span>
                        <span class="vt-studio-word-count">${(turn.text || '').split(/\s+/).filter(Boolean).length} words</span>
                    </div>
                </div>
            </div>`;
    }

    function bindEditStudioEvents(container) {
        const autoResizeTextareas = () => {
            container.querySelectorAll('.vt-edit-text').forEach((ta) => {
                const grow = () => {
                    ta.style.height = 'auto';
                    ta.style.height = `${Math.max(56, ta.scrollHeight)}px`;
                };
                grow();
                ta.addEventListener('input', () => {
                    grow();
                    const foot = ta.closest('.vt-studio-card')?.querySelector('.vt-studio-word-count');
                    if (foot) foot.textContent = `${ta.value.split(/\s+/).filter(Boolean).length} words`;
                });
            });
        };

        const focusTurn = (idx) => {
            const draft = state.editScriptDraft || [];
            if (!draft.length) {
                state.editFocusTurn = 0;
                return;
            }
            state.editFocusTurn = Math.max(0, Math.min(idx, draft.length - 1));
            container.querySelectorAll('.vt-studio-row').forEach((row) => {
                row.classList.toggle('focused', Number(row.dataset.idx) === state.editFocusTurn);
            });
            const row = container.querySelector(`.vt-studio-row[data-idx="${state.editFocusTurn}"]`);
            row?.scrollIntoView({ block: 'center', behavior: 'smooth' });
            const counter = document.getElementById('vt-edit-turn-counter');
            if (counter) counter.textContent = `Turn ${state.editFocusTurn + 1} of ${draft.length}`;
            // Put caret in the focused turn's textarea (Add turn / dup / nav)
            const ta = row?.querySelector('textarea.vt-edit-text');
            if (ta) {
                window.requestAnimationFrame(() => {
                    try {
                        ta.focus({ preventScroll: true });
                        const end = ta.value.length;
                        if (typeof ta.setSelectionRange === 'function') {
                            ta.setSelectionRange(end, end);
                        }
                    } catch {
                        ta.focus();
                    }
                });
            }
        };

        const applyMutation = (fn) => {
            syncEditDraftFromDom(container);
            const draft = state.editScriptDraft;
            if (!draft) return;
            // Mutations set state.editFocusTurn (add / dup / reorder); preserve that.
            fn(draft);
            if (!draft.length) {
                state.editFocusTurn = 0;
            } else {
                state.editFocusTurn = Math.max(
                    0,
                    Math.min(Number(state.editFocusTurn) || 0, draft.length - 1)
                );
            }
            renderPreviewScript();
        };

        container.querySelectorAll('.vt-studio-row').forEach((row) => {
            row.addEventListener('click', (e) => {
                if (e.target.closest('button, textarea')) return;
                focusTurn(Number(row.dataset.idx));
            });
        });

        container.querySelectorAll('[data-action]').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const row = btn.closest('.vt-studio-row');
                if (!row) return;
                const idx = Number(row.dataset.idx);
                const action = btn.dataset.action;

                if (action === 'role') {
                    applyMutation((draft) => {
                        if (draft[idx]) draft[idx].speaker = btn.dataset.role;
                    });
                    return;
                }

                if (action === 'up' && idx > 0) {
                    applyMutation((draft) => {
                        [draft[idx - 1], draft[idx]] = [draft[idx], draft[idx - 1]];
                        state.editFocusTurn = idx - 1;
                    });
                    return;
                }

                if (action === 'down' && idx < (state.editScriptDraft?.length || 0) - 1) {
                    applyMutation((draft) => {
                        [draft[idx], draft[idx + 1]] = [draft[idx + 1], draft[idx]];
                        state.editFocusTurn = idx + 1;
                    });
                    return;
                }

                if (action === 'dup') {
                    applyMutation((draft) => {
                        const copy = { ...draft[idx], text: draft[idx].text };
                        draft.splice(idx + 1, 0, copy);
                        state.editFocusTurn = idx + 1;
                    });
                    return;
                }

                if (action === 'add') {
                    applyMutation((draft) => {
                        const alt = draft[idx]?.speaker === 'customer' ? 'trainee' : 'customer';
                        draft.splice(idx + 1, 0, { speaker: alt, text: '' });
                        state.editFocusTurn = idx + 1;
                    });
                    return;
                }

                if (action === 'del' && (state.editScriptDraft?.length || 0) > 1) {
                    applyMutation((draft) => {
                        draft.splice(idx, 1);
                        state.editFocusTurn = Math.min(idx, draft.length - 1);
                    });
                }
            });
        });

        document.getElementById('vt-edit-add-end')?.addEventListener('click', () => {
            applyMutation((draft) => {
                const last = draft[draft.length - 1];
                const alt = last?.speaker === 'customer' ? 'trainee' : 'customer';
                draft.push({ speaker: alt, text: '' });
                state.editFocusTurn = draft.length - 1;
            });
        });

        document.getElementById('vt-edit-prev')?.addEventListener('click', () => focusTurn(state.editFocusTurn - 1));
        document.getElementById('vt-edit-next')?.addEventListener('click', () => focusTurn(state.editFocusTurn + 1));

        autoResizeTextareas();
        focusTurn(state.editFocusTurn);
    }

    function renderScriptEdit(container) {
        const draft = state.editScriptDraft || state.previewScenario?.success_script || [];
        const focusIdx = Math.max(0, Math.min(state.editFocusTurn, draft.length - 1));
        state.editFocusTurn = focusIdx;

        container.innerHTML = `
            <div class="vt-script-edit-layout">
            <div class="vt-script-studio-bar">
                <span><i class="fas fa-pen"></i> Script Studio · ${draft.length} turns</span>
                <div class="vt-studio-bar-actions">
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-edit-cancel">Cancel</button>
                    <button type="button" class="btn btn-primary btn-sm" id="vt-edit-save">Save changes</button>
                </div>
            </div>
            <p class="vt-script-hint">Customer ← left · Agent → right · Click a turn to focus · Use ↑↓ to reorder</p>
            <div class="vt-studio-columns">
                <span class="vt-studio-col-agent"><i class="fas fa-headset"></i> Agent</span>
                <span class="vt-studio-col-customer"><i class="fas fa-user"></i> Customer</span>
            </div>
            <div class="vt-edit-turns" id="vt-edit-turns-list">
                ${draft.map((t, i) => renderEditTurnCard(t, i, draft.length, focusIdx)).join('')}
            </div>
            <div class="vt-studio-footer">
                <button type="button" class="btn btn-secondary btn-sm" id="vt-edit-add-end"><i class="fas fa-plus"></i> Add turn at end</button>
                <div class="vt-studio-pager">
                    <span id="vt-edit-turn-counter">Turn ${focusIdx + 1} of ${draft.length}</span>
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-edit-prev" ${focusIdx === 0 ? 'disabled' : ''}>Prev</button>
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-edit-next" ${focusIdx >= draft.length - 1 ? 'disabled' : ''}>Next</button>
                </div>
            </div>
            </div>
        `;

        document.getElementById('vt-edit-cancel')?.addEventListener('click', () => {
            state.editScriptDraft = null;
            state.editFocusTurn = 0;
            state.previewScriptView = 'timeline';
            renderPreviewScript();
        });
        document.getElementById('vt-edit-save')?.addEventListener('click', saveEditedScript);
        bindEditStudioEvents(container);
    }

    async function saveEditedScript() {
        const draft = state.editScriptDraft;
        if (!draft || !state.scenarioId) return;
        const contentArea = document.getElementById('vt-script-content-area');
        syncEditDraftFromDom(contentArea);
        const cleaned = sanitizeScriptDraft(draft);
        if (cleaned.length < 2) {
            notify('Add at least two turns with text before saving.', 'warning');
            return;
        }
        try {
            if (typeof window.showLoader === 'function') window.showLoader('Saving script...');
            await vtApi(`/scenarios/${encodeURIComponent(state.scenarioId)}`, 'PUT', {
                success_script: cleaned,
            });
            const refreshed = await vtApi(`/scenarios/${encodeURIComponent(state.scenarioId)}`);
            state.previewScenario = refreshed;
            state.editScriptDraft = null;
            state.editFocusTurn = firstCustomerTurnIndex(coalesceScriptTurns(refreshed.success_script || []));
            state.previewTurn = state.editFocusTurn;
            state.previewScriptView = 'timeline';
            notify(isSuperAdmin() ? 'Script saved. The scenario owner has been notified.' : 'Script saved.', 'success');
            await loadScenarios();
            updateScriptTabCount((refreshed.success_script || []).length);
            renderPreviewScript();
        } catch (err) {
            notify(err?.message || 'Could not save script.', 'error');
        } finally {
            if (typeof window.hideLoader === 'function') window.hideLoader();
        }
    }

    function stopGenProgressTimer() {
        if (state.gen.progressTimer) {
            clearInterval(state.gen.progressTimer);
            state.gen.progressTimer = null;
        }
    }

    function formatGenWarning(raw) {
        const text = String(raw || '').trim();
        if (!text) return '';
        if (/Missing transcribe:StartTranscriptionJob|AWS Transcribe permission missing/i.test(text)) {
            const filePart = text.includes(':') ? text.split(':')[0].trim() : 'Audio file';
            return `${filePart}: Audio could not be transcribed — the server lacks AWS Transcribe permission. Upload a .txt transcript with the same filename, or ask your admin to add transcribe:StartTranscriptionJob to the server IAM role. Your scenario was still created from your description and other files.`;
        }
        if (/not configured/i.test(text) && /transcribe/i.test(text)) {
            const filePart = text.includes(':') ? text.split(':')[0].trim() : 'Audio file';
            return `${filePart}: AWS Transcribe is not configured. Upload a matching .txt transcript file (e.g. File_3.txt alongside File_3.wav).`;
        }
        if (/AccessDenied/i.test(text) && /transcribe/i.test(text)) {
            const filePart = text.includes(':') ? text.split(':')[0].trim() : 'Attached file';
            return `${filePart}: Audio could not be transcribed — the server lacks AWS Transcribe permission. Upload a .txt transcript or ask your admin to enable Transcribe.`;
        }
        if (/transcribe/i.test(text) && /error/i.test(text)) {
            const filePart = text.includes(':') ? text.split(':')[0].trim() : 'Attached file';
            return `${filePart}: Transcription failed. Upload a .txt transcript with the same name, or continue without this audio.`;
        }
        return text;
    }

    function buildGenProgressSteps() {
        if (state.gen.mode === 'ai_auto') {
            return [
                { label: 'Connecting to AI (Amazon Bedrock)', icon: 'fa-robot' },
                { label: `Generating ${state.gen.count || 1} unique scenario(s)`, icon: 'fa-layer-group' },
                { label: 'Saving scenarios to your library', icon: 'fa-save' },
            ];
        }
        const steps = [];
        const hasFiles = state.gen.files.length > 0;
        if (hasFiles) {
            steps.push({ label: 'Uploading your files to the server', icon: 'fa-cloud-upload-alt' });
            state.gen.files.forEach((f) => {
                if (isAudioFile(f.name)) {
                    steps.push({
                        label: `Transcribing "${f.name}" with AWS Transcribe`,
                        icon: 'fa-microphone',
                        detail: 'This step can take 30–90 seconds for longer recordings.',
                    });
                } else {
                    steps.push({
                        label: `Reading document "${f.name}"`,
                        icon: 'fa-file-alt',
                    });
                }
            });
        }
        if (state.gen.story.trim()) {
            steps.push({
                label: 'Applying your call description',
                icon: 'fa-pen',
            });
        }
        if (hasFiles) {
            steps.push({
                label: 'Using uploaded file as the primary conversation source',
                icon: 'fa-file-audio',
            });
        }
        if (state.gen.category === 'custom' && state.gen.customLabel) {
            steps.push({
                label: `Tagging scenario as "${state.gen.customLabel}"`,
                icon: 'fa-tags',
            });
        } else if (state.gen.category) {
            const cat = CATEGORIES[state.gen.category];
            steps.push({
                label: `Tagging scenario for ${cat?.label || state.gen.category}`,
                icon: 'fa-folder-open',
            });
        }
        if (state.gen.customUseCase?.trim()) {
            steps.push({
                label: 'Applying your policies and resolution rules',
                icon: 'fa-clipboard-list',
            });
        }
        steps.push(
            { label: 'Generating training script with AI (Bedrock)', icon: 'fa-robot', detail: 'Creating customer + agent turns and SOP checklist.' },
            { label: 'Saving scenario to your library', icon: 'fa-save' },
        );
        return steps;
    }

    function renderGenProgress(activeIndex, allDone = false) {
        const list = document.getElementById('vt-gen-progress-list');
        if (!list) return;
        const steps = state.gen.progressSteps;
        list.innerHTML = steps.map((step, idx) => {
            let status = 'pending';
            if (allDone || idx < activeIndex) status = 'done';
            else if (idx === activeIndex) status = 'active';
            const icon = status === 'done'
                ? '<i class="fas fa-check-circle"></i>'
                : status === 'active'
                    ? '<i class="fas fa-circle-notch fa-spin"></i>'
                    : `<i class="fas ${step.icon}"></i>`;
            const detail = step.detail && status === 'active'
                ? `<span class="vt-gen-progress-detail">${escapeHtml(step.detail)}</span>`
                : '';
            return `<li class="vt-gen-progress-item ${status}">
                <span class="vt-gen-progress-icon">${icon}</span>
                <span>${escapeHtml(step.label)}${detail}</span>
            </li>`;
        }).join('');
    }

    function showGenProgressPanel() {
        stopGenProgressTimer();
        state.gen.showResult = false;
        state.gen.progressSteps = buildGenProgressSteps();
        state.gen.progressIndex = 0;
        document.getElementById('vt-gen-panel-mode')?.classList.add('hidden');
        document.getElementById('vt-gen-panel-scope')?.classList.add('hidden');
        document.getElementById('vt-gen-panel-story')?.classList.add('hidden');
        document.getElementById('vt-gen-panel-launch')?.classList.add('hidden');
        document.getElementById('vt-gen-panel-result')?.classList.add('hidden');
        document.getElementById('vt-gen-panel-progress')?.classList.remove('hidden');
        document.getElementById('vt-gen-step-indicators')?.classList.add('hidden');
        document.getElementById('vt-gen-back-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-next-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-submit-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-cancel-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-done-btn')?.classList.add('hidden');
        const lead = document.getElementById('vt-gen-progress-lead');
        const hasFiles = state.gen.files.length > 0;
        const hasAudio = state.gen.files.some((f) => isAudioFile(f.name));
        if (lead) {
            if (hasAudio) {
                lead.textContent = 'Audio transcription can take up to a minute. Other steps run on the server while you wait.';
            } else if (hasFiles) {
                lead.textContent = 'Please wait — we are reading your documents and generating the scenario.';
            } else {
                lead.textContent = 'Please wait — we are building your scenario from your description.';
            }
        }
        renderGenProgress(0);
        const maxAdvance = Math.max(0, state.gen.progressSteps.length - 2);
        state.gen.progressTimer = setInterval(() => {
            if (state.gen.progressIndex < maxAdvance) {
                state.gen.progressIndex += 1;
                renderGenProgress(state.gen.progressIndex);
            }
        }, hasAudio ? 4500 : 2200);
    }

    function showGenResultPanel(res) {
        stopGenProgressTimer();
        state.gen.showResult = true;
        document.getElementById('vt-gen-panel-progress')?.classList.add('hidden');
        document.getElementById('vt-gen-panel-result')?.classList.remove('hidden');
        const container = document.getElementById('vt-gen-result-content');
        if (!container) return;

        const scenarios = res.scenarios || [];
        const warnings = (res.warnings || []).map(formatGenWarning).filter(Boolean);
        const usedFallback = res.source === 'fallback' || warnings.length > 0;
        const scenarioBlocks = scenarios.map((s) => `
            <div class="vt-gen-result-scenario">
                <h4>${escapeHtml(s.title || s.scenario_title || 'New scenario')}</h4>
                <p>${escapeHtml(s.description || s.summary || 'Ready for practice and assessment.')}</p>
            </div>
        `).join('');

        const warningBlock = warnings.length ? `
            <div class="vt-gen-result-warnings">
                <h4><i class="fas fa-triangle-exclamation"></i> Notes during generation</h4>
                <ul>${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul>
            </div>
        ` : '';

        const fallbackNote = usedFallback
            ? '<p class="vt-gen-result-note">Some steps used built-in templates because AI or file processing could not complete fully. You can still practice with the scenario, or edit the script from Preview.</p>'
            : '<p class="vt-gen-result-note">Scenario created with AI from your description and attached files.</p>';

        container.innerHTML = `
            <div class="vt-gen-result-banner">
                <i class="fas fa-check-circle"></i>
                <div>
                    <strong>${escapeHtml(res.message || 'Scenario created successfully.')}</strong>
                    <p>${scenarios.length ? `Added ${scenarios.length} scenario${scenarios.length > 1 ? 's' : ''} to your library.` : 'Your scenario is ready to use.'}</p>
                </div>
            </div>
            ${scenarioBlocks}
            ${warningBlock}
            ${fallbackNote}
        `;

        document.getElementById('vt-gen-back-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-next-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-submit-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-cancel-btn')?.classList.add('hidden');
        document.getElementById('vt-gen-done-btn')?.classList.remove('hidden');
        document.querySelector('.vt-generator-modal')?.classList.add('gen-result-mode');
    }

    function resetGenWizardPanels() {
        stopGenProgressTimer();
        state.gen.showResult = false;
        state.gen.progressSteps = [];
        state.gen.progressIndex = 0;
        document.getElementById('vt-gen-panel-progress')?.classList.add('hidden');
        document.getElementById('vt-gen-panel-result')?.classList.add('hidden');
        document.getElementById('vt-gen-step-indicators')?.classList.remove('hidden');
        document.getElementById('vt-gen-cancel-btn')?.classList.remove('hidden');
        document.getElementById('vt-gen-done-btn')?.classList.add('hidden');
        document.querySelector('.vt-generator-modal')?.classList.remove('gen-result-mode');
        document.querySelector('.vt-generator-modal')?.classList.remove('story-step');
    }

    /* ── Generator wizard ── */
    function openGeneratorWizard() {
        stopGenProgressTimer();
        state.gen = {
            step: 'mode',
            mode: 'ai_auto',
            category: 'technical_troubleshooting',
            customLabel: '',
            customUseCase: '',
            story: '',
            files: [],
            promptExamples: [],
            progressTimer: null,
            progressIndex: 0,
            progressSteps: [],
            showResult: false,
            count: 1,
        };
        document.getElementById('vt-gen-story').value = '';
        document.getElementById('vt-gen-auto-count').value = '1';
        document.getElementById('vt-gen-custom-label').value = '';
        document.getElementById('vt-gen-custom-use-case').value = '';
        document.getElementById('vt-gen-topic-hint').value = '';
        document.getElementById('vt-gen-examples-list').innerHTML = '';
        document.getElementById('vt-gen-file-list').innerHTML = '';
        document.getElementById('vt-gen-files').value = '';
        resetGenWizardPanels();
        renderGenCategoryGrid();
        updateGenWizardUI();
        document.getElementById('vt-generator-modal')?.classList.remove('hidden');
    }

    function closeGeneratorWizard() {
        const afterSuccessfulGeneration = Boolean(state.gen && state.gen.showResult);
        stopGenProgressTimer();
        resetGenWizardPanels();
        document.getElementById('vt-generator-modal')?.classList.add('hidden');
        // After AI generate succeeds, drop filters so the new scenario is visible.
        if (afterSuccessfulGeneration) {
            resetScenarioListFilters();
            loadScenarios();
        }
    }

    async function finishGeneratorWizard() {
        // Done closes the wizard; successful close resets filters and reloads the list.
        closeGeneratorWizard();
    }

    function genStepsForMode() {
        return state.gen.mode === 'ai_auto' ? ['mode', 'launch'] : ['mode', 'scope', 'story', 'launch'];
    }

    function updateGenWizardUI() {
        if (state.gen.showResult) return;
        resetGenWizardPanels();
        const steps = genStepsForMode();
        const step = state.gen.step;
        document.querySelectorAll('.vt-gen-step-dot').forEach((dot) => {
            const name = dot.dataset.step;
            const visible = steps.includes(name);
            dot.classList.toggle('hidden', !visible);
            dot.classList.toggle('active', name === step);
            dot.classList.toggle('done', steps.indexOf(name) < steps.indexOf(step));
        });
        document.getElementById('vt-gen-panel-mode')?.classList.toggle('hidden', step !== 'mode');
        document.getElementById('vt-gen-panel-scope')?.classList.toggle('hidden', step !== 'scope');
        document.getElementById('vt-gen-panel-story')?.classList.toggle('hidden', step !== 'story');
        document.getElementById('vt-gen-panel-launch')?.classList.toggle('hidden', step !== 'launch');
        document.getElementById('vt-gen-auto-count-wrap')?.classList.toggle(
            'hidden',
            step !== 'launch' || state.gen.mode !== 'ai_auto',
        );
        document.querySelectorAll('.vt-gen-mode-card').forEach((c) => {
            c.classList.toggle('active', c.dataset.mode === state.gen.mode);
        });
        document.getElementById('vt-gen-custom-fields')?.classList.toggle('hidden', state.gen.category !== 'custom');
        const idx = steps.indexOf(step);
        document.getElementById('vt-gen-back-btn')?.classList.toggle('hidden', idx <= 0);
        document.getElementById('vt-gen-next-btn')?.classList.toggle('hidden', step === 'launch');
        document.getElementById('vt-gen-submit-btn')?.classList.toggle('hidden', step !== 'launch');
        document.getElementById('vt-gen-story-count').textContent = String(state.gen.story.length);
        document.querySelector('.vt-generator-modal')?.classList.toggle('story-step', step === 'story');
        if (step === 'launch') renderGenSummary();
    }

    function examplePromptText(item) {
        if (typeof item === 'string') return item.trim();
        return String(item?.text || item?.prompt || '').trim();
    }

    function applyPromptExample(text, selectedIdx) {
        const val = String(text || '').trim();
        const ta = document.getElementById('vt-gen-story');
        if (!ta || !val) return;
        ta.value = val;
        state.gen.story = val;
        const countEl = document.getElementById('vt-gen-story-count');
        if (countEl) countEl.textContent = String(val.length);
        ta.classList.add('vt-gen-story-highlight');
        window.setTimeout(() => ta.classList.remove('vt-gen-story-highlight'), 1800);
        ta.focus({ preventScroll: true });
        ta.scrollIntoView({ behavior: 'smooth', block: 'center' });
        const body = document.querySelector('.vt-gen-body');
        if (body) body.scrollTo({ top: 0, behavior: 'smooth' });
        document.querySelectorAll('.vt-example-card').forEach((card) => {
            card.classList.toggle('selected', Number(card.dataset.idx) === selectedIdx);
        });
    }

    function renderGenCategoryGrid() {
        const grid = document.getElementById('vt-gen-category-grid');
        if (!grid) return;
        grid.innerHTML = Object.entries(CATEGORIES).map(([key, meta]) => `
            <button type="button" class="vt-gen-cat-card ${state.gen.category === key ? 'active' : ''}" data-cat="${key}">
                <i class="fas ${meta.icon}"></i>
                <span>${escapeHtml(meta.label)}</span>
            </button>
        `).join('');
        grid.querySelectorAll('.vt-gen-cat-card').forEach((btn) => {
            btn.addEventListener('click', () => {
                state.gen.category = btn.dataset.cat;
                renderGenCategoryGrid();
                updateGenWizardUI();
            });
        });
    }

    function renderGenSummary() {
        const el = document.getElementById('vt-gen-summary');
        if (!el) return;
        if (state.gen.mode === 'ai_auto') {
            const count = Math.max(1, Math.min(5, Number(state.gen.count) || 1));
            el.innerHTML = `
                <div class="vt-gen-summary-card">
                    <i class="fas fa-robot"></i>
                    <div>
                        <strong>Full AI Auto</strong>
                        <p>Generate <strong>${count}</strong> unique scenario${count === 1 ? '' : 's'} across billing, technical, escalations, and activation. Duplicates are skipped automatically.</p>
                    </div>
                </div>`;
            return;
        }
        const cat = CATEGORIES[state.gen.category] || CATEGORIES.custom;
        const story = state.gen.story.trim();
        const docSummary = describeGenFilesSummary();
        el.innerHTML = `
            <div class="vt-gen-summary-card">
                <i class="fas ${cat.icon}"></i>
                <div>
                    <strong>${escapeHtml(cat.label)}</strong>
                    ${story
                        ? `<p>${escapeHtml(story.slice(0, 200))}${story.length > 200 ? '…' : ''}</p>`
                        : '<p class="vt-gen-doc-skipped"><i class="fas fa-comment-dots"></i> No call description — using document content only.</p>'}
                    ${docSummary.html}
                </div>
            </div>`;
    }

    function genGoNext() {
        const steps = genStepsForMode();
        const idx = steps.indexOf(state.gen.step);
        if (state.gen.step === 'story') {
            state.gen.story = document.getElementById('vt-gen-story')?.value?.trim() || '';
            const hasFiles = state.gen.files.length > 0;
            if (state.gen.story.length < 20 && !hasFiles) {
                notify('Describe the call (20+ characters) or attach a reference document / WAV recording.', 'warning');
                return;
            }
        }
        if (state.gen.step === 'scope' && state.gen.category === 'custom') {
            state.gen.customLabel = document.getElementById('vt-gen-custom-label')?.value?.trim() || '';
            if (state.gen.customLabel.length < 3) {
                notify('Custom category name must be at least 3 characters.', 'warning');
                return;
            }
            state.gen.customUseCase = document.getElementById('vt-gen-custom-use-case')?.value?.trim() || '';
        }
        if (idx < steps.length - 1) {
            state.gen.step = steps[idx + 1];
            updateGenWizardUI();
        }
    }

    function genGoBack() {
        const steps = genStepsForMode();
        const idx = steps.indexOf(state.gen.step);
        if (idx > 0) {
            state.gen.step = steps[idx - 1];
            updateGenWizardUI();
        }
    }

    async function submitGenerator() {
        const submitBtn = document.getElementById('vt-gen-submit-btn');
        if (submitBtn?.disabled) return;
        try {
            if (submitBtn) submitBtn.disabled = true;
            if (state.gen.mode === 'ai_auto') {
                const rawCount = Number(document.getElementById('vt-gen-auto-count')?.value ?? state.gen.count ?? 1);
                const count = Math.max(1, Math.min(5, Number.isFinite(rawCount) ? Math.round(rawCount) : 1));
                state.gen.count = count;
            }
            showGenProgressPanel();
            let res;
            if (state.gen.mode === 'ai_auto') {
                res = await vtApi('/scenarios/generate', 'POST', {
                    generation_mode: 'ai_auto',
                    count: state.gen.count || 1,
                });
            } else {
                const form = new FormData();
                form.append('generation_mode', 'prompt_guided');
                form.append('category', state.gen.category);
                form.append('custom_prompt', state.gen.story.trim());
                if (state.gen.category === 'custom') {
                    form.append('custom_category_label', state.gen.customLabel);
                    if (state.gen.customUseCase) form.append('custom_use_case', state.gen.customUseCase);
                }
                state.gen.files.forEach((f) => form.append('files', f, f.name));
                res = await vtApi('/scenarios/generate-rich', 'POST', form, true);
            }
            renderGenProgress(state.gen.progressSteps.length, true);
            showGenResultPanel(res);
            notify(res.message || 'Scenario created.', 'success');
        } catch (err) {
            stopGenProgressTimer();
            resetGenWizardPanels();
            updateGenWizardUI();
            notify(err?.message || 'Scenario generation failed.', 'error');
        } finally {
            if (submitBtn) submitBtn.disabled = false;
            if (typeof window.hideLoader === 'function') window.hideLoader();
        }
    }

    async function loadPromptExamples() {
        const hint = document.getElementById('vt-gen-topic-hint')?.value?.trim();
        const list = document.getElementById('vt-gen-examples-list');
        if (!list) return;
        list.innerHTML = '<p class="placeholder-text"><i class="fas fa-spinner fa-spin"></i> Generating detailed examples...</p>';
        try {
            const res = await vtApi('/scenarios/prompt-examples', 'POST', { topic_hint: hint || null });
            const examples = res.examples || [];
            state.gen.promptExamples = Array.isArray(examples) ? examples : [];
            if (!state.gen.promptExamples.length) {
                list.innerHTML = '<p class="placeholder-text">No examples returned.</p>';
                return;
            }
            list.innerHTML = state.gen.promptExamples.map((ex, i) => {
                const text = examplePromptText(ex);
                return `
                <button type="button" class="vt-example-card" data-idx="${i}" aria-label="Use example ${i + 1}">
                    <span class="vt-example-card-head">
                        <span class="vt-example-card-num">Example ${i + 1}</span>
                        <span class="vt-example-card-action">Click to use &uarr;</span>
                    </span>
                    <span class="vt-example-card-text">${escapeHtml(text)}</span>
                </button>`;
            }).join('');
            list.querySelectorAll('.vt-example-card').forEach((card) => {
                card.addEventListener('click', () => {
                    const idx = Number(card.dataset.idx);
                    applyPromptExample(examplePromptText(state.gen.promptExamples[idx]), idx);
                });
            });
        } catch {
            list.innerHTML = '<p class="placeholder-text">Could not load examples.</p>';
        }
    }

    function handleGenFiles(fileList) {
        const incoming = Array.from(fileList || []);
        const allowed = [];
        const rejected = [];
        incoming.forEach((f) => {
            if (isGenAllowedFile(f.name)) allowed.push(f);
            else rejected.push(f.name);
        });
        if (rejected.length) {
            notify(
                `Unsupported file type: ${rejected.join(', ')}. Use PDF, DOCX, TXT, MD, CSV, WAV, MP3, M4A, FLAC, OGG, WEBM, or AMR.`,
                'warning',
            );
        }
        state.gen.files = allowed;
        const list = document.getElementById('vt-gen-file-list');
        if (!list) return;
        if (!allowed.length) {
            list.innerHTML = '';
            return;
        }
        list.innerHTML = allowed.map((f) => {
            const audio = isAudioFile(f.name);
            const icon = audio ? 'fa-microphone' : 'fa-file';
            const tag = audio ? ' <span class="vt-gen-audio-tag">AWS Transcribe</span>' : '';
            return `<span class="vt-gen-file-chip ${audio ? 'audio' : ''}"><i class="fas ${icon}"></i> ${escapeHtml(f.name)}${tag}</span>`;
        }).join('');
    }

    async function ensureAudioUnlocked() {
        if (state.audioUnlocked) return;
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (Ctx) {
                state.audioCtx = state.audioCtx || new Ctx();
                await state.audioCtx.resume();
            }
            state.audioUnlocked = true;
        } catch {
            /* browser TTS fallback still works */
        }
    }

    function emptyTranscriptMessage(mode) {
        return 'Speak your opening greeting — the customer will reply by voice.';
    }
    function showSessionContext(data) {
        // Cards above conversation history are hidden — keep data for SOP/hints only
        state.sessionContext = data || null;
        document.getElementById('vt-session-context')?.classList.add('hidden');
        document.getElementById('vt-sentiment-ring')?.classList.add('hidden');
    }

    function showCustomerSays(_text, _sentiment) {
        // Customer-says card removed from live session UI (lines live in conversation history)
        document.getElementById('vt-customer-says-panel')?.classList.add('hidden');
        document.getElementById('vt-sentiment-ring')?.classList.add('hidden');
    }

    function hideCustomerSays() {
        document.getElementById('vt-customer-says-panel')?.classList.add('hidden');
    }
    function setPreviewFooterBusy(busy, activeBtn = null) {
        const ids = [
            'vt-preview-practice-btn',
            'vt-preview-assess-btn',
            'vt-preview-edit-btn',
            'vt-preview-close',
        ];
        ids.forEach((id) => {
            const el = document.getElementById(id);
            if (!el || el === activeBtn) return;
            if (busy) {
                if (el.dataset.vtFooterPrevDisabled == null) {
                    el.dataset.vtFooterPrevDisabled = el.disabled ? '1' : '0';
                }
                el.disabled = true;
            } else if (el.dataset.vtFooterPrevDisabled != null) {
                el.disabled = el.dataset.vtFooterPrevDisabled === '1';
                delete el.dataset.vtFooterPrevDisabled;
            }
        });
        document.getElementById('vt-preview-modal')?.classList.toggle('vt-preview-starting', Boolean(busy));
    }

    async function startSession(scenarioId, mode, triggerBtn = null) {
        const fromAdmin = state.adminPreviewMode;
        if (mode === 'practice') {
            const scenario = state.scenarios.find((row) => row.id === scenarioId)
                || state.previewScenario;
            if (isPracticeDisabledForScenario(scenario)) {
                notify(
                    'Practice is disabled for trainees because practice sessions before assessment is set to 0.',
                    'warning',
                );
                return;
            }
        }
        const loadingLabel = mode === 'assessment' ? 'Starting assessment…' : 'Starting practice…';
        setActionButtonLoading(triggerBtn, true, loadingLabel);
        setPreviewFooterBusy(true, triggerBtn);
        // Force paint so spinner is visible before awaiting network/audio work
        if (triggerBtn) void triggerBtn.offsetWidth;
        try {
            try {
                stopAllPlayback();
            } catch { /* ignore */ }
            resetVoiceUiState();
            await ensureAudioUnlocked();
            await loadAgentSttConfig();
            if (fromAdmin && typeof window.switchView === 'function') {
                await window.switchView('voice-training');
            }
            if (fromAdmin) {
                state.resultsReturnTo = 'admin';
            }
            const data = await vtApi('/sessions/start', 'POST', { scenario_id: scenarioId, mode });
            state.sessionId = data.session_id;
            state.sessionMode = mode;
            state.scenarioId = scenarioId;
            document.getElementById('vt-session-title').textContent = data.scenario_title || 'Voice Session';
            const badge = document.getElementById('vt-session-mode-badge');
            if (badge) {
                badge.textContent = mode === 'practice' ? 'Practice mode' : 'Assessment';
                badge.className = `vt-badge ${mode}`;
            }
            document.getElementById('vt-hints-panel')?.classList.toggle('hidden', mode !== 'practice');
            document.getElementById('vt-practice-tips')?.classList.toggle('hidden', mode !== 'practice');
            document.getElementById('vt-assessment-tips')?.classList.toggle('hidden', mode !== 'assessment');
            hideCustomerSays();
            state.lastCustomerAudio = null;
            state.expandedSopStep = null;
            hideSessionCompleteBanner();
            state.conversationComplete = false;
            state.sessionExpectedTurns = data.expected_turns || 18;
            updateSessionHeaderActions(mode);
            document.getElementById('vt-session-context')?.classList.remove('compact');
            showSessionContext(data);
            const conversation = data.conversation || [];
            state.lastConversation = conversation;
            renderTranscript(conversation, 'vt-transcript', mode);
            const startGuidance = {
                ...(data.guidance || {}),
                suggested_response: data.suggested_response
                    || data.guidance?.suggested_response
                    || data.opening_agent_line
                    || null,
            };
            updateSessionPanels(conversation, startGuidance, data.hints, mode);
            updateReplyBar(conversation, mode);
            // Keep preview open with spinner until session is ready, then switch
            closePreview({ keepForSession: true });
            showVtSection('session');
            scrollTranscriptToBottom(false);

            if (data.customer_response) {
                state.lastCustomerAudio = {
                    audio: data.customer_audio,
                    text: data.customer_response,
                    sentiment: data.current_sentiment,
                };
                document.getElementById('vt-suggested-panel')?.classList.add('hidden');
                await playCustomerAudio(
                    data.customer_audio,
                    data.customer_response,
                    data.current_sentiment,
                );
                updateSessionPanels(conversation, data.guidance, data.hints, mode);
                scrollTranscriptToBottom(false);
            }
        } catch (err) {
            notify(err.message || 'Could not start session.', 'error');
        } finally {
            setActionButtonLoading(triggerBtn, false);
            setPreviewFooterBusy(false, triggerBtn);
            updatePreviewFooter();
        }
    }

    function chatSideForSpeaker(speaker) {
        return speaker === 'customer' ? 'left' : 'right';
    }

    function updateSessionProgress(conversation, guidance) {
        const label = document.getElementById('vt-session-turn-label');
        const sopLabel = document.getElementById('vt-session-sop-step-label');
        const fill = document.getElementById('vt-session-progress-fill');
        const track = document.querySelector('.vt-session-progress-track');
        const turns = conversation?.length || 0;
        // When the call ends early (filler skipped), snap expected to actual so UI shows N/N.
        if (state.conversationComplete && turns > 0) {
            state.sessionExpectedTurns = turns;
        }
        const expected = Math.max(state.sessionExpectedTurns || 18, turns, 1);
        const pct = Math.min(100, Math.round((turns / expected) * 100));
        if (label) {
            label.textContent = state.conversationComplete
                ? `Conversation: ${turns} / ${expected} turns`
                : `Conversation: ${turns} / ~${expected} turns`;
        }
        if (sopLabel && guidance?.current_sop?.name) {
            sopLabel.textContent = `SOP step ${guidance.current_step || 1}: ${guidance.current_sop.name}`;
        } else if (sopLabel) {
            sopLabel.textContent = guidance?.current_step
                ? `SOP step ${guidance.current_step} of ${guidance.total_steps || 5}`
                : '';
        }
        if (fill) fill.style.width = `${pct}%`;
        if (track) {
            track.setAttribute('aria-valuenow', String(pct));
            track.setAttribute('aria-valuemax', '100');
        }
    }

    function scrollTranscriptToBottom(smooth = true, boxEl = null) {
        const box = boxEl || document.getElementById('vt-transcript');
        if (!box || box.classList.contains('collapsed')) return;

        const pin = () => {
            if (box.classList.contains('collapsed')) return;
            const behavior = smooth ? 'smooth' : 'auto';
            // Direct assignment is most reliable after innerHTML reflow
            box.scrollTop = box.scrollHeight;
            if (typeof box.scrollTo === 'function') {
                box.scrollTo({ top: box.scrollHeight, behavior });
            }
            const latest = box.querySelector('.vt-session-chat-row.latest') || box.lastElementChild;
            if (latest && typeof latest.scrollIntoView === 'function') {
                latest.scrollIntoView({ block: 'end', inline: 'nearest', behavior });
                // Keep transcript scroller pinned even if ancestors also moved
                box.scrollTop = box.scrollHeight;
            }
            const stage = box.closest('.vt-session-stage');
            if (stage) {
                stage.scrollTop = stage.scrollHeight;
                if (typeof stage.scrollTo === 'function') {
                    stage.scrollTo({ top: stage.scrollHeight, behavior });
                }
            }
        };

        // Layout can settle across frames; turn-in animation also changes height
        requestAnimationFrame(() => {
            pin();
            requestAnimationFrame(pin);
        });
        window.setTimeout(pin, 60);
        window.setTimeout(pin, 400);
    }

    function updateTranscriptMeta(turnCount) {
        const countEl = document.getElementById('vt-transcript-count');
        const jumpBtn = document.getElementById('vt-transcript-jump-btn');
        if (countEl) {
            if (turnCount > 0) {
                countEl.textContent = String(turnCount);
                countEl.classList.remove('hidden');
            } else {
                countEl.classList.add('hidden');
            }
        }
        jumpBtn?.classList.toggle('hidden', turnCount < 4);
    }

    function renderTranscript(conversation, targetId = 'vt-transcript', mode = state.sessionMode) {
        const box = document.getElementById(targetId);
        if (!box) return;
        const turns = conversation || [];
        if (targetId === 'vt-transcript') {
            updateTranscriptMeta(turns.length);
        }
        if (!turns.length) {
            box.innerHTML = `<p class="vt-transcript-empty">${emptyTranscriptMessage(mode)}</p>`;
            return;
        }
        box.innerHTML = turns.map((t, i) => `
            <div class="vt-session-chat-row ${chatSideForSpeaker(t.speaker)} ${i === turns.length - 1 ? 'latest' : ''}">
                <div class="vt-turn vt-turn-${t.speaker}">
                    <span class="vt-turn-label">${t.speaker === 'customer' ? 'Customer' : 'You (Agent)'}</span>
                    <p>${escapeHtml(t.text)}</p>
                </div>
            </div>
        `).join('');
        // Auto pin for live session (practice + assessment); use instant jump for reliability
        if (targetId === 'vt-transcript') {
            scrollTranscriptToBottom(false, box);
        }
    }

    function setLiveSpeechVisible(visible, text = '') {
        const panel = document.getElementById('vt-live-speech');
        const textEl = document.getElementById('vt-live-speech-text');
        if (!panel || !textEl) return;
        if (visible && text) {
            textEl.textContent = text;
            panel.classList.remove('hidden');
        } else {
            panel.classList.add('hidden');
            textEl.textContent = '';
        }
    }

    function speechRecognitionLang() {
        if (state.transcribeLanguage && state.transcribeLanguage.startsWith('en')) {
            return state.transcribeLanguage;
        }
        const nav = (navigator.language || '').trim().toLowerCase();
        if (nav.startsWith('en-in')) return 'en-IN';
        if (nav.startsWith('en')) return navigator.language.trim();
        const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone || '').toLowerCase();
        if (tz.includes('kolkata') || tz.includes('calcutta') || tz.includes('mumbai') || tz.includes('chennai')) {
            return 'en-IN';
        }
        return 'en-US';
    }

    async function loadAgentSttConfig() {
        const token = window.authToken || localStorage.getItem('authToken');
        if (!token || !window.userProfile) {
            state.agentSttMode = 'browser';
            return;
        }
        if (typeof window.apiCall !== 'function') {
            state.agentSttMode = 'browser';
            return;
        }
        try {
            const result = await window.apiCall(
                `${VT_API}/health`,
                'GET',
                null,
                false,
                0,
                true,
                window.API_FETCH_TIMEOUT_MS || 30000,
                null,
                true,
            );
            if (!result?.success) {
                state.agentSttMode = 'browser';
                return;
            }
            const health = result.data || {};
            state.agentSttMode = health.agent_stt_mode === 'aws_transcribe' ? 'aws_transcribe' : 'browser';
            state.transcribeLanguage = health.transcribe_language || 'en-IN';
            state.agentSttMaxWaitMs = Number(health.agent_stt_max_wait_ms) || 12000;
        } catch {
            state.agentSttMode = 'browser';
        }
    }

    function normalizeTranscriptCompare(text) {
        return String(text || '')
            .trim()
            .toLowerCase()
            .replace(/[^\w\s']/g, ' ')
            .replace(/\s+/g, ' ')
            .trim();
    }

    function pickAgentTranscriptText(awsText, browserText) {
        const aws = dedupeRepeatedSpeech((awsText || '').trim());
        const browser = dedupeRepeatedSpeech((browserText || '').trim());
        const preferAws = state.agentSttMode === 'aws_transcribe';
        const awsOk = aws.length >= 2;
        const browserOk = browser.length >= 2;

        if (!awsOk && !browserOk) {
            return { text: '', source: preferAws ? 'aws_transcribe' : 'browser' };
        }
        if (awsOk && !browserOk) {
            return { text: aws, source: 'aws_transcribe' };
        }
        if (!awsOk && browserOk) {
            return { text: browser, source: 'browser' };
        }

        const awsNorm = normalizeTranscriptCompare(aws);
        const browserNorm = normalizeTranscriptCompare(browser);
        if (awsNorm === browserNorm) {
            return { text: aws, source: 'aws_transcribe' };
        }
        if (awsNorm.includes(browserNorm) || browserNorm.includes(awsNorm)) {
            return aws.length >= browser.length
                ? { text: aws, source: 'aws_transcribe' }
                : { text: browser, source: 'browser' };
        }

        // AWS mode: prefer Transcribe unless it looks like a tiny fragment and browser is much longer.
        if (preferAws) {
            const awsLooksFragment = aws.length < 12 && !aws.includes(' ');
            if (awsLooksFragment && browser.length > aws.length * 2.5) {
                return { text: browser, source: 'browser' };
            }
            if (aws.length >= browser.length * 0.5) {
                return { text: aws, source: 'aws_transcribe' };
            }
        }

        return browser.length > aws.length
            ? { text: browser, source: 'browser' }
            : { text: aws, source: 'aws_transcribe' };
    }

    function startBrowserCaptureParallel() {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition || state.browserCaptureActive) return false;
        try {
            state.recognitionStopRequested = false;
            state.recognition = new SpeechRecognition();
            state.recognition.continuous = true;
            state.recognition.interimResults = true;
            state.recognition.lang = speechRecognitionLang();
            if ('maxAlternatives' in state.recognition) {
                state.recognition.maxAlternatives = 1;
            }
            state.recognition.onresult = (ev) => {
                applyRecognitionResults(ev.results, ev.resultIndex);
            };
            state.recognition.onerror = (ev) => {
                if (ev.error === 'no-speech' || ev.error === 'aborted') return;
                state.browserCaptureActive = false;
            };
            state.recognition.onend = () => {
                if (state.browserCaptureActive && !state.recognitionStopRequested) {
                    try {
                        state.recognition.start();
                    } catch { /* ignore restart errors */ }
                }
            };
            state.recognition.start();
            state.browserCaptureActive = true;
            return true;
        } catch {
            state.browserCaptureActive = false;
            return false;
        }
    }

    function stopBrowserCaptureParallel() {
        return new Promise((resolve) => {
            if (!state.browserCaptureActive || !state.recognition) {
                resolve(dedupeRepeatedSpeech((state.speechFinal || mergedSpeechText() || '').trim()));
                return;
            }
            let settled = false;
            const finish = () => {
                if (settled) return;
                settled = true;
                const text = dedupeRepeatedSpeech((state.speechFinal || mergedSpeechText() || '').trim());
                state.browserCaptureActive = false;
                state.recognitionStopRequested = false;
                resetSpeechCapture();
                resolve(text);
            };
            state.recognitionStopRequested = true;
            commitSpeechInterim();
            state.recognition.onend = () => finish();
            try {
                state.recognition.stop();
            } catch {
                finish();
            }
            const hasPreview = Boolean((state.speechFinal || state.speechInterim || '').trim());
            window.setTimeout(finish, hasPreview ? 800 : 220);
        });
    }

    async function requestAwsTranscript(blob, mimeType, maxWaitMs) {
        const form = new FormData();
        form.append('audio', blob, recorderFilename(mimeType));
        const waitMs = Math.max(maxWaitMs || 12000, 2500);
        const awsCall = vtApi(
            `/sessions/${encodeURIComponent(state.sessionId)}/transcribe-reply`,
            'POST',
            form,
            true,
        );
        const res = await Promise.race([
            awsCall,
            new Promise((_, reject) => {
                window.setTimeout(() => reject(new Error('aws_timeout')), waitMs);
            }),
        ]);
        return dedupeRepeatedSpeech((res.text || '').trim());
    }

    async function requestAwsTranscriptWithRetry(blob, mimeType, options = {}) {
        const waitMs = Math.max(2500, options.maxWaitMs || state.agentSttMaxWaitMs || 12000);
        const maxAttempts = Math.max(1, options.maxAttempts ?? 2);
        for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
            try {
                const text = await requestAwsTranscript(blob, mimeType, waitMs);
                if (text) return text;
            } catch {
                /* retry or fail below */
            }
            if (attempt < maxAttempts - 1) {
                setReplyStatus('Still processing speech…', 'processing');
            }
        }
        return '';
    }

    function pickRecorderMimeType() {
        if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
        const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg;codecs=opus'];
        return candidates.find((t) => MediaRecorder.isTypeSupported(t)) || '';
    }

    function recorderFilename(mimeType) {
        const mt = (mimeType || '').toLowerCase();
        if (mt.includes('mp4')) return 'reply.m4a';
        if (mt.includes('ogg')) return 'reply.ogg';
        return 'reply.webm';
    }

    async function transcribeAndSendRecording(blob, mimeType, browserFallbackText = '') {
        const minBytes = state.minRecordingBytes || 512;
        const browserText = dedupeRepeatedSpeech((browserFallbackText || '').trim());
        if (!state.sessionId || !blob?.size || blob.size < minBytes) {
            if (browserText && blob?.size >= minBytes) {
                await sendMessage(browserText, { viaVoice: true, sttSource: 'browser' });
                return;
            }
            notify('Recording too short. Tap the mic, speak clearly, then tap again to send.', 'info');
            setReplyStatus('');
            updateReplyBar(state.lastConversation || [], state.sessionMode);
            return;
        }
        // Fast-fail empty speech: no browser preview → one short AWS attempt (or skip long retries)
        if (!browserText && blob.size < Math.max(minBytes * 4, 2048)) {
            notify('No speech detected. Tap the mic, speak clearly, then tap again to send.', 'info');
            setReplyStatus('');
            updateReplyBar(state.lastConversation || [], state.sessionMode);
            return;
        }
        setReplyStatus('Processing your speech…', 'processing');
        let awsText = '';
        if (state.agentSttMode === 'aws_transcribe') {
            awsText = await requestAwsTranscriptWithRetry(blob, mimeType, {
                maxWaitMs: browserText ? (state.agentSttMaxWaitMs || 12000) : 3500,
                maxAttempts: browserText ? 2 : 1,
            });
        }
        const picked = pickAgentTranscriptText(awsText, browserText);
        if (!picked.text) {
            notify('No speech detected. Tap the mic, speak clearly, then tap again to send.', 'warning');
            setReplyStatus('');
            updateReplyBar(state.lastConversation || [], state.sessionMode);
            return;
        }
        setLiveSpeechVisible(false);
        resetSpeechCapture();
        await sendMessage(picked.text, { viaVoice: true, sttSource: picked.source });
    }

    async function toggleAwsVoiceReply() {
        const btn = document.getElementById('vt-voice-reply-btn');
        const label = document.getElementById('vt-voice-reply-label');
        const pending = agentTurnPending(state.lastConversation || []);
        if (!state.isListening && (!pending || state.customerSpeaking || state.replyProcessing || state.conversationComplete)) {
            return;
        }

        if (state.isListening) {
            const elapsed = state.recordingStartedAt ? (Date.now() - state.recordingStartedAt) : 0;
            if (elapsed > 0 && elapsed < (state.minRecordingMs || 600)) {
                notify('Keep speaking a moment longer, then tap again to send.', 'info');
                return;
            }
            setReplyStatus('Finishing recording…', 'listening');
            try {
                if (state.mediaRecorder && state.mediaRecorder.state !== 'inactive') {
                    if (typeof state.mediaRecorder.requestData === 'function') {
                        state.mediaRecorder.requestData();
                    }
                    state.mediaRecorder.stop();
                }
            } catch {
                state.isListening = false;
                btn?.classList.remove('listening');
                setLiveSpeechVisible(false);
            }
            return;
        }

        if (typeof MediaRecorder === 'undefined') {
            return toggleBrowserVoiceReply();
        }

        await ensureAudioUnlocked();
        try {
            state.synth?.cancel?.();
        } catch { /* ignore */ }

        resetSpeechCapture();
        setLiveSpeechVisible(false);
        startBrowserCaptureParallel();
        try {
            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
            const mimeType = pickRecorderMimeType();
            const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
            state.mediaStream = stream;
            state.mediaRecorder = recorder;
            state.audioChunks = [];
            recorder.ondataavailable = (ev) => {
                if (ev.data?.size) state.audioChunks.push(ev.data);
            };
            recorder.onstop = async () => {
                stream.getTracks().forEach((t) => t.stop());
                state.mediaStream = null;
                state.mediaRecorder = null;
                const blob = new Blob(state.audioChunks, { type: recorder.mimeType || mimeType || 'audio/webm' });
                state.audioChunks = [];
                state.isListening = false;
                btn?.classList.remove('listening');
                if (label) label.textContent = defaultMicLabel(state.sessionMode);
                const browserText = await stopBrowserCaptureParallel();
                await transcribeAndSendRecording(blob, recorder.mimeType || mimeType, browserText);
            };
            recorder.start(250);
            state.recordingStartedAt = Date.now();
            state.isListening = true;
            btn?.classList.add('listening');
            btn?.classList.remove('your-turn');
            btn?.removeAttribute('disabled');
            if (label) label.textContent = 'Recording… tap again to send';
            setReplyStatus(
                'Speak your reply — live preview shows below. Tap mic again to send.',
                'listening',
            );
        } catch {
            state.isListening = false;
            await stopBrowserCaptureParallel();
            return toggleBrowserVoiceReply();
        }
    }

    function resetVoiceUiState() {
        state.customerSpeaking = false;
        state.replyProcessing = false;
        state.isListening = false;
        state.recognitionStopRequested = false;
        state.browserCaptureActive = false;
        state.mediaRecorder = null;
        state.audioChunks = [];
        if (state.mediaStream) {
            state.mediaStream.getTracks().forEach((t) => t.stop());
            state.mediaStream = null;
        }
        resetSpeechCapture();
        setLiveSpeechVisible(false);
        setCustomerSpeaking(false);
        setReplyProcessing(false);
    }

    function setMicEnabled(btn, enabled) {
        if (!btn) return;
        btn.disabled = !enabled;
        btn.classList.toggle('disabled', !enabled);
        if (enabled) {
            btn.removeAttribute('disabled');
        } else {
            btn.setAttribute('disabled', 'true');
        }
    }

    function mergedSpeechText() {
        return `${state.speechFinal || ''} ${state.speechInterim || ''}`.trim();
    }

    function updateSpeechPreview() {
        const preview = mergedSpeechText();
        state.speechBuffer = preview;
        const capturing = state.isListening || state.browserCaptureActive;
        if (capturing && preview) {
            setLiveSpeechVisible(true, preview);
        } else if (!capturing) {
            setLiveSpeechVisible(false);
        }
    }

    function commitSpeechInterim() {
        const interim = (state.speechInterim || '').trim();
        if (!interim) return;
        const cur = (state.speechFinal || '').trim();
        if (!cur) {
            state.speechFinal = interim;
        } else {
            const curL = cur.toLowerCase();
            const intL = interim.toLowerCase();
            if (curL === intL || curL.endsWith(intL) || intL.endsWith(curL)) {
                state.speechFinal = cur.length >= interim.length ? cur : interim;
            } else if (!curL.includes(intL) && !intL.includes(curL)) {
                state.speechFinal = `${cur} ${interim}`.trim();
            } else {
                state.speechFinal = cur.length >= interim.length ? cur : interim;
            }
        }
        state.speechFinal = dedupeRepeatedSpeech(state.speechFinal);
        state.speechInterim = '';
        updateSpeechPreview();
    }

    function dedupeRepeatedSpeech(text) {
        const t = (text || '').trim();
        if (t.length < 16) return t;
        const half = Math.floor(t.length / 2);
        const a = t.slice(0, half).trim();
        const b = t.slice(half).trim();
        if (a && b && a.toLowerCase() === b.toLowerCase()) return a;
        const words = t.split(/\s+/);
        if (words.length >= 8) {
            const mid = Math.floor(words.length / 2);
            const first = words.slice(0, mid).join(' ').toLowerCase();
            const second = words.slice(mid).join(' ').toLowerCase();
            if (first === second) return words.slice(0, mid).join(' ');
        }
        return t;
    }

    function appendSpeechFinal(part) {
        const p = (part || '').trim();
        if (!p) return;
        const cur = (state.speechFinal || '').trim();
        if (!cur) {
            state.speechFinal = p;
            return;
        }
        if (cur.toLowerCase() === p.toLowerCase() || cur.toLowerCase().endsWith(p.toLowerCase())) return;
        state.speechFinal = dedupeRepeatedSpeech(`${cur} ${p}`.trim());
    }

    function applyRecognitionResults(results, fromIndex = 0) {
        for (let i = fromIndex; i < results.length; i += 1) {
            const part = (results[i][0]?.transcript || '').trim();
            if (!part) continue;
            if (results[i].isFinal) {
                appendSpeechFinal(part);
            }
        }
        let interim = '';
        for (let i = 0; i < results.length; i += 1) {
            if (!results[i].isFinal) {
                const part = (results[i][0]?.transcript || '').trim();
                if (part) interim += (interim ? ' ' : '') + part;
            }
        }
        state.speechInterim = interim.trim();
        updateSpeechPreview();
    }

    function resetSpeechCapture() {
        state.speechFinal = '';
        state.speechInterim = '';
        state.speechBuffer = '';
    }

    async function finalizeSpeechCapture() {
        commitSpeechInterim();
        const hasAny = Boolean((state.speechFinal || state.speechInterim || mergedSpeechText()).trim());
        // Empty capture should fail quickly — only wait longer when something was heard
        await new Promise((resolve) => setTimeout(resolve, hasAny ? 450 : 120));
        commitSpeechInterim();
        const captured = dedupeRepeatedSpeech((state.speechFinal || mergedSpeechText()).trim());
        resetSpeechCapture();
        setLiveSpeechVisible(false);
        if (!captured) {
            setReplyStatus('');
            notify('No speech detected. Tap the mic, speak clearly, then tap again to send.', 'info');
            updateReplyBar(state.lastConversation || [], state.sessionMode);
            return;
        }
        await sendMessage(captured, { viaVoice: true });
    }

    function setReplyStatus(message, type = 'info') {
        const el = document.getElementById('vt-reply-status');
        if (!el) return;
        if (!message) {
            el.replaceChildren();
            el.className = 'vt-reply-status hidden';
            return;
        }
        // DOM nodes + textContent only — CodeQL-safe (no HTML from status strings)
        el.className = `vt-reply-status ${type || 'info'}`;
        el.replaceChildren();
        if (type === 'processing') {
            const spinner = document.createElement('span');
            spinner.className = 'vt-reply-status-spinner';
            spinner.setAttribute('aria-hidden', 'true');
            const text = document.createElement('span');
            text.className = 'vt-reply-status-text';
            text.textContent = message;
            el.append(spinner, text);
        } else {
            el.textContent = message;
        }
        el.classList.remove('hidden');
    }

    function isTransientReplyStatus(text) {
        return /^(Sending your reply|Processing your speech|Still processing speech|Finishing recording|Finishing capture)/i
            .test(String(text || '').trim());
    }

    function setReplyProcessing(active) {
        state.replyProcessing = active;
        const btn = document.getElementById('vt-voice-reply-btn');
        const icon = document.getElementById('vt-voice-reply-icon');
        const label = document.getElementById('vt-voice-reply-label');
        btn?.classList.toggle('processing', active);
        // Keep mic icon static — spinner only lives in the status bar above
        if (icon) {
            icon.className = 'fas fa-microphone vt-voice-icon';
        }
        if (active) {
            btn?.classList.remove('listening', 'your-turn');
            setReplyStatus('Sending your reply…', 'processing');
            if (label) label.textContent = 'Please wait…';
        } else {
            // Keep non-transient statuses (warnings, customer speaking, etc.)
            const el = document.getElementById('vt-reply-status');
            if (el && isTransientReplyStatus(el.textContent)) {
                setReplyStatus('');
            }
        }
        updateReplyBar(state.lastConversation || [], state.sessionMode);
    }

    function defaultMicLabel() {
        return 'Tap to speak — tap again when done';
    }

    function updateReplyBar(conversation, mode) {
        const btn = document.getElementById('vt-voice-reply-btn');
        const label = document.getElementById('vt-voice-reply-label');
        const turnHint = document.getElementById('vt-reply-turn-hint');
        const turnHintText = document.getElementById('vt-reply-turn-hint-text');
        const suggestedPanel = document.getElementById('vt-suggested-panel');
        const showSuggest = suggestedPanel && !suggestedPanel.classList.contains('hidden');
        const replyWrap = document.getElementById('vt-reply-dock');
        if (!btn || !label) return;
        if (state.conversationComplete) {
            replyWrap?.classList.add('hidden');
            turnHint?.classList.add('hidden');
            setMicEnabled(btn, false);
            return;
        }
        replyWrap?.classList.remove('hidden');
        const pending = agentTurnPending(conversation);
        const blockMic = !pending || state.customerSpeaking || state.replyProcessing;
        if (state.isListening) {
            setMicEnabled(btn, true);
        } else {
            setMicEnabled(btn, !blockMic);
        }
        btn.classList.toggle('your-turn', pending && !blockMic && !state.isListening);
        if (turnHint) {
            const showTurnHint = pending && !blockMic && !state.isListening && !showSuggest
                && mode === 'assessment';
            turnHint.classList.toggle('hidden', !showTurnHint);
            if (turnHintText && showTurnHint) {
                turnHintText.textContent = !conversation?.length
                    ? 'Your turn — tap the mic and speak your greeting'
                    : 'Your turn — tap the mic and respond';
            }
        }
        if (state.replyProcessing) {
            label.textContent = 'Please wait…';
        } else if (state.isListening) {
            label.textContent = 'Listening… tap again to send';
        } else if (!pending) {
            label.textContent = state.customerSpeaking ? 'Customer speaking…' : 'Listen to the customer…';
        } else if (!conversation?.length) {
            label.textContent = 'Tap to speak your opening greeting';
        } else {
            label.textContent = defaultMicLabel(mode);
        }
    }

    function agentTurnPending(conversation) {
        const last = lastSpeakerFromConversation(conversation);
        return !last || last === 'customer';
    }

    function lastSpeakerFromConversation(conversation) {
        if (!conversation?.length) return null;
        return conversation[conversation.length - 1].speaker;
    }

    function setCustomerSpeaking(active) {
        state.customerSpeaking = active;
        document.getElementById('vt-customer-speaking')?.classList.toggle('hidden', !active);
        document.getElementById('vt-customer-says-panel')?.classList.toggle('speaking', active);
        document.getElementById('vt-hear-customer-btn')?.toggleAttribute('disabled', active);
    }

    function updateSessionPanels(conversation, guidance, hints, mode) {
        state.lastConversation = conversation || [];
        let nextGuidance = guidance || {};
        // When the call ends, mark every SOP step complete in the checklist UI.
        if (state.conversationComplete && Array.isArray(nextGuidance.sop_progress) && nextGuidance.sop_progress.length) {
            nextGuidance = {
                ...nextGuidance,
                sop_progress: nextGuidance.sop_progress.map((s) => ({ ...s, completed: true })),
                current_step: nextGuidance.total_steps || nextGuidance.sop_progress.length,
            };
        }
        state.lastGuidance = nextGuidance;
        updateGuidance(nextGuidance, hints);
        updateSessionProgress(conversation, nextGuidance);
        const lastCustomer = [...(conversation || [])].reverse().find((t) => t.speaker === 'customer');
        if (lastCustomer?.text) {
            showCustomerSays(lastCustomer.text, guidance?.current_sentiment);
        } else {
            hideCustomerSays();
        }

        const suggestedPanel = document.getElementById('vt-suggested-panel');
        const suggested = document.getElementById('vt-suggested-response');
        const label = document.getElementById('vt-suggested-label');
        const hint = document.getElementById('vt-agent-turn-hint');
        const sopTag = document.getElementById('vt-suggested-sop-tag');
        const isAgentTurn = agentTurnPending(conversation);
        const suggestedText = guidance?.suggested_response;
        const showSuggest = mode === 'practice' && isAgentTurn && suggestedText;

        document.getElementById('vt-assessment-speak-panel')?.classList.toggle(
            'hidden',
            state.conversationComplete
            || mode !== 'assessment'
            || !isAgentTurn
            || state.customerSpeaking,
        );
        const assessSopTag = document.getElementById('vt-assessment-sop-tag');
        if (assessSopTag) {
            if (mode === 'assessment' && isAgentTurn && guidance?.current_sop?.name) {
                assessSopTag.textContent = `Current SOP: ${guidance.current_sop.name}`;
                assessSopTag.classList.remove('hidden');
            } else {
                assessSopTag.classList.add('hidden');
            }
        }

        if (showSuggest && suggested) {
            suggested.textContent = suggestedText;
            if (label) {
                label.textContent = !conversation?.length ? 'Your opening (read & speak)' : 'Your response (read & speak)';
            }
            if (hint) {
                hint.textContent = !conversation?.length
                    ? 'Read your greeting below, then tap the microphone to speak it.'
                    : 'Read the suggested line below, then tap the microphone to speak your reply.';
            }
            if (sopTag && guidance?.current_sop?.name) {
                sopTag.textContent = guidance.current_sop.name;
                sopTag.classList.remove('hidden');
            } else {
                sopTag?.classList.add('hidden');
            }
            suggestedPanel?.classList.remove('hidden');
        } else {
            suggestedPanel?.classList.add('hidden');
        }

        updateReplyBar(conversation, mode);
        if (!state.isListening && !state.browserCaptureActive) {
            setLiveSpeechVisible(false);
        }
    }

    function updateGuidance(guidance, hints) {
        const sopList = document.getElementById('vt-sop-list');
        if (sopList && guidance?.sop_progress) {
            const current = guidance.current_step || 1;
            sopList.innerHTML = guidance.sop_progress.map((s, i) => {
                const stepNum = i + 1;
                const isCurrent = stepNum === current;
                const expanded = state.expandedSopStep === stepNum;
                const cue = s.script_cue
                    ? `<p class="vt-sop-item-cue"><i class="fas fa-link"></i> Timeline: "${escapeHtml(s.script_cue.slice(0, 80))}${s.script_cue.length > 80 ? '…' : ''}"</p>`
                    : '';
                return `<li class="vt-sop-item ${s.completed ? 'done' : ''} ${isCurrent ? 'current' : ''} ${expanded ? 'expanded' : ''}" data-sop-step="${stepNum}" role="button" tabindex="0">
                    <span class="vt-sop-num">${stepNum}</span>
                    <div class="vt-sop-item-body">
                        <strong>${escapeHtml(s.name)}</strong>
                        ${s.description ? `<p class="vt-sop-item-desc">${escapeHtml(s.description)}</p>` : ''}
                        ${cue}
                    </div>
                    <i class="fas fa-chevron-down vt-sop-item-chevron" aria-hidden="true"></i>
                </li>`;
            }).join('');
            sopList.querySelectorAll('.vt-sop-item').forEach((item) => {
                const toggle = () => {
                    const step = Number(item.dataset.sopStep);
                    state.expandedSopStep = state.expandedSopStep === step ? null : step;
                    sopList.querySelectorAll('.vt-sop-item').forEach((el) => {
                        const n = Number(el.dataset.sopStep);
                        el.classList.toggle('expanded', n === state.expandedSopStep);
                    });
                };
                item.addEventListener('click', toggle);
                item.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        toggle();
                    }
                });
            });
        }
        const hintsList = document.getElementById('vt-hints-list');
        if (hintsList && Array.isArray(hints)) {
            if (hints.length) {
                hintsList.innerHTML = hints.map((h) => `<li>${escapeHtml(h)}</li>`).join('');
            } else {
                hintsList.innerHTML = '';
            }
        }
        const ring = document.getElementById('vt-sentiment-ring');
        if (ring && guidance?.current_sentiment) {
            const sentiment = String(guidance.current_sentiment).toLowerCase();
            ring.className = `vt-sentiment ${sentiment}`;
            ring.textContent = sentiment;
        }
    }

    async function sendMessage(text, options = {}) {
        if (!state.sessionId || !text?.trim()) return;
        const cleaned = dedupeRepeatedSpeech(text.trim());
        if (!cleaned) return;
        await ensureAudioUnlocked();
        const input = document.getElementById('vt-manual-input');
        if (input) input.value = '';
        setLiveSpeechVisible(false);
        resetSpeechCapture();
        setReplyProcessing(true);
        try {
            const res = await vtApi(`/sessions/${encodeURIComponent(state.sessionId)}/message`, 'POST', { text: cleaned });

            const conv = res.session?.conversation || [];
            state.lastConversation = conv;
            renderTranscript(conv, 'vt-transcript', state.sessionMode);
            const guidance = {
                ...(res.guidance || {}),
                current_sentiment: res.sentiment || res.guidance?.current_sentiment,
                suggested_response: res.suggested_response || res.guidance?.suggested_response,
            };
            updateSessionPanels(conv, guidance, res.hints, state.sessionMode);
            scrollTranscriptToBottom(false);

            // DF_S3: off-script reply — keep customer on the same beat
            if (res.script_hold) {
                notify(
                    res.script_hold_reason
                        || 'Stay on the script before the customer continues.',
                    'warning',
                );
                setReplyProcessing(false);
                setReplyStatus(res.script_hold_reason || 'Please retry with the suggested line.', 'warn');
                document.getElementById('vt-reply-dock')?.classList.remove('hidden');
                if (state.sessionMode === 'practice') {
                    document.getElementById('vt-suggested-panel')?.classList.remove('hidden');
                }
                scrollTranscriptToBottom(false);
                return;
            }

            if (res.customer_response) {
                state.lastCustomerAudio = {
                    audio: res.audio,
                    text: res.customer_response,
                    sentiment: res.sentiment,
                };
                document.getElementById('vt-suggested-panel')?.classList.add('hidden');
                setReplyProcessing(false);
                setReplyStatus('Customer is responding…', 'customer');
                scrollTranscriptToBottom(false);
                await playCustomerAudio(
                    res.audio,
                    res.customer_response,
                    res.sentiment,
                );
                setReplyStatus('');
                setLiveSpeechVisible(false);
                resetSpeechCapture();
                if (res.conversation_complete) {
                    state.conversationComplete = true;
                }
                updateSessionPanels(conv, guidance, res.hints, state.sessionMode);
                if (res.conversation_complete) {
                    showSessionCompleteBanner();
                } else {
                    state.conversationComplete = false;
                    document.getElementById('vt-reply-dock')?.classList.remove('hidden');
                }
                scrollTranscriptToBottom(false);
            }
        } catch (err) {
            notify(err?.message || 'Failed to send message.', 'error');
        } finally {
            setReplyProcessing(false);
        }
    }

    const TTS_DIGIT_WORDS = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine'];
    const TTS_EMAIL_RE = /[\w.+-]+@[\w-]+(?:\.[\w-]+)+/gi;
    const TTS_ORDINAL_WORDS = {
        1: 'first', 2: 'second', 3: 'third', 4: 'fourth', 5: 'fifth',
        6: 'sixth', 7: 'seventh', 8: 'eighth', 9: 'ninth', 10: 'tenth',
        11: 'eleventh', 12: 'twelfth', 13: 'thirteenth', 14: 'fourteenth', 15: 'fifteenth',
        16: 'sixteenth', 17: 'seventeenth', 18: 'eighteenth', 19: 'nineteenth', 20: 'twentieth',
        21: 'twenty-first', 22: 'twenty-second', 23: 'twenty-third', 24: 'twenty-fourth',
        25: 'twenty-fifth', 26: 'twenty-sixth', 27: 'twenty-seventh', 28: 'twenty-eighth',
        29: 'twenty-ninth', 30: 'thirtieth', 31: 'thirty-first',
    };
    const TTS_MONTH = (
        'January|February|March|April|May|June|July|August|September|October|November|December|'
        + 'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'
    );
    const TTS_MONTH_DAY_RE = new RegExp(
        `\\b((?:${TTS_MONTH})\\.?)\\s+([1-9]|[12]\\d|3[01])(?:st|nd|rd|th)?\\b`,
        'gi'
    );
    const TTS_DAY_MONTH_RE = new RegExp(
        `\\b([1-9]|[12]\\d|3[01])(?:st|nd|rd|th)?\\s+((?:${TTS_MONTH})\\.?)\\b`,
        'gi'
    );
    const TTS_ORDINAL_RE = /(?<!\d)\b([1-9]|[12]\d|3[01])(st|nd|rd|th)\b/gi;

    function emailToSpoken(email) {
        const at = email.indexOf('@');
        if (at < 0) return email;
        const spell = (part) => part
            .replace(/\./g, ' dot ')
            .replace(/_/g, ' underscore ')
            .replace(/-/g, ' hyphen ')
            .replace(/\+/g, ' plus ');
        return `${spell(email.slice(0, at))} at ${spell(email.slice(at + 1))}`;
    }

    function dayToOrdinalWord(day) {
        const n = Number(day);
        return TTS_ORDINAL_WORDS[n] || String(day);
    }

    function expandDatesForSpeech(text) {
        let s = String(text || '');
        if (!s.trim()) return s;
        // Reset /g lastIndex so repeated TTS calls stay correct.
        TTS_MONTH_DAY_RE.lastIndex = 0;
        TTS_DAY_MONTH_RE.lastIndex = 0;
        TTS_ORDINAL_RE.lastIndex = 0;
        s = s.replace(TTS_MONTH_DAY_RE, (_, month, day) => `${month} ${dayToOrdinalWord(day)}`);
        s = s.replace(TTS_DAY_MONTH_RE, (_, day, month) => `${dayToOrdinalWord(day)} ${month}`);
        s = s.replace(TTS_ORDINAL_RE, (_, day) => dayToOrdinalWord(day));
        return s;
    }

    function formatDigitsForSpeech(text) {
        let s = expandDatesForSpeech(text);
        s = s.replace(TTS_EMAIL_RE, (match) => emailToSpoken(match));
        return s.replace(/(?<!\$)(?<!\d)\d{2,}(?!\d)(?!\.\d)(?!(?:st|nd|rd|th)\b)/gi, (match) => (
            match.split('').map((d) => TTS_DIGIT_WORDS[Number(d)] || d).join(' ')
        ));
    }

    function pickVoice(speaker) {
        const voices = state.synth?.getVoices?.() || [];
        if (!voices.length) return null;
        if (speaker === 'customer') {
            return voices.find((v) => /zira|samantha|jenny|joanna|aria|susan|hazel|ruth|female/i.test(v.name) && v.lang.startsWith('en'))
                || voices.find((v) => v.lang.startsWith('en-GB') || v.lang.startsWith('en-US'));
        }
        return voices.find((v) => v.lang.startsWith('en'));
    }

    function speakTextAsync(text, speaker = 'customer', epoch = state.playbackEpoch) {
        if (!text || !state.synth) {
            return Promise.resolve();
        }
        if (!isPlaybackCurrent(epoch)) {
            return Promise.resolve();
        }
        const chunks = String(text).match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [text];
        state.synth.cancel();
        const speakChunk = (piece) => new Promise((resolve) => {
            if (!isPlaybackCurrent(epoch)) {
                resolve();
                return;
            }
            const u = new SpeechSynthesisUtterance(formatDigitsForSpeech(piece));
            u.lang = 'en-US';
            if (speaker === 'hint') {
                u.rate = 0.95;
                u.pitch = 1.0;
            } else {
                u.rate = speaker === 'customer' ? 0.92 : 1.0;
                u.pitch = speaker === 'customer' ? 1.02 : 1.0;
            }
            const voice = pickVoice(speaker);
            if (voice) u.voice = voice;
            u.onend = () => resolve();
            u.onerror = () => resolve();
            if (!isPlaybackCurrent(epoch)) {
                resolve();
                return;
            }
            state.synth.speak(u);
        });
        return chunks.reduce(
            (chain, chunk) => chain.then(() => {
                if (!isPlaybackCurrent(epoch)) return undefined;
                const piece = chunk.trim();
                return piece ? speakChunk(piece) : undefined;
            }),
            Promise.resolve(),
        );
    }

    function speakText(text, speaker = 'customer') {
        speakTextAsync(text, speaker).catch(() => {});
    }

    async function playAudioBase64(b64, epoch = state.playbackEpoch) {
        if (!isPlaybackCurrent(epoch)) return;
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        const blob = new Blob([bytes], { type: 'audio/mpeg' });
        const objUrl = URL.createObjectURL(blob);
        const audioEl = new Audio();
        audioEl.preload = 'auto';
        audioEl.playsInline = true;
        audioEl.src = objUrl;
        state.activeAudioEl = audioEl;
        state.activeAudioObjectUrl = objUrl;
        await new Promise((resolve, reject) => {
            if (!isPlaybackCurrent(epoch)) {
                stopActiveAudioElement();
                resolve();
                return;
            }
            const onReady = () => {
                audioEl.removeEventListener('canplaythrough', onReady);
                audioEl.removeEventListener('error', onError);
                resolve();
            };
            const onError = () => {
                audioEl.removeEventListener('canplaythrough', onReady);
                audioEl.removeEventListener('error', onError);
                stopActiveAudioElement();
                reject(new Error('Audio load failed'));
            };
            audioEl.addEventListener('canplaythrough', onReady);
            audioEl.addEventListener('error', onError);
            audioEl.load();
        });
        if (!isPlaybackCurrent(epoch)) {
            stopActiveAudioElement();
            return;
        }
        await new Promise((resolve, reject) => {
            audioEl.onended = () => {
                if (state.activeAudioEl === audioEl) {
                    state.activeAudioEl = null;
                    state.activeAudioObjectUrl = null;
                }
                URL.revokeObjectURL(objUrl);
                resolve();
            };
            audioEl.onerror = () => {
                stopActiveAudioElement();
                reject(new Error('Audio playback failed'));
            };
            const playPromise = audioEl.play();
            if (playPromise?.catch) playPromise.catch(reject);
        });
    }

    async function playCustomerAudio(audio, text, sentiment, options = {}) {
        if (!text) return;
        const epoch = state.playbackEpoch;
        showCustomerSays(text, sentiment);
        setCustomerSpeaking(true);
        const preferBrowser = Boolean(options.preferBrowser || audio?.use_browser_tts);
        try {
            if (!preferBrowser && audio?.audio_base64) {
                try {
                    await playAudioBase64(audio.audio_base64, epoch);
                    return;
                } catch {
                    if (!isPlaybackCurrent(epoch)) return;
                    /* fall through to browser TTS */
                }
            }
            if (!preferBrowser && audio?.audio_url) {
                try {
                    await playAudioUrl(audio.audio_url, epoch);
                    return;
                } catch {
                    if (!isPlaybackCurrent(epoch)) return;
                    /* fall through */
                }
            }
            await speakTextAsync(text, 'customer', epoch);
        } catch {
            if (!isPlaybackCurrent(epoch)) return;
            try {
                await speakTextAsync(text, 'customer', epoch);
            } catch {
                notify('Could not play customer voice. Read the customer line in conversation history.', 'warning');
            }
        } finally {
            if (isPlaybackCurrent(epoch)) {
                setCustomerSpeaking(false);
                updateReplyBar(state.lastConversation || [], state.sessionMode);
            }
        }
    }

    async function hearCustomerAgain() {
        const last = state.lastCustomerAudio;
        if (!last?.text) {
            notify('No customer line to replay yet.', 'info');
            return;
        }
        await playCustomerAudio(last.audio, last.text, last.sentiment);
    }

    async function playAudioUrl(url, epoch = state.playbackEpoch) {
        if (!isPlaybackCurrent(epoch)) return;
        const token = window.authToken || localStorage.getItem('authToken');
        const response = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
        if (!response.ok) throw new Error('Audio unavailable');
        if (!isPlaybackCurrent(epoch)) return;
        const blob = await response.blob();
        const objUrl = URL.createObjectURL(blob);
        const audioEl = new Audio();
        audioEl.preload = 'auto';
        audioEl.playsInline = true;
        audioEl.src = objUrl;
        state.activeAudioEl = audioEl;
        state.activeAudioObjectUrl = objUrl;
        await new Promise((resolve, reject) => {
            if (!isPlaybackCurrent(epoch)) {
                stopActiveAudioElement();
                resolve();
                return;
            }
            const onReady = () => {
                audioEl.removeEventListener('canplaythrough', onReady);
                audioEl.removeEventListener('error', onError);
                resolve();
            };
            const onError = () => {
                audioEl.removeEventListener('canplaythrough', onReady);
                audioEl.removeEventListener('error', onError);
                stopActiveAudioElement();
                reject(new Error('Audio load failed'));
            };
            audioEl.addEventListener('canplaythrough', onReady);
            audioEl.addEventListener('error', onError);
            audioEl.load();
        });
        if (!isPlaybackCurrent(epoch)) {
            stopActiveAudioElement();
            return;
        }
        await new Promise((resolve, reject) => {
            audioEl.onended = () => {
                if (state.activeAudioEl === audioEl) {
                    state.activeAudioEl = null;
                    state.activeAudioObjectUrl = null;
                }
                URL.revokeObjectURL(objUrl);
                resolve();
            };
            audioEl.onerror = () => {
                stopActiveAudioElement();
                reject(new Error('Audio playback failed'));
            };
            const playPromise = audioEl.play();
            if (playPromise?.catch) playPromise.catch(reject);
        });
    }

    async function toggleBrowserVoiceReply() {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) {
            notify('Speech recognition is not supported in this browser. Use Chrome or Edge.', 'warning');
            return;
        }
        const btn = document.getElementById('vt-voice-reply-btn');
        const label = document.getElementById('vt-voice-reply-label');
        const pending = agentTurnPending(state.lastConversation || []);
        if (!state.isListening && (!pending || state.customerSpeaking || state.replyProcessing || state.conversationComplete)) {
            return;
        }

        if (state.isListening) {
            state.recognitionStopRequested = true;
            commitSpeechInterim();
            setReplyStatus('Finishing capture…', 'listening');
            try {
                state.recognition?.stop();
            } catch {
                await finalizeSpeechCapture();
            }
            return;
        }

        await ensureAudioUnlocked();
        try {
            state.synth?.cancel?.();
        } catch { /* ignore */ }

        resetSpeechCapture();
        setLiveSpeechVisible(false);
        state.recognitionStopRequested = false;
        state.recognition = new SpeechRecognition();
        state.recognition.continuous = true;
        state.recognition.interimResults = true;
        state.recognition.lang = speechRecognitionLang();
        if ('maxAlternatives' in state.recognition) {
            state.recognition.maxAlternatives = 1;
        }

        state.recognition.onresult = (ev) => {
            applyRecognitionResults(ev.results, ev.resultIndex);
        };

        state.recognition.onerror = (ev) => {
            if (ev.error === 'no-speech') {
                if (!state.recognitionStopRequested) return;
            }
            if (ev.error === 'aborted') return;
            state.isListening = false;
            state.recognitionStopRequested = false;
            btn?.classList.remove('listening');
            setLiveSpeechVisible(false);
            const hint = ev.error === 'not-allowed'
                ? 'Microphone access blocked. Allow mic permission for this site in browser settings.'
                : ev.error === 'network'
                    ? 'Speech recognition needs an internet connection (Chrome uses Google servers).'
                    : 'Could not capture speech. Try Chrome/Edge, speak clearly, and tap the mic again.';
            notify(hint, 'warning');
            updateReplyBar(state.lastConversation || [], state.sessionMode);
        };

        state.recognition.onend = async () => {
            if (state.recognitionStopRequested) {
                state.isListening = false;
                state.recognitionStopRequested = false;
                btn?.classList.remove('listening');
                await finalizeSpeechCapture();
                return;
            }
            if (state.isListening) {
                try {
                    state.recognition.start();
                } catch {
                    /* recognition may already be restarting */
                }
            }
        };

        try {
            state.recognition.start();
            state.isListening = true;
            btn?.classList.add('listening');
            btn?.classList.remove('your-turn');
            btn?.removeAttribute('disabled');
            if (label) label.textContent = 'Listening… tap again to send';
            setReplyStatus(
                'Speak your reply — live preview shows below. Tap mic again to send.',
                'listening',
            );
        } catch {
            state.isListening = false;
            notify('Could not start microphone. Check browser permissions.', 'warning');
        }
    }

    async function toggleVoiceReply() {
        if (state.agentSttMode === 'aws_transcribe') {
            try {
                return await toggleAwsVoiceReply();
            } catch {
                notify('Switching to browser speech recognition.', 'info');
                return toggleBrowserVoiceReply();
            }
        }
        return toggleBrowserVoiceReply();
    }

    function hearSuggestedReply() {
        const text = document.getElementById('vt-suggested-response')?.textContent;
        if (text) speakText(text, 'hint');
    }

    function showSessionCompleteBanner() {
        const banner = document.getElementById('vt-session-complete-banner');
        const msg = document.getElementById('vt-session-complete-msg');
        const completeBtn = document.getElementById('vt-session-complete-end-btn');
        if (!banner) return;
        state.conversationComplete = true;
        document.getElementById('vt-assessment-speak-panel')?.classList.add('hidden');
        document.getElementById('vt-suggested-panel')?.classList.add('hidden');
        // Align turn counter to the turns actually spoken (filler may have been skipped).
        const conv = state.lastConversation || [];
        if (conv.length) {
            state.sessionExpectedTurns = conv.length;
        }
        updateReplyBar(conv, state.sessionMode);
        updateSessionProgress(conv, state.lastGuidance || {});
        // Ensure SOP checklist shows all steps done after the call ends.
        if (state.lastGuidance?.sop_progress?.length) {
            updateGuidance({
                ...state.lastGuidance,
                sop_progress: state.lastGuidance.sop_progress.map((s) => ({ ...s, completed: true })),
                current_step: state.lastGuidance.total_steps || state.lastGuidance.sop_progress.length,
            }, null);
        }
        const isAssessment = state.sessionMode === 'assessment';
        if (msg) {
            msg.textContent = isAssessment
                ? 'The customer has ended the conversation. Tap View results to see your contextual assessment score and download the PDF.'
                : 'Great job — this practice call is complete. Review the transcript above, then end the session when you are ready.';
        }
        if (completeBtn) {
            if (isAssessment) {
                completeBtn.classList.remove('hidden');
                completeBtn.innerHTML = '<i class="fas fa-chart-bar"></i> View results';
            } else {
                completeBtn.classList.remove('hidden');
                completeBtn.innerHTML = '<i class="fas fa-check"></i> End practice';
            }
        }
        const endBtn = document.getElementById('vt-end-session-btn');
        if (endBtn && isAssessment) {
            endBtn.innerHTML = '<i class="fas fa-chart-bar"></i> Submit for evaluation';
            endBtn.classList.add('pulse-cta');
        }
        document.getElementById('vt-voice-reply-btn') && setMicEnabled(document.getElementById('vt-voice-reply-btn'), false);
        setReplyStatus(isAssessment ? 'Call complete — view results when ready.' : 'Practice complete — submit practice when ready.', 'customer');
        banner.classList.remove('hidden');
        updateSessionHeaderActions(state.sessionMode || 'practice');
    }

    function hideSessionCompleteBanner() {
        document.getElementById('vt-session-complete-banner')?.classList.add('hidden');
        state.conversationComplete = false;
        const endBtn = document.getElementById('vt-end-session-btn');
        if (endBtn) {
            endBtn.classList.remove('pulse-cta');
        }
        updateSessionHeaderActions(state.sessionMode || 'practice');
    }

    async function endSession() {
        if (!state.sessionId) return;
        const endBtn = document.getElementById('vt-end-session-btn');
        if (state.sessionMode === 'assessment') {
            const agentTurns = traineeTurnCount(state.lastConversation);
            if (agentTurns < 1) {
                notify(
                    'Speak at least your opening greeting before submitting, or use Cancel Assessment to exit without a score.',
                    'warning',
                );
                return;
            }
            if (!customerWasAnswered(state.lastConversation)) {
                notify(
                    'Answer the customer at least once before submitting, or use Cancel Assessment to exit without a score.',
                    'warning',
                );
                return;
            }
            if (!state.conversationComplete && !window.confirm(
                'Submit this call before it is finished?\n\n'
                + 'Every score will be reduced in proportion to how much of the conversation you '
                + 'handled, and this attempt cannot be taken again. Keep going to answer the '
                + 'remaining turns first.',
            )) {
                return;
            }
            stopSessionActivity();
            try {
                if (endBtn) {
                    endBtn.disabled = true;
                    endBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Evaluating…';
                }
                const result = await vtApi('/assessments/evaluate', 'POST', { session_id: state.sessionId });
                state.resultsReturnTo = 'home';
                showResults(result);
            } catch (err) {
                notify(err?.message || 'Evaluation failed.', 'error');
            } finally {
                if (endBtn) {
                    endBtn.disabled = false;
                    updateSessionHeaderActions('assessment');
                    endBtn.classList.remove('pulse-cta');
                }
            }
        } else {
            if (!state.conversationComplete) {
                notify(
                    'Complete the full practice call (all turns) before submitting. Use Back to Scenarios to leave without counting it.',
                    'warning',
                );
                return;
            }
            const sessionId = state.sessionId;
            stopSessionActivity();
            if (endBtn) {
                endBtn.disabled = true;
                endBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Submitting…';
            }
            try {
                await vtApi(`/sessions/${encodeURIComponent(sessionId)}/complete`, 'POST');
                notify('Practice session submitted.', 'success');
            } catch (err) {
                notify(err?.message || 'Could not submit practice session.', 'error');
                if (endBtn) {
                    endBtn.disabled = false;
                    updateSessionHeaderActions('practice');
                }
                return;
            } finally {
                if (endBtn) {
                    endBtn.disabled = false;
                    updateSessionHeaderActions('practice');
                }
            }
            state.sessionId = null;
            state.sessionMode = null;
            state.scenarioId = null;
            state.lastConversation = [];
            hideSessionCompleteBanner();
            showVtSection('home');
            loadScenarios();
        }
    }

    function switchResultsTab(tab) {
        state.resultsTab = tab;
        document.querySelectorAll('.vt-results-tab').forEach((t) => {
            t.classList.toggle('active', t.dataset.tab === tab);
        });
        ['summary', 'evaluation', 'context', 'transcript'].forEach((name) => {
            document.getElementById(`vt-results-panel-${name}`)?.classList.toggle('hidden', name !== tab);
        });
    }

    function formatResultsDate(iso) {
        if (!iso) return '';
        if (typeof formatDateTimeIST === 'function') {
            return formatDateTimeIST(iso, '', false);
        }
        const d = parseVtIsoDate(iso);
        if (!d) return '';
        return d.toLocaleString('en-IN', {
            timeZone: 'Asia/Kolkata',
            day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
        });
    }

    function weakTurnLines(turnAnalysis, limit = 3) {
        return (turnAnalysis || [])
            .filter((t) => !t.passed || (t.intent_pct || 0) < 40)
            .slice(0, limit)
            .map((t) => `Turn #${t.turn_index}: ${t.feedback} (${(t.combined_pct || 0).toFixed(1)}% combined)`);
    }

    function avgContextMatch(turnAnalysis) {
        const rows = (turnAnalysis || []).filter((t) => !t.post_script);
        if (!rows.length) return 0;
        return rows.reduce((s, t) => s + (t.combined_pct || 0), 0) / rows.length;
    }

    function avgCombinedMatch(result) {
        const meta = result?.metadata || {};
        if (meta.avg_combined_match_pct != null) return meta.avg_combined_match_pct;
        return avgContextMatch(meta.turn_analysis || []);
    }

    function renderMetricParamCard(metric, turnAnalysis) {
        const pct = Math.round((metric.score / 5) * 100);
        const evidence = weakTurnLines(turnAnalysis, 2);
        const coaching = METRIC_COACHING[metric.parameter];
        const check = METRIC_CHECKS[metric.parameter] || '';
        const weightPct = ((metric.weight || 0) * 100).toFixed(1);
        return `
            <div class="vt-eval-param-card ${metric.passed ? 'pass' : 'needs-work'}">
                <div class="vt-eval-param-head">
                    <strong>${escapeHtml(metric.parameter)}</strong>
                    <span class="vt-eval-param-status ${metric.passed ? 'pass' : 'fail'}">${metric.passed ? '✓ PASS' : '✗ NEEDS WORK'}</span>
                </div>
                <div class="vt-eval-param-bar"><div style="width:${pct}%"></div></div>
                <div class="vt-eval-param-meta">Weight ${weightPct}% · Contrib ${(metric.weighted_score || 0).toFixed(2)}</div>
                ${check ? `<p class="vt-eval-param-check"><strong>Checked by:</strong> ${escapeHtml(check)}</p>` : ''}
                <div class="vt-eval-your-result">
                    <span>Your result</span>
                    <p>${escapeHtml(metric.remarks || '')}</p>
                </div>
                ${evidence.length && !metric.passed ? `
                    <div class="vt-eval-evidence">
                        <strong>Evidence from your call</strong>
                        <ul>${evidence.map((e) => `<li>${escapeHtml(e)}</li>`).join('')}</ul>
                    </div>` : ''}
                ${coaching && !metric.passed ? `
                    <div class="vt-eval-coaching">
                        <i class="fas fa-lightbulb"></i>
                        <div><strong>Coaching tip</strong><p>${escapeHtml(coaching)}</p></div>
                    </div>` : ''}
                ${['Intent Accuracy', 'Conversation Flow', 'Empathy'].includes(metric.parameter) ? `
                    <button type="button" class="vt-eval-jump-context" data-metric="${escapeHtml(metric.parameter)}">See turn-by-turn context for this metric →</button>` : ''}
            </div>`;
    }

    function renderEvaluationPanel(result) {
        const metrics = result.metrics || [];
        const passed = metrics.filter((m) => m.passed).length;
        const below = metrics.length - passed;
        const passRate = metrics.length ? Math.round((passed / metrics.length) * 100) : 0;
        const ctxMatch = Math.round(avgCombinedMatch(result));
        const filter = state.evalMetricFilter;
        const catFilter = state.evalCategoryFilter;

        let filtered = metrics;
        if (filter === 'passed') filtered = filtered.filter((m) => m.passed);
        if (filter === 'needs') filtered = filtered.filter((m) => !m.passed);
        if (catFilter !== 'all') filtered = filtered.filter((m) => m.category === catFilter);

        const categoryPills = EVAL_GROUPS.map((g) => {
            const items = metrics.filter((m) => m.category === g.category);
            const avg = items.length ? (items.reduce((s, m) => s + m.score, 0) / items.length).toFixed(1) : '—';
            return `<button type="button" class="vt-eval-cat-pill ${catFilter === g.category ? 'active' : ''}" data-cat="${escapeHtml(g.category)}">${escapeHtml(g.category)} (${avg})</button>`;
        }).join('');

        const groupsHtml = EVAL_GROUPS.filter((g) => catFilter === 'all' || g.category === catFilter).map((g) => {
            const items = metrics.filter((m) => g.params.includes(m.parameter));
            if (!items.length) return '';
            const avg = (items.reduce((s, m) => s + m.score, 0) / items.length).toFixed(1);
            const passCount = items.filter((m) => m.passed).length;
            const turnAnalysis = result.metadata?.turn_analysis || [];
            return `
                <section class="vt-eval-group ${items.some((m) => !m.passed) ? 'has-needs-work' : ''}">
                    <header class="vt-eval-group-head">
                        <div>
                            <h4><i class="fas ${g.icon}"></i> ${escapeHtml(g.category)}</h4>
                            <p class="vt-eval-question">${escapeHtml(g.question)}</p>
                            <p class="vt-eval-check"><strong>What we check:</strong> ${escapeHtml(g.check)}</p>
                        </div>
                        <div class="vt-eval-group-score">
                            <span class="vt-eval-avg">${avg}</span>
                            <span class="vt-eval-avg-lbl">avg / 5</span>
                            <span class="vt-eval-pass-count">${passCount}/${items.length} passed</span>
                        </div>
                    </header>
                    <div class="vt-eval-param-grid">
                        ${items.map((m) => renderMetricParamCard(m, turnAnalysis)).join('')}
                    </div>
                </section>`;
        }).join('');

        return `
            <div class="vt-eval-overview">
                <div class="vt-eval-pass-ring" style="--pass-pct:${passRate}">
                    <span class="vt-eval-pass-val">${passRate}%</span>
                    <span class="vt-eval-pass-lbl">PASS RATE</span>
                </div>
                <div class="vt-eval-stat-cards">
                    <div class="vt-eval-stat"><strong>${passed}</strong><span>PASSED (≥ 3/5)</span></div>
                    <div class="vt-eval-stat warn"><strong>${below}</strong><span>BELOW THRESHOLD</span></div>
                    <div class="vt-eval-stat"><strong>${(result.final_score || 0).toFixed(1)}</strong><span>WEIGHTED / 5</span></div>
                    <div class="vt-eval-stat"><strong>${ctxMatch}%</strong><span>CONTEXT MATCH</span></div>
                </div>
                <button type="button" class="vt-eval-jump-needs" id="vt-jump-needs-work">Jump to needs work →</button>
            </div>
            <div class="vt-eval-filters">
                <button type="button" class="vt-eval-filter-btn ${filter === 'all' ? 'active' : ''}" data-filter="all">All ${metrics.length}</button>
                <button type="button" class="vt-eval-filter-btn ${filter === 'passed' ? 'active' : ''}" data-filter="passed">Passed ${passed}</button>
                <button type="button" class="vt-eval-filter-btn ${filter === 'needs' ? 'active' : ''}" data-filter="needs">Needs work ${below}</button>
            </div>
            <div class="vt-eval-cat-pills">
                <button type="button" class="vt-eval-cat-pill ${catFilter === 'all' ? 'active' : ''}" data-cat="all">All</button>
                ${categoryPills}
            </div>
            ${groupsHtml || '<p class="placeholder-text">No metrics match the current filter.</p>'}
        `;
    }

    function bindEvaluationPanelEvents(result) {
        const panel = document.getElementById('vt-results-panel-evaluation');
        if (!panel) return;
        panel.querySelectorAll('.vt-eval-filter-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                state.evalMetricFilter = btn.dataset.filter;
                panel.innerHTML = renderEvaluationPanel(result);
                bindEvaluationPanelEvents(result);
            });
        });
        panel.querySelectorAll('.vt-eval-cat-pill').forEach((btn) => {
            btn.addEventListener('click', () => {
                state.evalCategoryFilter = btn.dataset.cat;
                panel.innerHTML = renderEvaluationPanel(result);
                bindEvaluationPanelEvents(result);
            });
        });
        panel.querySelector('#vt-jump-needs-work')?.addEventListener('click', () => {
            state.evalMetricFilter = 'needs';
            state.evalCategoryFilter = 'all';
            panel.innerHTML = renderEvaluationPanel(result);
            bindEvaluationPanelEvents(result);
            panel.querySelector('.vt-eval-param-card.needs-work, .vt-eval-group.has-needs-work')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
        panel.querySelectorAll('.vt-eval-jump-context').forEach((btn) => {
            btn.addEventListener('click', () => {
                switchResultsTab('context');
                renderResultsPanels(result);
            });
        });
    }

    function renderResultsSopChecklist(sopActs) {
        if (!sopActs?.length) return '';
        const rows = sopActs.map((act, i) => {
            const done = Boolean(act.completed);
            return `<tr class="${done ? 'done' : 'missed'}">
                <td>${i + 1}</td>
                <td>${escapeHtml(act.name || '')}</td>
                <td>${escapeHtml(act.description || '')}</td>
                <td>${act.required === false ? 'No' : 'Yes'}</td>
                <td><span class="vt-sop-status ${done ? 'pass' : 'fail'}">${done ? '✓ COMPLETED' : '✗ MISSED'}</span></td>
            </tr>`;
        }).join('');
        return `
            <div class="vt-results-sop-checklist">
                <h4><i class="fas fa-list-check"></i> SOP Compliance (same as PDF)</h4>
                <table class="vt-results-sop-table">
                    <thead><tr><th>#</th><th>Activity</th><th>Description</th><th>Required</th><th>Status</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }

    function renderResultsPanels(result) {
        const meta = result.metadata || {};
        const summary = meta.transcript_summary || {};
        const turnAnalysis = meta.turn_analysis || [];
        const sopActs = meta.sop_activities || [];
        const passedTurns = turnAnalysis.filter((t) => t.passed).length;
        const conv = result.conversation || [];
        const agentTurns = conv.filter((t) => t.speaker === 'trainee' || t.speaker === 'agent').length;
        const ctxMatch = Math.round(avgCombinedMatch(result));
        const cat = categoryMeta({ category: result.category, custom_category_label: result.custom_category_label });
        const sentStart = (result.sentiment_start?.value || result.sentiment_start || 'neutral').toLowerCase();
        const sentEnd = (result.sentiment_end?.value || result.sentiment_end || 'neutral').toLowerCase();
        const completedSop = sopActs.filter((a) => a.completed).map((a) => a.name);
        const missedSop = sopActs.filter((a) => a.required !== false && !a.completed).map((a) => a.name);

        const summaryPanel = document.getElementById('vt-results-panel-summary');
        if (summaryPanel) {
            summaryPanel.innerHTML = `
                <div class="vt-results-kpi-row">
                    <div class="vt-results-kpi"><i class="fas fa-check-circle"></i><span>SOP Compliance</span><strong>${result.sop_compliance_rate || 0}%</strong></div>
                    <div class="vt-results-kpi"><i class="fas fa-bullseye"></i><span>First Contact Resolution</span><strong>${result.fcr_achieved ? 'Yes' : 'No'}</strong></div>
                    <div class="vt-results-kpi"><i class="fas fa-face-smile"></i><span>Customer Sentiment</span><strong>${escapeHtml(sentStart)} → ${escapeHtml(sentEnd)}</strong></div>
                    <div class="vt-results-kpi"><i class="fas fa-crosshairs"></i><span>Combined Match</span><strong>${ctxMatch}%</strong></div>
                    <div class="vt-results-kpi"><i class="fas fa-comments"></i><span>Responses / Turns</span><strong>${agentTurns}/${conv.length}</strong></div>
                </div>
                <div class="vt-results-narrative">
                    <h4><i class="fas fa-book-open"></i> Call summary</h4>
                    <p>${escapeHtml(summary.narrative || result.remarks || '')}</p>
                    ${summary.call_outcome ? `<p class="vt-results-outcome"><i class="fas fa-flag-checkered"></i> <strong>Outcome:</strong> ${escapeHtml(summary.call_outcome)}</p>` : ''}
                </div>
                ${renderResultsSopChecklist(sopActs)}
                ${completedSop.length ? `<div class="vt-results-sop-pills"><span>Completed SOP steps:</span> ${completedSop.map((n) => `<span class="vt-sop-pill done">${escapeHtml(n)}</span>`).join('')}</div>` : ''}
                ${missedSop.length ? `<div class="vt-results-sop-pills missed"><span>Missed SOP steps:</span> ${missedSop.map((n) => `<span class="vt-sop-pill missed">${escapeHtml(n)}</span>`).join('')}</div>` : ''}
                <div class="vt-results-highlight-pills">
                    ${[...new Set((result.metrics || []).filter((m) => m.passed).map((m) => m.category))].slice(0, 4).map((cat) => `<span class="vt-highlight-pill pass">${escapeHtml(cat)}</span>`).join('')}
                    ${(result.metrics || []).filter((m) => !m.passed).map((m) => `<span class="vt-highlight-pill warn">Improve: ${escapeHtml(m.parameter)}</span>`).join('')}
                    <span class="vt-highlight-pill info">Contextual turns passed: ${passedTurns}/${turnAnalysis.length || '—'}</span>
                </div>
                <div class="vt-results-transcript-snap">
                    <div class="vt-results-snap-head">
                        <h4>Transcript Snapshot</h4>
                        <button type="button" class="vt-link-btn" id="vt-goto-full-transcript">View full transcript →</button>
                    </div>
                    <div id="vt-results-snap-inner" class="vt-transcript compact"></div>
                </div>
            `;
            renderTranscript(conv.slice(0, 6), 'vt-results-snap-inner');
            document.getElementById('vt-goto-full-transcript')?.addEventListener('click', () => switchResultsTab('transcript'));
        }

        const evalPanel = document.getElementById('vt-results-panel-evaluation');
        if (evalPanel) {
            evalPanel.innerHTML = renderEvaluationPanel(result);
            bindEvaluationPanelEvents(result);
        }

        const contextPanel = document.getElementById('vt-results-panel-context');
        if (contextPanel) {
            if (!turnAnalysis.length) {
                contextPanel.innerHTML = '<p class="placeholder-text">No turn-by-turn context analysis available.</p>';
            } else {
                contextPanel.innerHTML = `
                    <p class="vt-results-turn-intro">Each agent turn is scored on meaning and context — exact script wording is not required. Paraphrasing is allowed when intent matches.</p>
                    <div class="vt-results-turn-cards">
                        ${turnAnalysis.map((t) => `
                            <div class="vt-results-turn-card ${t.passed ? 'pass' : 'warn'} ${t.post_script ? 'closing' : ''}" id="vt-turn-card-${t.turn_index}">
                                <div class="vt-results-turn-head">
                                    <strong>${t.post_script ? 'Closing' : `Turn #${t.turn_index}`}</strong>
                                    <span class="vt-results-turn-score">${(t.combined_pct || 0).toFixed(1)}% combined</span>
                                </div>
                                <div class="vt-results-turn-bars">
                                    <span title="Context">Ctx ${t.context_match_pct || 0}%</span>
                                    <span title="Response">Rsp ${t.response_relevance_pct || 0}%</span>
                                    <span title="Intent">Int ${t.intent_pct || 0}%</span>
                                </div>
                                ${t.reference_text ? `<p class="vt-results-ref-line"><span>Reference cue:</span> ${escapeHtml(t.reference_text)}</p>` : ''}
                                ${t.customer_context ? `<p class="vt-results-customer-ctx"><span>Customer said:</span> ${escapeHtml(t.customer_context)}</p>` : ''}
                                <p class="vt-results-agent-said"><span>You said:</span> ${escapeHtml(t.trainee_text)}</p>
                                <p class="vt-results-turn-feedback">${escapeHtml(t.feedback)}</p>
                            </div>
                        `).join('')}
                    </div>`;
            }
        }

        const transcriptPanel = document.getElementById('vt-results-panel-transcript');
        if (transcriptPanel) {
            transcriptPanel.innerHTML = `
                <div class="vt-results-transcript-overview">
                    <p>${escapeHtml(summary.narrative || result.remarks || '')}</p>
                    <div class="vt-results-transcript-stats">
                        <div><strong>${conv.length}</strong><span>Total messages</span></div>
                        <div><strong>${agentTurns}</strong><span>Your responses</span></div>
                        <div><strong>${conv.length - agentTurns}</strong><span>Customer messages</span></div>
                        <div><strong>${escapeHtml(sentStart)} → ${escapeHtml(sentEnd)}</strong><span>Sentiment arc</span></div>
                    </div>
                </div>
                <div id="vt-results-transcript-inner" class="vt-transcript chat-style"></div>`;
            renderTranscript(conv, 'vt-results-transcript-inner');
        }
    }

    function showResults(result) {
        stopAllPlayback();
        state.lastAssessmentResult = result;
        state.resultsTab = 'summary';
        state.evalMetricFilter = 'all';
        state.evalCategoryFilter = 'all';
        hideSessionCompleteBanner();
        showVtSection('results');
        updateResultsBackButton();

        const titleEl = document.getElementById('vt-results-title');
        if (titleEl) titleEl.textContent = result.scenario_title || 'Assessment Results';

        const tagsEl = document.getElementById('vt-results-tags');
        const cat = categoryMeta({ category: result.category, custom_category_label: result.custom_category_label });
        const outcome = result.metadata?.transcript_summary?.call_outcome || (result.fcr_achieved ? 'Resolved' : 'In progress');
        if (tagsEl) {
            tagsEl.innerHTML = `
                <span class="vt-results-tag cat"><i class="fas ${cat.icon}"></i> ${escapeHtml(cat.label)}</span>
                <span class="vt-results-tag date">${escapeHtml(formatResultsDate(result.created_at))}</span>
                <span class="vt-results-tag outcome">${escapeHtml(outcome)}</span>
            `;
        }

        const gauge = document.getElementById('vt-results-score-gauge');
        if (gauge) {
            gauge.innerHTML = `
                <div class="vt-grade-ring grade-${escapeHtml(result.grade)}">${escapeHtml(result.grade)}</div>
                <div class="vt-grade-details">
                    <strong>${result.final_percentage}%</strong>
                    <span>${(result.final_score || 0).toFixed(1)} / 5.0 overall</span>
                </div>`;
        }

        const turnAnalysis = result.metadata?.turn_analysis || [];
        const conv = result.conversation || [];
        const evalCount = document.getElementById('vt-results-count-eval');
        const ctxCount = document.getElementById('vt-results-count-ctx');
        const transCount = document.getElementById('vt-results-count-trans');
        if (evalCount) evalCount.textContent = `(${(result.metrics || []).length})`;
        if (ctxCount) ctxCount.textContent = `(${turnAnalysis.length})`;
        if (transCount) transCount.textContent = `(${conv.length})`;

        document.getElementById('vt-try-another-btn')?.classList.toggle('hidden', state.resultsReturnTo === 'admin');

        const breadcrumb = document.getElementById('vt-results-breadcrumb');
        if (breadcrumb) {
            breadcrumb.textContent = state.resultsReturnTo === 'admin'
                ? 'Super Admin › Voice Training Reports'
                : 'Voice Training Agent › Scenarios';
        }
        updateResultsBackButton();

        switchResultsTab('summary');
        renderResultsPanels(result);
        const pdfBtn = document.getElementById('vt-download-pdf-btn');
        if (pdfBtn) {
            if (result.assessment_id) {
                pdfBtn.dataset.assessmentId = result.assessment_id;
                pdfBtn.classList.remove('hidden');
                pdfBtn.disabled = false;
            } else {
                delete pdfBtn.dataset.assessmentId;
                pdfBtn.classList.add('hidden');
            }
        }
        state.sessionId = null;
    }

    async function downloadAssessmentPdfById(assessmentId) {
        if (!assessmentId) return;
        try {
            if (typeof window.showLoader === 'function') window.showLoader('Preparing PDF...');
            const blob = await vtApiBlob(`/assessments/${encodeURIComponent(assessmentId)}/pdf`);
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${assessmentId}_report.pdf`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (err) {
            notify(err?.message || 'Could not download PDF.', 'error');
        } finally {
            if (typeof window.hideLoader === 'function') window.hideLoader();
        }
    }

    async function downloadAssessmentPdf() {
        const assessmentId = document.getElementById('vt-download-pdf-btn')?.dataset?.assessmentId;
        await downloadAssessmentPdfById(assessmentId);
    }

    async function viewAdminAssessment(assessmentId) {
        if (!assessmentId) return;
        try {
            if (typeof window.showLoader === 'function') window.showLoader('Loading assessment...');
            const data = await vtApi(`/assessments/${encodeURIComponent(assessmentId)}`);
            state.resultsReturnTo = 'admin';
            if (typeof window.switchView === 'function') {
                await window.switchView('voice-training');
            }
            showResults(data);
        } catch (err) {
            notify(err?.message || 'Could not open assessment results.', 'error');
        } finally {
            if (typeof window.hideLoader === 'function') window.hideLoader();
        }
    }

    function filterAdminScenarios(rows) {
        const q = (state.adminSearchQuery || '').trim().toLowerCase();
        if (!q) return rows;
        return rows.filter((r) => {
            const haystack = [
                r.title,
                r.category,
                r.author_email,
                r.author_role,
                r.process_name,
            ].filter(Boolean).join(' ').toLowerCase();
            return haystack.includes(q);
        });
    }

    function filterAdminAssessments(rows) {
        const q = (state.adminSearchQuery || '').trim().toLowerCase();
        if (!q) return rows;
        return rows.filter((r) => {
            const haystack = [
                r.scenario_title,
                r.user_email,
                r.user_role,
                r.process_name,
                r.grade,
            ].filter(Boolean).join(' ').toLowerCase();
            return haystack.includes(q);
        });
    }

    function renderAdminScenariosTable(scenarios) {
        const rows = filterAdminScenarios(scenarios);
        if (!rows.length) {
            return '<p class="placeholder-text">No scenarios match the current filters.</p>';
        }
        const selected = new Set(state.adminSelectedScenarios || []);
        const allChecked = rows.length > 0 && rows.every((r) => selected.has(r.id));
        const archivedSelected = rows.filter((r) => selected.has(r.id) && r.archived);
        const activeSelected = rows.filter((r) => selected.has(r.id) && !r.archived);
        const bulkBar = selected.size
            ? `<div class="vt-admin-bulk-bar">
                <span><strong>${selected.size}</strong> selected</span>
                <div class="vt-admin-bulk-actions">
                    ${activeSelected.length ? `<button type="button" class="btn btn-secondary btn-sm" id="vt-admin-bulk-archive"><i class="fas fa-box-archive"></i> Archive (${activeSelected.length})</button>` : ''}
                    ${archivedSelected.length ? `<button type="button" class="btn btn-danger btn-sm" id="vt-admin-bulk-permanent-delete"><i class="fas fa-trash"></i> Delete permanently (${archivedSelected.length})</button>` : ''}
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-admin-clear-scenario-selection">Clear</button>
                </div>
               </div>`
            : '';
        return `${bulkBar}<table class="admin-table vt-admin-table"><thead><tr>
            <th class="vt-admin-check-col"><input type="checkbox" id="vt-admin-select-all-scenarios" ${allChecked ? 'checked' : ''} aria-label="Select all scenarios" /></th>
            <th>Date</th><th>Title</th><th>Category</th><th>Created by</th><th>Role</th><th>Process</th><th>Turns</th><th>Archive</th><th>Publish</th><th>Actions</th>
        </tr></thead><tbody>${rows.map((r) => `
            <tr class="${r.archived ? 'vt-admin-row-archived' : ''}">
                <td class="vt-admin-check-col"><input type="checkbox" class="vt-admin-scenario-check" data-id="${escapeHtml(r.id)}" ${selected.has(r.id) ? 'checked' : ''} aria-label="Select scenario" /></td>
                <td>${formatVtDate(r.created_at)}</td>
                <td><strong>${escapeHtml(r.title || '')}</strong></td>
                <td>${escapeHtml((r.category || '').replace(/_/g, ' '))}</td>
                <td>${escapeHtml(r.author_email || '')}</td>
                <td>${roleBadge(r.author_role)}</td>
                <td>${escapeHtml(r.process_name || '—')}</td>
                <td>${r.turn_count || 0}</td>
                <td>${r.archived ? '<span class="vt-archived-tag">Archived</span>' : '<span class="vt-active-tag">Active</span>'}</td>
                <td>${r.is_published
                    ? '<span class="vt-published-tag"><i class="fas fa-share-square"></i> Published</span>'
                    : '<span class="vt-draft-tag"><i class="fas fa-lock"></i> Not published</span>'}</td>
                <td class="vt-admin-actions">
                    <button type="button" class="btn btn-secondary btn-sm vt-admin-preview-scenario" data-id="${escapeHtml(r.id)}" title="Preview scenario"><i class="fas fa-eye"></i></button>
                    <button type="button" class="btn btn-secondary btn-sm vt-admin-edit-scenario" data-id="${escapeHtml(r.id)}" title="Edit script"><i class="fas fa-pen"></i></button>
                    ${r.archived
                        ? `<button type="button" class="btn btn-secondary btn-sm vt-admin-restore-scenario" data-id="${escapeHtml(r.id)}" data-title="${escapeHtml(r.title || '')}" title="Restore scenario"><i class="fas fa-rotate-left"></i></button>
                           <button type="button" class="btn btn-danger btn-sm vt-admin-permanent-delete-scenario" data-id="${escapeHtml(r.id)}" data-title="${escapeHtml(r.title || '')}" title="Delete permanently"><i class="fas fa-trash"></i></button>`
                        : `<button type="button" class="btn btn-danger btn-sm vt-admin-delete-scenario" data-id="${escapeHtml(r.id)}" data-title="${escapeHtml(r.title || '')}" title="Archive & notify owner"><i class="fas fa-trash"></i></button>`}
                </td>
            </tr>`).join('')}</tbody></table>`;
    }

    function renderAdminAssessmentsTable(assessments) {
        const rows = filterAdminAssessments(assessments);
        if (!rows.length) {
            return '<p class="placeholder-text">No graded assessments match the current filters.</p>';
        }
        const selected = new Set(state.adminSelectedAssessments || []);
        const allChecked = rows.length > 0 && rows.every((r) => selected.has(r.assessment_id));
        const bulkBar = selected.size
            ? `<div class="vt-admin-bulk-bar">
                <span><strong>${selected.size}</strong> selected</span>
                <div class="vt-admin-bulk-actions">
                    <button type="button" class="btn btn-danger btn-sm" id="vt-admin-bulk-delete-assessments"><i class="fas fa-trash"></i> Delete (${selected.size})</button>
                    <button type="button" class="btn btn-secondary btn-sm" id="vt-admin-clear-assessment-selection">Clear</button>
                </div>
               </div>`
            : '';
        return `${bulkBar}<table class="admin-table vt-admin-table"><thead><tr>
            <th class="vt-admin-check-col"><input type="checkbox" id="vt-admin-select-all-assessments" ${allChecked ? 'checked' : ''} aria-label="Select all assessments" /></th>
            <th>Date</th><th>User</th><th>Role</th><th>Scenario</th><th>Attempt</th><th>Grade</th><th>Score</th><th>Process</th><th>Actions</th>
        </tr></thead><tbody>${rows.map((r) => `
            <tr class="${r.passed ? 'vt-assessment-passed-row' : ''}">
                <td class="vt-admin-check-col"><input type="checkbox" class="vt-admin-assessment-check" data-id="${escapeHtml(r.assessment_id || '')}" ${selected.has(r.assessment_id) ? 'checked' : ''} aria-label="Select assessment" /></td>
                <td>${formatVtDate(r.created_at)}</td>
                <td>${escapeHtml(r.user_email || '')}</td>
                <td>${roleBadge(r.user_role)}</td>
                <td>${escapeHtml(r.scenario_title || '')}</td>
                <td>${r.attempt_number ? `#${r.attempt_number}${(r.attempt_total || 0) > 1 ? ` / ${r.attempt_total}` : ''}` : '—'}${r.passed ? ' <span class="vt-pass-tag">Pass</span>' : ''}</td>
                <td><span class="vt-grade-pill grade-${escapeHtml(r.grade || '')}">${escapeHtml(r.grade || '—')}</span></td>
                <td>${r.final_percentage || 0}%</td>
                <td>${escapeHtml(r.process_name || '—')}</td>
                <td class="vt-admin-actions">
                    <button type="button" class="btn btn-primary btn-sm vt-admin-view-assessment" data-id="${escapeHtml(r.assessment_id || '')}" title="View full results"><i class="fas fa-chart-bar"></i> View</button>
                    ${r.assessment_id ? `<button type="button" class="btn btn-secondary btn-sm vt-admin-download-pdf" data-id="${escapeHtml(r.assessment_id)}" title="Download PDF"><i class="fas fa-file-pdf"></i></button>` : ''}
                    ${r.assessment_id ? `<button type="button" class="btn btn-danger btn-sm vt-admin-delete-assessment" data-id="${escapeHtml(r.assessment_id)}" data-title="${escapeHtml(r.scenario_title || '')}" title="Delete assessment"><i class="fas fa-trash"></i></button>` : ''}
                </td>
            </tr>`).join('')}</tbody></table>`;
    }

    function bindAdminReportActions(container) {
        const cache = state.adminReportCache;
        const scenRows = cache ? filterAdminScenarios(cache.scenarios) : [];
        const assessRows = cache ? filterAdminAssessments(cache.assessments) : [];

        container.querySelectorAll('.vt-admin-scenario-check').forEach((cb) => {
            cb.addEventListener('change', () => {
                const id = cb.dataset.id;
                const set = new Set(state.adminSelectedScenarios || []);
                if (cb.checked) set.add(id);
                else set.delete(id);
                state.adminSelectedScenarios = [...set];
                rerenderAdminPanels();
            });
        });
        document.getElementById('vt-admin-select-all-scenarios')?.addEventListener('change', (e) => {
            state.adminSelectedScenarios = e.target.checked ? scenRows.map((r) => r.id) : [];
            rerenderAdminPanels();
        });
        document.getElementById('vt-admin-clear-scenario-selection')?.addEventListener('click', () => {
            state.adminSelectedScenarios = [];
            rerenderAdminPanels();
        });
        document.getElementById('vt-admin-bulk-archive')?.addEventListener('click', () => {
            adminBulkArchiveScenarios();
        });
        document.getElementById('vt-admin-bulk-permanent-delete')?.addEventListener('click', () => {
            adminBulkPermanentDeleteScenarios();
        });

        container.querySelectorAll('.vt-admin-assessment-check').forEach((cb) => {
            cb.addEventListener('change', () => {
                const id = cb.dataset.id;
                const set = new Set(state.adminSelectedAssessments || []);
                if (cb.checked) set.add(id);
                else set.delete(id);
                state.adminSelectedAssessments = [...set];
                rerenderAdminPanels();
            });
        });
        document.getElementById('vt-admin-select-all-assessments')?.addEventListener('change', (e) => {
            state.adminSelectedAssessments = e.target.checked
                ? assessRows.map((r) => r.assessment_id).filter(Boolean)
                : [];
            rerenderAdminPanels();
        });
        document.getElementById('vt-admin-clear-assessment-selection')?.addEventListener('click', () => {
            state.adminSelectedAssessments = [];
            rerenderAdminPanels();
        });
        document.getElementById('vt-admin-bulk-delete-assessments')?.addEventListener('click', () => {
            adminBulkDeleteAssessments();
        });

        container.querySelectorAll('.vt-admin-delete-scenario').forEach((btn) => {
            btn.addEventListener('click', () => adminRemoveScenario(btn.dataset.id, btn.dataset.title));
        });
        container.querySelectorAll('.vt-admin-restore-scenario').forEach((btn) => {
            btn.addEventListener('click', () => adminRestoreScenario(btn.dataset.id, btn.dataset.title));
        });
        container.querySelectorAll('.vt-admin-permanent-delete-scenario').forEach((btn) => {
            btn.addEventListener('click', () => adminPermanentDeleteScenario(btn.dataset.id, btn.dataset.title));
        });
        container.querySelectorAll('.vt-admin-preview-scenario').forEach((btn) => {
            btn.addEventListener('click', async () => {
                setActionButtonLoading(btn, true, 'Loading…');
                try {
                    await openAdminScenarioPreview(btn.dataset.id, false, btn);
                } finally {
                    setActionButtonLoading(btn, false);
                }
            });
        });
        container.querySelectorAll('.vt-admin-edit-scenario').forEach((btn) => {
            btn.addEventListener('click', async () => {
                setActionButtonLoading(btn, true, 'Loading…');
                try {
                    await openAdminScenarioPreview(btn.dataset.id, true, btn);
                } finally {
                    setActionButtonLoading(btn, false);
                }
            });
        });
        container.querySelectorAll('.vt-admin-view-assessment').forEach((btn) => {
            btn.addEventListener('click', () => viewAdminAssessment(btn.dataset.id));
        });
        container.querySelectorAll('.vt-admin-download-pdf').forEach((btn) => {
            btn.addEventListener('click', () => downloadAssessmentPdfById(btn.dataset.id));
        });
        container.querySelectorAll('.vt-admin-delete-assessment').forEach((btn) => {
            btn.addEventListener('click', () => adminDeleteAssessment(btn.dataset.id, btn.dataset.title));
        });
    }

    function rerenderAdminPanels() {
        const cache = state.adminReportCache;
        if (!cache) return;
        const scenPanel = document.getElementById('vt-admin-panel-scenarios');
        const assessPanel = document.getElementById('vt-admin-panel-assessments');
        if (!scenPanel || !assessPanel) return;
        const scenRows = filterAdminScenarios(cache.scenarios);
        const assessRows = filterAdminAssessments(cache.assessments);
        scenPanel.innerHTML = renderAdminScenariosTable(cache.scenarios);
        assessPanel.innerHTML = renderAdminAssessmentsTable(cache.assessments);
        const scenTab = document.querySelector('.vt-admin-tab[data-vt-admin-tab="scenarios"]');
        const assessTab = document.querySelector('.vt-admin-tab[data-vt-admin-tab="assessments"]');
        if (scenTab) scenTab.textContent = `Scenarios (${scenRows.length})`;
        if (assessTab) assessTab.textContent = `Graded assessments (${assessRows.length})`;
        bindAdminReportActions(document.getElementById('admin-voice-training-reports-content'));
    }

    function setAdminActiveTab(container, tabName, scenPanel, assessPanel, tabHint, tabHints) {
        state.adminActiveTab = tabName;
        container.querySelectorAll('.vt-admin-tab').forEach((t) => {
            t.classList.toggle('active', t.dataset.vtAdminTab === tabName);
        });
        scenPanel.classList.toggle('hidden', tabName !== 'scenarios');
        assessPanel.classList.toggle('hidden', tabName !== 'assessments');
        if (tabHint) tabHint.textContent = tabHints[tabName] || '';
        container.querySelectorAll('.vt-admin-filter-group').forEach((g) => {
            const applies = g.dataset.applies || 'all';
            g.classList.toggle('vt-filter-group-active', applies === 'all' || applies === tabName);
            g.classList.toggle('vt-filter-group-muted', applies !== 'all' && applies !== tabName);
        });
        const searchInput = document.getElementById('vt-admin-search');
        if (searchInput) {
            searchInput.placeholder = tabName === 'assessments'
                ? 'Search assessments by user, scenario, grade…'
                : 'Search scenarios by title, author, category…';
        }
    }

    function buildAdminReportsQuery() {
        const f = state.adminFilters;
        const params = new URLSearchParams();
        if (f.process) params.set('process_id', f.process);
        if (f.role) params.set('role', f.role);
        if (f.creator) params.set('author_id', f.creator);
        if (f.assessedBy) params.set('user_id', f.assessedBy);
        if (f.publish) params.set('publish_status', f.publish);
        const qs = params.toString();
        return qs ? `?${qs}` : '';
    }

    function renderAnalyticsBars(analytics) {
        const cats = analytics?.by_category || {};
        const roles = analytics?.by_creator_role || {};
        const catMax = Math.max(...Object.values(cats), 1);
        const roleMax = Math.max(...Object.values(roles), 1);
        const catHtml = Object.entries(cats).map(([k, v]) => `
            <div class="vt-admin-bar-row">
                <span class="vt-admin-bar-label">${escapeHtml(k.replace(/_/g, ' '))}</span>
                <div class="vt-admin-bar-track"><div class="vt-admin-bar-fill" style="width:${(v / catMax) * 100}%"></div></div>
                <span class="vt-admin-bar-val">${v}</span>
            </div>
        `).join('') || '<p class="placeholder-text">No scenario data for current filters.</p>';
        const roleHtml = Object.entries(roles).map(([k, v]) => `
            <div class="vt-admin-bar-row">
                <span class="vt-admin-bar-label">${escapeHtml(k.replace(/_/g, ' '))}</span>
                <div class="vt-admin-bar-track"><div class="vt-admin-bar-fill role" style="width:${(v / roleMax) * 100}%"></div></div>
                <span class="vt-admin-bar-val">${v}</span>
            </div>
        `).join('') || '';
        return `
            <div class="vt-admin-analytics">
                <div class="vt-admin-analytics-card">
                    <h4><i class="fas fa-chart-bar"></i> Scenarios by category</h4>
                    ${catHtml}
                </div>
                ${roleHtml ? `<div class="vt-admin-analytics-card">
                    <h4><i class="fas fa-user-tie"></i> Scenarios by creator role</h4>
                    ${roleHtml}
                </div>` : ''}
            </div>
        `;
    }

    async function adminBulkArchiveScenarios() {
        const cache = state.adminReportCache;
        if (!cache) return;
        const ids = (state.adminSelectedScenarios || []).filter((id) => {
            const row = (cache.scenarios || []).find((s) => s.id === id);
            return row && !row.archived;
        });
        if (!ids.length) {
            notify('Select active scenarios to archive.', 'warning');
            return;
        }
        if (!window.confirm(`Archive ${ids.length} selected scenario(s)? Creators will be notified.`)) return;
        try {
            const res = await vtApi('/admin/scenarios/bulk-archive', 'POST', { scenario_ids: ids });
            notify(res?.message || `Archived ${ids.length} scenario(s).`, 'success');
            state.adminSelectedScenarios = [];
            await loadAdminReports();
        } catch (err) {
            notify(err?.message || 'Could not archive scenarios.', 'error');
        }
    }

    async function adminBulkPermanentDeleteScenarios() {
        const cache = state.adminReportCache;
        if (!cache) return;
        const ids = (state.adminSelectedScenarios || []).filter((id) => {
            const row = (cache.scenarios || []).find((s) => s.id === id);
            return row && row.archived;
        });
        if (!ids.length) {
            notify('Select archived scenarios to permanently delete.', 'warning');
            return;
        }
        if (!window.confirm(`Permanently delete ${ids.length} archived scenario(s)?\n\nThis cannot be undone.`)) return;
        try {
            const res = await vtApi('/admin/scenarios/bulk-permanent-delete', 'POST', { scenario_ids: ids });
            notify(res?.message || `Deleted ${ids.length} scenario(s).`, 'success');
            state.adminSelectedScenarios = [];
            await loadAdminReports();
        } catch (err) {
            notify(err?.message || 'Could not delete scenarios.', 'error');
        }
    }

    async function adminBulkDeleteAssessments() {
        const ids = (state.adminSelectedAssessments || []).filter(Boolean);
        if (!ids.length) {
            notify('Select assessments to delete.', 'warning');
            return;
        }
        if (!window.confirm(`Delete ${ids.length} selected assessment(s)?\n\nThis permanently removes reports and PDFs.`)) return;
        try {
            const res = await vtApi('/admin/assessments/bulk-delete', 'POST', { assessment_ids: ids });
            notify(res?.message || `Deleted ${ids.length} assessment(s).`, 'success');
            state.adminSelectedAssessments = [];
            await loadAdminReports();
        } catch (err) {
            notify(err?.message || 'Could not delete assessments.', 'error');
        }
    }

    async function adminRemoveScenario(scenarioId, title) {
        if (!scenarioId) return;
        if (!window.confirm(`Archive scenario "${title}"? The creator will be notified.`)) return;
        try {
            await vtApi(`/admin/scenarios/${encodeURIComponent(scenarioId)}`, 'DELETE');
            notify('Scenario archived. Creator has been notified.', 'success');
            await loadAdminReports();
        } catch (err) {
            notify(err?.message || 'Could not remove scenario.', 'error');
        }
    }

    async function adminRestoreScenario(scenarioId, title) {
        if (!scenarioId) return;
        try {
            await vtApi(`/admin/scenarios/${encodeURIComponent(scenarioId)}/restore`, 'POST');
            notify(`Scenario "${title || 'scenario'}" restored.`, 'success');
            await loadAdminReports();
        } catch (err) {
            notify(err?.message || 'Could not restore scenario.', 'error');
        }
    }

    async function adminPermanentDeleteScenario(scenarioId, title) {
        if (!scenarioId) return;
        if (!window.confirm(`Permanently delete "${title || 'this scenario'}"?\n\nThis removes the scenario and all related practice sessions and assessments. This cannot be undone.`)) return;
        try {
            await vtApi(`/admin/scenarios/${encodeURIComponent(scenarioId)}/permanent`, 'DELETE');
            notify('Scenario permanently deleted.', 'success');
            await loadAdminReports();
        } catch (err) {
            notify(err?.message || 'Could not delete scenario.', 'error');
        }
    }

    async function adminDeleteAssessment(assessmentId, title) {
        if (!assessmentId) return;
        if (!window.confirm(`Delete assessment for "${title || 'this scenario'}"?\n\nThis permanently removes the report and PDF.`)) return;
        try {
            await vtApi(`/admin/assessments/${encodeURIComponent(assessmentId)}`, 'DELETE');
            notify('Assessment deleted.', 'success');
            await loadAdminReports();
        } catch (err) {
            notify(err?.message || 'Could not delete assessment.', 'error');
        }
    }

    async function loadAdminReports() {
        const container = document.getElementById('admin-voice-training-reports-content');
        if (!container) return;
        container.innerHTML = '<div class="loading-state"><i class="fas fa-spinner fa-spin"></i> Loading voice training data...</div>';
        try {
            const data = await vtApi(`/admin/reports${buildAdminReportsQuery()}`);
            const summary = data?.summary || {};
            const filters = data?.filters || {};
            const scenarios = data?.scenarios || [];
            const assessments = data?.assessments || [];
            const processes = filters.processes || [];
            const creators = filters.creators || [];
            const assessmentUsers = filters.assessment_users || [];
            const roles = filters.roles || ['trainee', 'trainer', 'super_admin'];

            state.adminReportCache = { scenarios, assessments, summary, analytics: data.analytics };
            state.adminSelectedScenarios = (state.adminSelectedScenarios || []).filter((id) =>
                scenarios.some((s) => s.id === id),
            );
            state.adminSelectedAssessments = (state.adminSelectedAssessments || []).filter((id) =>
                assessments.some((a) => a.assessment_id === id),
            );
            const scenCount = filterAdminScenarios(scenarios).length;
            const assessCount = filterAdminAssessments(assessments).length;
            const activeFilters = [
                state.adminFilters.process ? 'Process' : '',
                state.adminFilters.creator ? 'Author' : '',
                state.adminFilters.publish ? 'Publish' : '',
                state.adminFilters.role ? 'Taker role' : '',
                state.adminFilters.assessedBy ? 'Taker' : '',
            ].filter(Boolean);

            container.innerHTML = `
                <div class="vt-admin-filters-grid">
                    <div class="vt-admin-filter-group vt-filter-group-active" data-applies="all">
                        <h4><i class="fas fa-filter"></i> Applies to both tabs</h4>
                        <div class="vt-admin-filter">
                            <label for="vt-admin-filter-process">Process</label>
                            <select id="vt-admin-filter-process" class="form-control">
                                <option value="">All processes</option>
                                ${processes.map((p) => `<option value="${escapeHtml(p.id)}" ${state.adminFilters.process === p.id ? 'selected' : ''}>${escapeHtml(p.name)}</option>`).join('')}
                            </select>
                        </div>
                    </div>
                    <div class="vt-admin-filter-group" data-applies="scenarios">
                        <h4><i class="fas fa-layer-group"></i> Scenario filters</h4>
                        <div class="vt-admin-filter">
                            <label for="vt-admin-filter-creator">Scenario author</label>
                            <select id="vt-admin-filter-creator" class="form-control">
                                <option value="">All trainers / admins</option>
                                ${creators.map((c) => `<option value="${escapeHtml(c.id)}" ${state.adminFilters.creator === c.id ? 'selected' : ''}>${escapeHtml(c.email)} (${escapeHtml(c.role)})</option>`).join('')}
                            </select>
                        </div>
                        <div class="vt-admin-filter">
                            <label for="vt-admin-filter-publish">Publish status</label>
                            <select id="vt-admin-filter-publish" class="form-control">
                                <option value="">All scenarios</option>
                                <option value="published" ${state.adminFilters.publish === 'published' ? 'selected' : ''}>Published only</option>
                                <option value="unpublished" ${state.adminFilters.publish === 'unpublished' ? 'selected' : ''}>Not published</option>
                            </select>
                        </div>
                    </div>
                    <div class="vt-admin-filter-group" data-applies="assessments">
                        <h4><i class="fas fa-chart-bar"></i> Assessment filters</h4>
                        <div class="vt-admin-filter">
                            <label for="vt-admin-filter-role">Assessment taker role</label>
                            <select id="vt-admin-filter-role" class="form-control">
                                <option value="">All roles</option>
                                ${roles.map((r) => `<option value="${escapeHtml(r)}" ${state.adminFilters.role === r ? 'selected' : ''}>${escapeHtml(r.replace(/_/g, ' '))}</option>`).join('')}
                            </select>
                        </div>
                        <div class="vt-admin-filter">
                            <label for="vt-admin-filter-assessed-by">Assessment taker</label>
                            <select id="vt-admin-filter-assessed-by" class="form-control">
                                <option value="">All users with assessments</option>
                                ${assessmentUsers.map((u) => `<option value="${escapeHtml(u.id)}" ${state.adminFilters.assessedBy === u.id ? 'selected' : ''}>${escapeHtml(u.email)} (${escapeHtml(u.role)})</option>`).join('')}
                            </select>
                        </div>
                    </div>
                    <div class="vt-admin-filter-actions">
                        <button type="button" id="vt-admin-filter-reset" class="btn btn-secondary btn-sm"><i class="fas fa-rotate-left"></i> Reset filters</button>
                    </div>
                </div>
                ${activeFilters.length ? `<div class="vt-admin-active-filters"><span>Active:</span> ${activeFilters.map((f) => `<span class="vt-filter-chip">${escapeHtml(f)}</span>`).join('')}</div>` : ''}
                <div class="vt-admin-search-row">
                    <label for="vt-admin-search"><i class="fas fa-search"></i> Search</label>
                    <input type="search" id="vt-admin-search" class="form-control" value="${escapeHtml(state.adminSearchQuery || '')}" placeholder="Search scenarios by title, author, category…" />
                </div>
                <div class="vt-admin-summary">
                    <div class="vt-admin-stat"><span class="vt-admin-stat-val">${summary.total_scenarios || 0}</span><span class="vt-admin-stat-lbl">Scenarios</span></div>
                    <div class="vt-admin-stat"><span class="vt-admin-stat-val">${summary.published_scenarios || 0}</span><span class="vt-admin-stat-lbl">Published</span></div>
                    <div class="vt-admin-stat"><span class="vt-admin-stat-val">${summary.unpublished_scenarios || 0}</span><span class="vt-admin-stat-lbl">Not published</span></div>
                    <div class="vt-admin-stat"><span class="vt-admin-stat-val">${summary.archived_scenarios || 0}</span><span class="vt-admin-stat-lbl">Archived</span></div>
                    <div class="vt-admin-stat vt-admin-stat-highlight"><span class="vt-admin-stat-val">${summary.total_assessments || 0}</span><span class="vt-admin-stat-lbl">Graded assessments</span></div>
                </div>
                ${renderAnalyticsBars(data.analytics)}
                <div class="vt-admin-tabs" role="tablist">
                    <button type="button" class="vt-admin-tab" data-vt-admin-tab="scenarios">Scenarios (${scenCount})</button>
                    <button type="button" class="vt-admin-tab" data-vt-admin-tab="assessments">Graded assessments (${assessCount})</button>
                </div>
                <p class="vt-admin-tab-hint" id="vt-admin-tab-hint"></p>
                <div id="vt-admin-panel-scenarios" class="vt-admin-panel"></div>
                <div id="vt-admin-panel-assessments" class="vt-admin-panel hidden"></div>
            `;

            const scenPanel = document.getElementById('vt-admin-panel-scenarios');
            const assessPanel = document.getElementById('vt-admin-panel-assessments');
            const tabHint = document.getElementById('vt-admin-tab-hint');
            scenPanel.innerHTML = renderAdminScenariosTable(scenarios);
            assessPanel.innerHTML = renderAdminAssessmentsTable(assessments);

            const tabHints = {
                scenarios: 'Training modules authored by trainers and super admins. Use Scenario author to narrow this list.',
                assessments: 'Completed voice assessments. Use Assessment taker role or Assessment taker to narrow this list.',
            };

            container.querySelectorAll('.vt-admin-tab').forEach((tab) => {
                tab.addEventListener('click', () => {
                    setAdminActiveTab(container, tab.dataset.vtAdminTab, scenPanel, assessPanel, tabHint, tabHints);
                });
            });
            setAdminActiveTab(container, state.adminActiveTab || 'scenarios', scenPanel, assessPanel, tabHint, tabHints);

            document.getElementById('vt-admin-filter-process')?.addEventListener('change', (e) => {
                state.adminFilters.process = e.target.value;
                loadAdminReports();
            });
            document.getElementById('vt-admin-filter-role')?.addEventListener('change', (e) => {
                state.adminFilters.role = e.target.value;
                state.adminActiveTab = 'assessments';
                loadAdminReports();
            });
            document.getElementById('vt-admin-filter-creator')?.addEventListener('change', (e) => {
                state.adminFilters.creator = e.target.value;
                state.adminActiveTab = 'scenarios';
                loadAdminReports();
            });
            document.getElementById('vt-admin-filter-publish')?.addEventListener('change', (e) => {
                state.adminFilters.publish = e.target.value;
                state.adminActiveTab = 'scenarios';
                loadAdminReports();
            });
            document.getElementById('vt-admin-filter-assessed-by')?.addEventListener('change', (e) => {
                state.adminFilters.assessedBy = e.target.value;
                state.adminActiveTab = 'assessments';
                loadAdminReports();
            });
            document.getElementById('vt-admin-filter-reset')?.addEventListener('click', () => {
                state.adminFilters = { process: '', role: '', creator: '', assessedBy: '', publish: '' };
                state.adminSearchQuery = '';
                state.adminActiveTab = 'scenarios';
                loadAdminReports();
            });
            document.getElementById('vt-admin-search')?.addEventListener('input', (e) => {
                state.adminSearchQuery = e.target.value || '';
                rerenderAdminPanels();
            });
            bindAdminReportActions(container);
        } catch (err) {
            container.innerHTML = `<p class="placeholder-text">Failed to load reports: ${escapeHtml(err.message || 'Unknown error')}</p>`;
        }
    }

    function onVoiceTrainingTabActivated() {
        applyVoiceTrainingRoleUi();
        showVtSection('home');
        loadAgentSttConfig();
        loadScenarios();
    }

    function initVoiceTraining() {
        applyVoiceTrainingRoleUi();
        document.getElementById('vt-ai-generate-btn')?.addEventListener('click', openGeneratorWizard);
        document.getElementById('vt-refresh-scenarios-btn')?.addEventListener('click', loadScenarios);
        document.getElementById('vt-category-filter')?.addEventListener('change', (e) => {
            state.filterCategory = e.target.value;
            applyScenarioListFilters();
        });
        document.getElementById('vt-publish-filter')?.addEventListener('change', (e) => {
            state.filterPublish = e.target.value;
            applyScenarioListFilters();
        });
        document.getElementById('vt-scenario-search')?.addEventListener('input', (e) => {
            state.searchQuery = e.target.value || '';
            applyScenarioListFilters();
        });
        document.getElementById('vt-view-archived-btn')?.addEventListener('click', toggleArchivedView);
        document.getElementById('vt-assessment-view-filter')?.addEventListener('change', (e) => {
            state.assessmentViewFilter = e.target.value || 'all';
            applyAssessmentViewFilter();
        });
        document.getElementById('vt-show-archived')?.addEventListener('change', (e) => {
            state.showArchived = Boolean(e.target.checked);
            document.getElementById('vt-view-archived-btn')?.classList.toggle('active', state.showArchived);
            loadScenarios();
        });

        document.getElementById('vt-assessment-config-close')?.addEventListener('click', closeVtAssessmentConfigModal);
        document.getElementById('vt-assessment-config-cancel')?.addEventListener('click', closeVtAssessmentConfigModal);
        document.getElementById('vt-assessment-config-form')?.addEventListener('submit', submitVtAssessmentConfig);
        document.getElementById('vt-assessment-config-modal')?.addEventListener('click', (e) => {
            if (e.target.id === 'vt-assessment-config-modal') closeVtAssessmentConfigModal();
        });

        document.getElementById('vt-publish-modal-close')?.addEventListener('click', closeVtPublishModal);
        document.getElementById('vt-publish-cancel')?.addEventListener('click', closeVtPublishModal);
        document.getElementById('vt-publish-form')?.addEventListener('submit', submitVtPublish);
        document.getElementById('vt-publish-modal')?.addEventListener('click', (e) => {
            if (e.target.id === 'vt-publish-modal') closeVtPublishModal();
        });
        document.querySelectorAll('input[name="vt-publication-type"]').forEach((radio) => {
            radio.addEventListener('change', updateVtPublishBatchVisibility);
        });

        document.getElementById('vt-audience-modal-close')?.addEventListener('click', closeVtAudienceModal);
        document.getElementById('vt-audience-cancel')?.addEventListener('click', closeVtAudienceModal);
        document.getElementById('vt-audience-save')?.addEventListener('click', saveVtAudience);
        document.getElementById('vt-audience-add-btn')?.addEventListener('click', addVtAudienceTrainee);
        document.getElementById('vt-audience-modal')?.addEventListener('click', (e) => {
            if (e.target.id === 'vt-audience-modal') closeVtAudienceModal();
        });

        document.getElementById('vt-preview-close')?.addEventListener('click', closePreview);
        document.getElementById('vt-preview-modal')?.addEventListener('click', (e) => {
            if (e.target.id === 'vt-preview-modal') closePreview();
        });
        document.querySelectorAll('.vt-preview-tab').forEach((tab) => {
            tab.addEventListener('click', () => switchPreviewTab(tab.dataset.tab));
        });
        document.getElementById('vt-preview-practice-btn')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            if (!state.scenarioId || btn.disabled || btn.classList.contains('vt-btn-loading')) return;
            await startSession(state.scenarioId, 'practice', btn);
        });
        document.getElementById('vt-preview-assess-btn')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            if (!state.scenarioId || btn.disabled || btn.classList.contains('vt-btn-loading')) return;
            await startSession(state.scenarioId, 'assessment', btn);
        });
        document.getElementById('vt-preview-edit-btn')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            if (!state.scenarioId || btn.disabled || btn.classList.contains('vt-btn-loading')) return;
            await openPreviewForEdit(state.scenarioId, btn);
        });

        document.getElementById('vt-gen-close')?.addEventListener('click', closeGeneratorWizard);
        document.getElementById('vt-gen-cancel-btn')?.addEventListener('click', closeGeneratorWizard);
        document.getElementById('vt-gen-done-btn')?.addEventListener('click', finishGeneratorWizard);
        document.getElementById('vt-gen-back-btn')?.addEventListener('click', genGoBack);
        document.getElementById('vt-gen-next-btn')?.addEventListener('click', genGoNext);
        document.getElementById('vt-gen-submit-btn')?.addEventListener('click', submitGenerator);
        document.querySelectorAll('.vt-gen-mode-card').forEach((card) => {
            card.addEventListener('click', () => {
                state.gen.mode = card.dataset.mode;
                if (state.gen.mode === 'ai_auto' && ['scope', 'story'].includes(state.gen.step)) {
                    state.gen.step = 'mode';
                }
                updateGenWizardUI();
            });
        });
        document.getElementById('vt-gen-story')?.addEventListener('input', (e) => {
            state.gen.story = e.target.value;
            document.getElementById('vt-gen-story-count').textContent = String(state.gen.story.length);
        });
        document.getElementById('vt-gen-examples-btn')?.addEventListener('click', loadPromptExamples);
        const dropzone = document.getElementById('vt-gen-dropzone');
        const fileInput = document.getElementById('vt-gen-files');
        dropzone?.addEventListener('click', () => fileInput?.click());
        fileInput?.addEventListener('change', (e) => handleGenFiles(e.target.files));
        dropzone?.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
        dropzone?.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
        dropzone?.addEventListener('drop', (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
            handleGenFiles(e.dataTransfer.files);
        });

        document.getElementById('vt-send-suggested-btn')?.addEventListener('click', async () => {
            await ensureAudioUnlocked();
            sendMessage(document.getElementById('vt-suggested-response')?.textContent, { viaVoice: false });
        });
        document.getElementById('vt-hear-suggested-btn')?.addEventListener('click', hearSuggestedReply);
        document.getElementById('vt-hear-customer-btn')?.addEventListener('click', hearCustomerAgain);
        document.getElementById('vt-transcript-toggle')?.addEventListener('click', () => {
            state.transcriptOpen = !state.transcriptOpen;
            document.getElementById('vt-transcript')?.classList.toggle('collapsed', !state.transcriptOpen);
            const toggle = document.getElementById('vt-transcript-toggle');
            toggle?.setAttribute('aria-expanded', state.transcriptOpen ? 'true' : 'false');
            toggle?.querySelector('.vt-transcript-chevron')?.classList.toggle('fa-chevron-down', !state.transcriptOpen);
            toggle?.querySelector('.vt-transcript-chevron')?.classList.toggle('fa-chevron-up', state.transcriptOpen);
            if (state.transcriptOpen) scrollTranscriptToBottom(true);
        });
        document.getElementById('vt-transcript-jump-btn')?.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!state.transcriptOpen) {
                state.transcriptOpen = true;
                document.getElementById('vt-transcript')?.classList.remove('collapsed');
                const toggle = document.getElementById('vt-transcript-toggle');
                toggle?.setAttribute('aria-expanded', 'true');
                toggle?.querySelector('.vt-transcript-chevron')?.classList.remove('fa-chevron-down');
                toggle?.querySelector('.vt-transcript-chevron')?.classList.add('fa-chevron-up');
            }
            scrollTranscriptToBottom(true);
        });
        document.getElementById('vt-transcript-jump-btn')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                e.stopPropagation();
                document.getElementById('vt-transcript-jump-btn')?.click();
            }
        });

        if (state.synth) {
            state.synth.getVoices();
            window.speechSynthesis.onvoiceschanged = () => state.synth.getVoices();
        }
        document.getElementById('vt-voice-reply-btn')?.addEventListener('click', toggleVoiceReply);
        document.getElementById('vt-end-session-btn')?.addEventListener('click', endSession);
        document.getElementById('vt-cancel-assessment-btn')?.addEventListener('click', cancelAssessment);
        document.getElementById('vt-session-back-btn')?.addEventListener('click', () => {
            if (state.sessionMode === 'assessment' && state.sessionId) {
                cancelAssessment();
                return;
            }
            if (state.sessionMode === 'practice' && state.sessionId) {
                cancelPractice();
                return;
            }
            showVtSection('home');
        });
        document.getElementById('vt-practice-tips-close')?.addEventListener('click', () => {
            document.getElementById('vt-practice-tips')?.classList.add('hidden');
        });
        document.getElementById('vt-assessment-tips-close')?.addEventListener('click', () => {
            document.getElementById('vt-assessment-tips')?.classList.add('hidden');
        });
        document.getElementById('vt-session-complete-end-btn')?.addEventListener('click', endSession);
        document.querySelectorAll('.vt-results-tab').forEach((tab) => {
            tab.addEventListener('click', () => switchResultsTab(tab.dataset.tab));
        });
        document.getElementById('vt-results-back-btn')?.addEventListener('click', async () => {
            if (state.resultsReturnTo === 'admin') {
                await returnToAdminVoiceReports();
            } else {
                showVtSection('home');
                loadScenarios();
            }
        });
        document.getElementById('vt-try-another-btn')?.addEventListener('click', () => {
            state.resultsReturnTo = 'home';
            showVtSection('home');
            loadScenarios();
        });
        document.getElementById('vt-download-pdf-btn')?.addEventListener('click', (e) => {
            e.preventDefault();
            downloadAssessmentPdf();
        });
    }

    window.initVoiceTraining = initVoiceTraining;
    window.onVoiceTrainingTabActivated = onVoiceTrainingTabActivated;
    window.stopVoiceTrainingPlayback = stopAllPlayback;
    window.loadVoiceTrainingAdminReports = loadAdminReports;
    window.openVoiceTrainingPreview = openAdminScenarioPreview;
    window.viewVoiceTrainingAssessment = viewAdminAssessment;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initVoiceTraining);
    } else {
        initVoiceTraining();
    }
})();
