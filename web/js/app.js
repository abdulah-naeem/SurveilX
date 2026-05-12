/**
 * SurveilX Dashboard Logic
 */

document.addEventListener('DOMContentLoaded', () => {
    initApp();
});

// --- Constants & State ---
const THEME_KEY = 'theme';
const OVERLAY_KEY = 'overlayVisible';
const SIDEBAR_KEY = 'sidebarFolded';
const ROLE_KEY = 'authRole';
let CAMS = [];
let DETECTION_STATE = {};
let VIEW_MODE = localStorage.getItem('viewMode') || 'grid'; // 'grid' | 'single'
let OVERLAY_VISIBLE = localStorage.getItem(OVERLAY_KEY) !== 'false'; // Default TRUE
let SIDEBAR_FOLDED = localStorage.getItem(SIDEBAR_KEY) === 'true'; // Default FALSE
let SHOW_CONFIDENCES = localStorage.getItem('showConfidences') !== 'false'; // Default TRUE
const LOGS = []; // Legacy placeholder, UI removed

const SIDEBAR_CONTEXTS = {
    dashboard: {
        value: 'Overview',
        metric1Label: 'Active Cameras',
        metric1Value: () => `${CAMS.length}/${CAMS.length || 0}`,
        metric2Label: 'Live Alerts',
        metric2Value: () => Object.keys(DETECTION_STATE || {}).length,
    },
    cameras: {
        value: 'Camera Registry',
        metric1Label: 'Online Feeds',
        metric1Value: () => `${CAMS.filter(c => c.enabled !== false).length}/${CAMS.length || 0}`,
        metric2Label: 'Add Camera',
        metric2Value: () => 'Available',
    },
    events: {
        value: 'Alerts Feed',
        metric1Label: 'Recent Alerts',
        metric1Value: () => Object.values(DETECTION_STATE || {}).filter(v => v && v.is_alert).length,
        metric2Label: 'System State',
        metric2Value: () => 'Monitoring',
    },
    analytics: {
        value: 'Analytics',
        metric1Label: 'Cameras',
        metric1Value: () => CAMS.length,
        metric2Label: 'Insights',
        metric2Value: () => 'Live',
    },
    search: {
        value: 'Layout',
        metric1Label: 'Presets',
        metric1Value: () => '6',
        metric2Label: 'Mode',
        metric2Value: () => VIEW_MODE,
    },
    settings: {
        value: 'Settings',
        metric1Label: 'Role',
        metric1Value: () => localStorage.getItem(ROLE_KEY) || 'unknown',
        metric2Label: 'Sync',
        metric2Value: () => 'Pending',
    }
};

function updateSidebarContext(tabId) {
    const context = SIDEBAR_CONTEXTS[tabId] || SIDEBAR_CONTEXTS.dashboard;
    const titleEl = document.getElementById('sidebar-context-label');
    const valueEl = document.getElementById('sidebar-context-value');
    const metric1LabelEl = document.getElementById('sidebar-metric-1-label');
    const metric1ValueEl = document.getElementById('sidebar-metric-1-value');
    const metric2LabelEl = document.getElementById('sidebar-metric-2-label');
    const metric2ValueEl = document.getElementById('sidebar-metric-2-value');

    if (titleEl) titleEl.textContent = 'Dashboard Context';
    if (valueEl) valueEl.textContent = context.value;
    if (metric1LabelEl) metric1LabelEl.textContent = context.metric1Label;
    if (metric1ValueEl) metric1ValueEl.textContent = String(context.metric1Value());
    if (metric2LabelEl) metric2LabelEl.textContent = context.metric2Label;
    if (metric2ValueEl) metric2ValueEl.textContent = String(context.metric2Value());
}

// --- Alerts ---
class AlertQueue {
    constructor() {
        this.queue = [];
        this.active = false;
        this.soundPlayed = false;
        this.audioCtx = null;
    }

    add(msg, type = 'error') {
        this.queue.push({ msg, type });
        this.process();
    }

    async process() {
        if (this.active || this.queue.length === 0) return;
        this.active = true;
        const { msg, type } = this.queue.shift();

        if (type === 'critical') {
            this.playAlert();
            showBigAlert(msg);
        }

        await showToast(msg, type);
        this.active = false;
        if (this.queue.length > 0) setTimeout(() => this.process(), 300);
    }

    playAlert() {
        // Simple Audio Beep
        if (!this.audioCtx) this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (this.audioCtx.state === 'suspended') this.audioCtx.resume();

        const osc = this.audioCtx.createOscillator();
        const gain = this.audioCtx.createGain();
        osc.connect(gain);
        gain.connect(this.audioCtx.destination);

        osc.type = 'sawtooth';
        osc.frequency.setValueAtTime(880, this.audioCtx.currentTime); // High pitch
        osc.frequency.exponentialRampToValueAtTime(440, this.audioCtx.currentTime + 0.1);

        gain.gain.setValueAtTime(0.5, this.audioCtx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.01, this.audioCtx.currentTime + 0.5);

        osc.start();
        osc.stop(this.audioCtx.currentTime + 0.5);
    }
}
const ALERTS = new AlertQueue();

function showToast(msg, type = 'info') {
    return new Promise(resolve => {
        let container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toast-container';
            document.body.appendChild(container);
        }

        const el = document.createElement('div');
        el.className = `toast toast-${type}`;
        el.innerHTML = `
            <div class="toast-icon">${type === 'critical' ? '🚨' : 'ℹ️'}</div>
            <div class="toast-body">${msg}</div>
        `;

        container.appendChild(el);

        // Force reflow
        el.offsetHeight;
        el.classList.add('show');

        setTimeout(() => {
            el.classList.remove('show');
            setTimeout(() => {
                el.remove();
                resolve();
            }, 300);
        }, 5000); // 5 seconds display
    });
}

// --- Initialization ---
function initApp() {
    applyTheme(localStorage.getItem(THEME_KEY) || 'dark');

    if (!ensureAuth()) return; // Redirects if failed
    applyRoleUi();

    // Bind Global Event Listeners
    bindGlobalEvents();
    applyOverlayVisibility(); // Apply initial state from localStorage
    applySidebarFold();       // Apply initial state from localStorage

    // Initial Data Load
    loadCameras();
    fetchDetections();
    pollEmbeddings();
    if (typeof window.initCustomVideoPage === 'function') {
        window.initCustomVideoPage();
    }

    // Polling Intervals
    setInterval(fetchDetections, 2500);
    setInterval(loadCameras, 15000);
    setInterval(pollEmbeddings, 5000);

    // Dashboard Stats Polling (For everyone)
    fetchAdminOverview();
    setInterval(fetchAdminOverview, 5000); 

    if (getRole() === 'admin') {
        fetchAdminHealth();
        setInterval(fetchAdminHealth, 10000);
        loadAdminCameras();
        loadAdminUsers();
    }

    // Initial view: Dashboard
    const dashLink = document.getElementById('nav-dashboard');
    if (dashLink) dashLink.click();

    // Timezone & Security
    refreshTimezone();
    startHeartbeat();
}

function startHeartbeat() {
    let fails = 0;
    const MAX_FAILS = 3;
    setInterval(async () => {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 2000);
            const res = await fetch('/auth/me', { method: 'HEAD', signal: controller.signal });
            clearTimeout(timeoutId);

            if (res.status === 401) {
                doLogout();
                return;
            }
            if (res.ok) {
                fails = 0;
            } else {
                fails++;
            }
        } catch (e) {
            fails++;
        }

        if (fails >= MAX_FAILS) {
            console.warn("Connection lost. Retrying...");
            // Do not wipe screen or throw error, just retry next loop
            fails = 0; // reset to avoid spamming
        }

        // Always poll overview stats for the dashboard charts/cards
        await fetchAdminOverview();

        const role = getRole();
        if (role === 'admin') {
            await fetchAdminHealth();
        }
        
        // Always poll events if the feed is part of the current UI
        await fetchUserEvents();
    }, 2000);
}

// --- Auth & Role ---
function getRole() { return localStorage.getItem(ROLE_KEY); }

function ensureAuth() {
    try {
        const tok = localStorage.getItem('authToken');
        const xhr = new XMLHttpRequest();
        xhr.open('GET', '/auth/me', false); // synchronous check
        xhr.withCredentials = true;
        if (tok) xhr.setRequestHeader('Authorization', 'Bearer ' + tok);
        xhr.send(null);
        if (xhr.status !== 200) {
            doLogout();
            return false;
        }

        // Parse user info from response
        try {
            const user = JSON.parse(xhr.responseText);
            updateUserInfo(user);
        } catch (e) {
            console.error('Failed to parse user info', e);
        }

    } catch (e) {
        doLogout();
        return false;
    }
    const r = getRole();
    if (!r) {
        doLogout();
        return false;
    }
    document.documentElement.setAttribute('data-role', r);
    return true;
}

function updateUserInfo(user) {
    if (!user) return;
    const nameEls = document.querySelectorAll('.user-info .name');
    const roleEls = document.querySelectorAll('.user-info .role');
    const avatarEls = document.querySelectorAll('.user-avatar');

    nameEls.forEach(el => el.textContent = user.username);
    roleEls.forEach(el => el.textContent = user.role.toUpperCase());

    // Simple avatar logic: first letter
    if (user.username) {
        avatarEls.forEach(el => el.textContent = user.username.charAt(0).toUpperCase());
    }
}

function doLogout() {
    localStorage.removeItem('authToken');
    localStorage.removeItem('authRole');
    window.location.href = 'login-user.html';
}

function applyRoleUi() {
    const r = getRole();
    const isAdmin = r === 'admin';

    // Toggle Nav & Panels
    const navSettings = document.getElementById('nav-settings');

    if (!isAdmin) {
        if (navSettings) navSettings.style.display = 'none';
    }

    // Toggle role-specific classes
    const adminEls = Array.from(document.querySelectorAll('.admin-only'));
    const userEls = Array.from(document.querySelectorAll('.user-only'));
    adminEls.forEach(el => el.style.display = isAdmin ? '' : 'none');
    userEls.forEach(el => el.style.display = isAdmin ? 'none' : '');
}

// --- Helper Functions ---
async function fetchJSON(url, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    const tok = localStorage.getItem('authToken');
    if (tok && !headers['Authorization']) headers['Authorization'] = 'Bearer ' + tok;

    const res = await fetch(url, { credentials: 'include', headers, ...opts });
    if (res.status === 401) {
        doLogout();
        throw new Error('Session expired');
    }
    if (!res.ok) throw new Error(await res.text());
    return res.json();
}

async function runAsyncAction(btn, action, loadingText = 'Wait...') {
    if (!btn || btn.disabled) return;
    const originalHtml = btn.innerHTML;
    // Save minimal width to prevent jumping
    const w = btn.offsetWidth;
    btn.style.minWidth = w + 'px';

    btn.disabled = true;
    btn.textContent = loadingText;
    btn.classList.add('btn-loading'); // For optional CSS styling

    try {
        await action();
    } catch (e) {
        console.error(e);
        alert('Action failed: ' + (e.message || e));
    } finally {
        btn.innerHTML = originalHtml;
        btn.disabled = false;
        btn.classList.remove('btn-loading');
        btn.style.minWidth = '';
    }
}

function debounce(fn, ms) {
    let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn.apply(null, args), ms); };
}

// --- Theme & Overlay Visibility ---
function applyTheme(theme) {
    const t = (theme === 'light' || theme === 'dark') ? theme : 'dark';
    document.documentElement.setAttribute('data-theme', t);
    localStorage.setItem(THEME_KEY, t);
}

function applyOverlayVisibility() {
    document.body.classList.toggle('overlay-hide', !OVERLAY_VISIBLE);
}

function applySidebarFold() {
    const sidebar = document.querySelector('.sidebar');
    if (sidebar) sidebar.classList.toggle('collapsed', SIDEBAR_FOLDED);
    document.body.classList.toggle('sidebar-folded', SIDEBAR_FOLDED);
}

// --- Global Binding ---
function bindGlobalEvents() {
    // Theme Toggle
    const themeBtn = document.getElementById('theme-toggle');
    if (themeBtn) themeBtn.addEventListener('click', () => {
        const cur = localStorage.getItem(THEME_KEY) || 'dark';
        applyTheme(cur === 'dark' ? 'light' : 'dark');
    });

    // Overlay Toggle
    const overlayBtn = document.getElementById('overlay-toggle');
    if (overlayBtn) {
        if (OVERLAY_VISIBLE) overlayBtn.classList.add('active');
        overlayBtn.addEventListener('click', () => {
            OVERLAY_VISIBLE = !OVERLAY_VISIBLE;
            localStorage.setItem(OVERLAY_KEY, OVERLAY_VISIBLE);
            overlayBtn.classList.toggle('active', OVERLAY_VISIBLE);
            applyOverlayVisibility();
            showToast(`Overlay ${OVERLAY_VISIBLE ? 'Shown' : 'Hidden'}`, 'info');
        });
    }

    // Sidebar Toggle
    const sidebarBtn = document.getElementById('sidebar-toggle-btn');
    if (sidebarBtn) {
        sidebarBtn.addEventListener('click', () => {
            SIDEBAR_FOLDED = !SIDEBAR_FOLDED;
            localStorage.setItem(SIDEBAR_KEY, SIDEBAR_FOLDED);
            applySidebarFold();
        });
    }

    // Auto-Responsive Sidebar
    window.addEventListener('resize', debounce(() => {
        const width = window.innerWidth;
        const shouldFold = width <= 1024;
        if (shouldFold !== SIDEBAR_FOLDED) {
            SIDEBAR_FOLDED = shouldFold;
            localStorage.setItem(SIDEBAR_KEY, SIDEBAR_FOLDED);
            applySidebarFold();
        }
    }, 250));

    // Premium Segmented View Mode Controllers
    const btnGrid = document.getElementById('btn-mode-grid');
    const btnSingle = document.getElementById('btn-mode-single');
    const setMode = (mode) => {
        VIEW_MODE = mode;
        localStorage.setItem('viewMode', VIEW_MODE);
        if (VIEW_MODE === 'single') {
            const sel = document.getElementById('camera-select');
            if (sel && !sel.value && CAMS[0]) sel.value = CAMS[0].id;
        }
        applyViewMode();
    };
    if (btnGrid) btnGrid.addEventListener('click', () => setMode('grid'));
    if (btnSingle) btnSingle.addEventListener('click', () => setMode('single'));

    // Legacy View Toggle mapping
    const viewBtn = document.getElementById('view-toggle');
    if (viewBtn) viewBtn.addEventListener('click', () => {
        setMode(VIEW_MODE === 'single' ? 'grid' : 'single');
    });

    // Confidence Visibility Toggle
    const confToggle = document.getElementById('conf-toggle');
    if (confToggle) {
        confToggle.checked = SHOW_CONFIDENCES;
        confToggle.addEventListener('change', () => {
            SHOW_CONFIDENCES = confToggle.checked;
            localStorage.setItem('showConfidences', SHOW_CONFIDENCES);
            updateDetectionBadges();
            showToast(`Confidence stats ${SHOW_CONFIDENCES ? 'shown' : 'hidden'}`, 'info');
        });
    }

    // Logout
    const logoutBtn = document.getElementById('logout-btn');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', async () => {
            // Robust logout: Try API, but ALWAYS redirect
            try {
                // Disable button to show feedback
                logoutBtn.disabled = true;
                logoutBtn.textContent = '...';
                await fetch('/auth/logout', { method: 'POST' });
            } catch (e) {
                console.warn('Logout API failed, forcing local logout', e);
            } finally {
                doLogout();
            }
        });
    }

    // Keyboard Shortcuts
    document.addEventListener('keydown', handleKeyboardShortcuts);
    // Allow Escape to exit fullscreen if active
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && document.fullscreenElement) {
            try { document.exitFullscreen(); } catch (err) { /* ignore */ }
        }
    });
    // When exiting fullscreen, return to grid view to allow minimizing
    function onFullScreenChange() {
        if (!document.fullscreenElement) {
            VIEW_MODE = 'grid';
            localStorage.setItem('viewMode', VIEW_MODE);
            applyViewMode();
        }
    }
    document.addEventListener('fullscreenchange', onFullScreenChange);
    document.addEventListener('webkitfullscreenchange', onFullScreenChange);

    // Nav Links
    const navLinks = Array.from(document.querySelectorAll('.nav a'));
    navLinks.forEach(a => a.addEventListener('click', (e) => {
        const href = a.getAttribute('href');
        if (!href || !href.startsWith('#')) return;

        navLinks.forEach(n => {
            if (n.getAttribute('href')?.startsWith('#')) {
                n.classList.remove('active');
            }
        });
        a.classList.add('active');

        const targetId = href.substring(1);
        updateSidebarContext(targetId);
        const sections = document.querySelectorAll('main section');
        
        // Define which sections belong to which "tab"
        const tabGroups = {
            'dashboard':    ['admin-stats', 'cameras', 'thumbbar'],
            'analytics':    ['admin-analytics'],
            'cameras':      ['admin-cam-mgr', 'cameras'],
            'events':       ['user-events'],
            'search':       ['search', 'similar-card'],
            'custom-video': ['custom-video'],
            'clips':        ['clips'],
            'logs':         ['logs'],
            'settings':     ['admin-user-mgr', 'admin-health', 'settings']
        };

        const activeSections = tabGroups[targetId] || [];

        sections.forEach(s => {
            const isVisible = activeSections.includes(s.id);
            s.style.display = isVisible ? '' : 'none';
        });

        if (targetId === 'settings') {
            loadAdminSettings();
            loadAdminUsers();
            fetchAdminHealth();
        }
        if (targetId === 'dashboard') {
            loadCameras();
            fetchAdminOverview();
        }
        if (targetId === 'custom-video') {
            if (typeof window.initCustomVideoPage === 'function') {
                window.initCustomVideoPage();
            }
        }
    }));

    // Upload Search
    const uploadBtn = document.getElementById('upload-search');
    const uploadInput = document.getElementById('upload-file');
    const uploadName = document.getElementById('upload-name');

    if (uploadInput) {
        uploadInput.addEventListener('change', () => {
            if (uploadInput.files && uploadInput.files[0]) {
                uploadName.textContent = uploadInput.files[0].name;
            } else {
                uploadName.textContent = 'Choose an image';
            }
        });
    }

    if (uploadBtn && uploadInput) {
        uploadBtn.addEventListener('click', () => {
            const textInput = document.getElementById('text-search-input');
            const hasText = textInput && textInput.value.trim().length > 0;
            const hasFile = uploadInput.files && uploadInput.files[0];

            if (!hasFile && !hasText) {
                showToast('Please provide an image or text prompt.', 'warn');
                return;
            }

            const minMatch = document.getElementById('min-match');

            runAsyncAction(uploadBtn, async () => {
                let res;
                const tok = localStorage.getItem('authToken');
                const authHeader = tok ? { 'Authorization': 'Bearer ' + tok } : {};
                
                if (hasText) {
                    const query = encodeURIComponent(textInput.value.trim());
                    res = await fetch(`/api/embeddings/search_text?query=${query}&k=12`, { headers: authHeader });
                } else {
                    const fd = new FormData();
                    fd.append('file', uploadInput.files[0]);
                    res = await fetch('/api/embeddings/search_image?k=12', { method: 'POST', body: fd, headers: authHeader });
                }

                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail || 'Search failed');
                }
                const data = await res.json();

                const simCard = document.getElementById('similar-card');
                if (simCard) simCard.style.display = 'block';

                renderSimilarResults(data, parseFloat(minMatch.value) || 0);
            }, 'Searching...');
        });
    }

    // Camera Filter for Logs
    const camFilter = document.getElementById('cam-filter');
    if (camFilter) camFilter.addEventListener('change', renderLogs);

    // Log Controls
    ['lvl-info', 'lvl-warn', 'lvl-error', 'lvl-debug'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('change', renderLogs);
    });
    const logSearch = document.getElementById('log-search');
    if (logSearch) logSearch.addEventListener('input', debounce(renderLogs, 200));

    const tailSize = document.getElementById('tail-size');
    if (tailSize) tailSize.addEventListener('change', renderLogs);

    const autoScroll = document.getElementById('auto-scroll');
    if (autoScroll) autoScroll.addEventListener('change', renderLogs);

    const pauseLog = document.getElementById('pause-log');
    if (pauseLog) pauseLog.addEventListener('click', () => {
        LOGS_PAUSED = !LOGS_PAUSED;
        pauseLog.textContent = LOGS_PAUSED ? 'Resume' : 'Pause';
    });

    const clearLog = document.getElementById('clear-log');
    if (clearLog) clearLog.addEventListener('click', () => {
        LOGS.length = 0;
        renderLogs();
    });

    const exportLog = document.getElementById('export-log');
    if (exportLog) exportLog.addEventListener('click', exportLogs);

    // Admin Buttons
    const addCamBtn = document.getElementById('btn-add-camera');
    if (addCamBtn) addCamBtn.addEventListener('click', openAddCameraModal);

    const addUserBtn = document.getElementById('btn-add-user');
    if (addUserBtn) addUserBtn.addEventListener('click', openAddUserModal);

    // Close Modal Overlay
    const overlay = document.getElementById('modal-overlay');
    const cancelParams = document.getElementById('modal-cancel');
    if (overlay) {
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) closeModal();
        });
    }
    if (cancelParams) cancelParams.addEventListener('click', closeModal);

    // Save Settings
    const saveSettingsBtn = document.getElementById('btn-save-settings');
    if (saveSettingsBtn) {
        saveSettingsBtn.addEventListener('click', () => {
            updateAdminSettings();
        });
    }
}

function applyViewMode() {
    const isSingle = VIEW_MODE === 'single';
    const btnGrid = document.getElementById('btn-mode-grid');
    const btnSingle = document.getElementById('btn-mode-single');
    const camSelect = document.getElementById('camera-select');
    const mainTitle = document.getElementById('page-main-title');

    if (btnGrid) btnGrid.classList.toggle('active', !isSingle);
    if (btnSingle) btnSingle.classList.toggle('active', isSingle);
    if (camSelect) camSelect.classList.toggle('hidden', !isSingle);
    if (mainTitle) mainTitle.textContent = isSingle ? 'Camera Single View' : 'System Dashboard';

    const legacyBtn = document.getElementById('view-toggle');
    if (legacyBtn) legacyBtn.textContent = isSingle ? 'Grid View' : 'Single View';

    applyFocusFromDropdown();
    if (typeof updateDetectionBadges === 'function') updateDetectionBadges();
}

// --- Cameras ---
async function loadCameras() {
    try {
        const res = await fetchJSON('/cameras');
        const camsDiv = document.getElementById('cams');
        if (!camsDiv) return;

        // Preserve scroll if possible or optimized re-render? For now full rebuild
        // but we can optimize by checking diff. simpler to rebuild for now.
        const currentScroll = camsDiv.scrollTop;

        camsDiv.innerHTML = '';
        const cams = applySavedOrder(res.cameras || []);
        // Deduplicate by id in case backend returned duplicates
        const unique = Array.from(new Map(cams.map(c => [String(c.id), c])).values());
        CAMS = unique;

        const countEl = document.getElementById('cam-count');
        if (countEl) countEl.textContent = `${unique.length} camera(s)`;

        populateDropdown(unique);

        unique.forEach(({ id, name }) => {
            const card = createCameraCard(id, name);
            camsDiv.appendChild(card);
        });

        if (camsDiv.scrollTo) camsDiv.scrollTo(0, currentScroll);

        buildThumbbar(unique);
        applyFocusFromDropdown();
        applyViewMode();
        renderDetectionTabs();
    } catch (e) {
        console.error('Failed to load cameras', e);
        const countEl = document.getElementById('cam-count');
        if (countEl) countEl.textContent = 'Failed to load';
    }
}

function createCameraCard(id, name) {
    const card = document.createElement('div');
    card.className = 'card cam-card';
    card.dataset.camId = id;

    // Drag & Drop
    card.draggable = true;
    card.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', id));
    card.addEventListener('dragover', (e) => e.preventDefault());
    card.addEventListener('drop', (e) => onDrop(e, id));

    const frame = document.createElement('div');
    frame.className = 'frame';

    // Badges inside video frame
    const badgesOverlay = document.createElement('div');
    badgesOverlay.className = 'cam-badges-overlay';
    
    const badge = document.createElement('div');
    badge.className = 'pill rec cam-badge';
    badge.dataset.badgeFor = id;
    badge.innerHTML = '● REC';
    badgesOverlay.appendChild(badge);

    const confBadge = document.createElement('div');
    confBadge.className = 'pill motion cam-conf hidden';
    confBadge.innerHTML = '● MOTION';
    badgesOverlay.appendChild(confBadge);

    frame.appendChild(badgesOverlay);

    // Timestamp
    const timestamp = document.createElement('div');
    timestamp.className = 'cam-timestamp';
    const updateTime = () => { timestamp.textContent = new Date().toLocaleTimeString([], { hour12: true }); };
    updateTime();
    setInterval(updateTime, 1000);
    frame.appendChild(timestamp);

    // Confidence Overlay Container
    const probs = document.createElement('div');
    probs.className = 'cam-probs hidden';
    frame.appendChild(probs);
    
    const img = document.createElement('img');
    img.src = `/stream/${id}`;
    img.alt = `Stream ${id}`;
    img.loading = 'lazy'; // Performant loading
    img.title = 'Click to focus/fullscreen';

    img.addEventListener('click', () => {
        const sel = document.getElementById('camera-select');
        sel.value = String(id);
        VIEW_MODE = 'single';
        localStorage.setItem('viewMode', VIEW_MODE);
        applyViewMode();
        requestFullscreen(frame);
    });

    frame.appendChild(img);

    // Info bar below video
    const infoBar = document.createElement('div');
    infoBar.className = 'cam-info-bar';
    
    const infoLeft = document.createElement('div');
    infoLeft.className = 'cam-info-left';
    
    const title = document.createElement('div');
    title.className = 'cam-name';
    title.textContent = name ? name : `Camera ${id}`;
    
    const location = document.createElement('div');
    location.className = 'cam-location';
    location.textContent = `Location ${id}`; // Placeholder
    
    infoLeft.appendChild(title);
    infoLeft.appendChild(location);

    const infoRight = document.createElement('div');
    infoRight.className = 'cam-status';
    infoRight.style.display = 'flex';
    infoRight.style.alignItems = 'center';
    infoRight.style.gap = '8px';

    const aiToggleLbl = document.createElement('label');
    aiToggleLbl.style.cssText = 'display:flex; align-items:center; gap:4px; cursor:pointer; background:var(--bg-tertiary); padding:2px 6px; border-radius:10px; border:1px solid var(--border-color);';
    aiToggleLbl.title = 'Enable/disable real-time AI detection for this stream';
    
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = true;
    checkbox.className = 'cam-ai-toggle-chk';
    checkbox.style.cursor = 'pointer';
    
    const lblSpan = document.createElement('span');
    lblSpan.style.cssText = 'font-size:10px; font-weight:bold; color:var(--accent-secondary);';
    lblSpan.textContent = 'AI';

    aiToggleLbl.appendChild(lblSpan);
    aiToggleLbl.appendChild(checkbox);

    checkbox.addEventListener('change', async (e) => {
        const enabled = e.target.checked;
        lblSpan.style.color = enabled ? 'var(--accent-secondary)' : 'var(--text-muted)';
        try {
            await fetchJSON(`/api/cameras/${id}/toggle_ai`, {
                method: 'POST',
                body: JSON.stringify({ enabled })
            });
            showToast(`AI detection ${enabled ? 'enabled' : 'disabled'} for stream ${id}`, 'info');
            if (typeof updateDetectionBadges === 'function') updateDetectionBadges();
        } catch (err) {
            console.error('Failed to toggle AI', err);
            e.target.checked = !enabled;
            lblSpan.style.color = !enabled ? 'var(--accent-secondary)' : 'var(--text-muted)';
        }
    });

    const statusLive = document.createElement('div');
    statusLive.style.display = 'flex';
    statusLive.style.alignItems = 'center';
    statusLive.innerHTML = `<div class="status-dot"></div> Live`;

    infoRight.appendChild(aiToggleLbl);
    infoRight.appendChild(statusLive);

    infoBar.appendChild(infoLeft);
    infoBar.appendChild(infoRight);

    // Stats box below video for Single View
    const statsBox = document.createElement('div');
    statsBox.className = 'cam-stats-box hidden';
    statsBox.innerHTML = `
        <div class="cam-stats-box-header">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 20V10"></path><path d="M12 20V4"></path><path d="M6 20v-6"></path></svg>
            <span>Live Class Confidences</span>
        </div>
        <div class="cam-stats-grid"></div>
    `;

    card.appendChild(frame);
    card.appendChild(infoBar);
    card.appendChild(statsBox);

    return card;
}

function onDrop(ev, targetId) {
    ev.preventDefault();
    const srcId = ev.dataTransfer.getData('text/plain');
    if (!srcId || srcId === targetId) return;

    const order = CAMS.map(c => c.id);
    const from = order.indexOf(srcId);
    const to = order.indexOf(targetId);
    if (from === -1 || to === -1) return;

    order.splice(to, 0, order.splice(from, 1)[0]);
    localStorage.setItem('cameraOrder', JSON.stringify(order));
    loadCameras(); // Re-render
}

function applySavedOrder(cams) {
    const order = JSON.parse(localStorage.getItem('cameraOrder') || '[]');
    if (!order.length) return cams;
    const map = Object.fromEntries(cams.map(c => [c.id, c]));
    const ordered = order.filter(id => map[id]).map(id => map[id]);
    const remaining = cams.filter(c => !order.includes(c.id));
    return [...ordered, ...remaining];
}

// --- View Mode & Navigation ---
function populateDropdown(cams) {
    const sel = document.getElementById('camera-select');
    const customSel = document.getElementById('page-custom-cam');

    if (customSel) {
        const currCustom = customSel.value;
        customSel.innerHTML = cams.map(c => `<option value="${c.id}">${c.name || c.id}</option>`).join('');
        if (currCustom && cams.some(c => String(c.id) === String(currCustom))) {
            customSel.value = currCustom;
        } else if (sel && sel.value) {
            customSel.value = sel.value;
        }
    }

    if (!sel) return;

    const current = sel.value;
    sel.innerHTML = '';

    const optAll = document.createElement('option');
    optAll.value = '';
    optAll.textContent = 'Overview (All)';
    sel.appendChild(optAll);

    cams.forEach(({ id, name }) => {
        const opt = document.createElement('option');
        opt.value = id;
        opt.textContent = name || id;
        sel.appendChild(opt);
    });

    if (Array.from(sel.options).some(o => o.value === current)) sel.value = current;
    sel.onchange = applyFocusFromDropdown;
}

function applyFocusFromDropdown() {
    const sel = document.getElementById('camera-select');
    if (!sel) return;
    const selVal = sel.value;

    const cards = Array.from(document.querySelectorAll('#cams .card'));
    const singleMode = VIEW_MODE === 'single';
    const thumbbar = document.getElementById('thumbbar');

    if (!selVal || !singleMode) {
        cards.forEach(c => {
            c.classList.remove('focused', 'dimmed', 'hidden');
            c.style.display = '';
        });
        if (thumbbar) thumbbar.classList.add('hidden');
        return;
    }

    // Single/Focus Mode
    let found = false;
    cards.forEach(c => {
        const isTarget = String(c.dataset.camId) === String(selVal);
        c.classList.toggle('focused', isTarget);
        c.classList.toggle('dimmed', !isTarget);
        c.classList.toggle('hidden', !isTarget);
        c.style.display = isTarget ? '' : 'none';
        if (isTarget) found = true;
    });

    if (thumbbar) thumbbar.classList.remove('hidden');

    if (found) {
        const target = cards.find(c => c.dataset.camId === selVal);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    if (typeof updateDetectionBadges === 'function') updateDetectionBadges();
}



function handleKeyboardShortcuts(e) {
    if (e.ctrlKey || e.altKey || e.metaKey || e.target.tagName === 'INPUT') return;

    // Number keys 1-9
    const num = parseInt(e.key, 10);
    if (!isNaN(num) && num >= 1 && num <= 9) {
        const idx = num - 1;
        if (CAMS[idx]) {
            const sel = document.getElementById('camera-select');
            sel.value = CAMS[idx].id;
            VIEW_MODE = 'single';
            localStorage.setItem('viewMode', VIEW_MODE);
            applyViewMode();
        }
    }

    // Arrow keys
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        const sel = document.getElementById('camera-select');
        // Only cycle if in single mode
        if (VIEW_MODE !== 'single') return;

        e.preventDefault();
        const currentId = sel.value;
        const idx = CAMS.findIndex(c => String(c.id) === currentId);

        let nextIdx = 0;
        if (idx !== -1) {
            const delta = (e.key === 'ArrowRight') ? 1 : -1;
            nextIdx = (idx + delta + CAMS.length) % CAMS.length;
        }

        if (CAMS[nextIdx]) {
            sel.value = String(CAMS[nextIdx].id);
            applyViewMode();
        }
    }
}

function requestFullscreen(elem) {
    // Toggle fullscreen: exit if already fullscreen, otherwise request
    try {
        if (document.fullscreenElement) {
            if (document.exitFullscreen) return document.exitFullscreen();
            if (document.webkitExitFullscreen) return document.webkitExitFullscreen();
            if (document.msExitFullscreen) return document.msExitFullscreen();
            return;
        }

        if (elem.requestFullscreen) {
            elem.requestFullscreen();
        } else if (elem.webkitRequestFullscreen) { /* Safari */
            elem.webkitRequestFullscreen();
        } else if (elem.msRequestFullscreen) { /* IE11 */
            elem.msRequestFullscreen();
        }
    } catch (err) {
        console.warn('Fullscreen toggle failed', err);
    }
}

// --- Thumbnails ---
function buildThumbbar(cams) {
    const bar = document.getElementById('thumbbar');
    if (!bar) return;
    bar.innerHTML = '';

    cams.forEach(({ id, name }) => {
        const t = document.createElement('div');
        t.className = 'thumb';
        const img = document.createElement('img');
        img.src = `/stream/${id}`;
        img.alt = id;
        const label = document.createElement('span');
        label.textContent = name || id;

        t.appendChild(img);
        t.appendChild(label);
        t.addEventListener('click', () => {
            const sel = document.getElementById('camera-select');
            sel.value = id;
            applyFocusFromDropdown();
        });
        bar.appendChild(t);
    });
}

// --- Similar Search Render (Chroma format) ---
function renderSimilarResults(data, minPct) {
    const wrap = document.getElementById('similar-wrap');
    if (!wrap) return;
    wrap.innerHTML = '';

    // Support both old chroma format and new Chroma {results:[]} format
    const results = data.results || [];
    // Legacy fallback
    const legacyMetas = data.metadatas || [];
    const legacyIds   = data.ids || [];
    const legacyDists = data.distances || [];

    const items = results.length > 0
        ? results.map(r => ({
            id:             r.id,
            score:          r.score,            // cosine sim 0-1
            camera_id:      r.camera_id,
            timestamp_iso:  r.timestamp_iso,
            cloudinary_url: r.cloudinary_url,
            violence_label: r.violence_label,
          }))
        : legacyMetas.map((m, i) => ({
            id:             legacyIds[i],
            score:          typeof legacyDists[i] === 'number' ? (1 - legacyDists[i]) : undefined,
            camera_id:      m.camera_id,
            timestamp_iso:  m.timestamp_iso,
            cloudinary_url: m.cloudinary_url || '',
            violence_label: m.violence_label,
          }));

    let shown = 0;
    for (const item of items) {
        const pct = typeof item.score === 'number' ? Math.max(0, Math.min(1, item.score)) * 100 : undefined;
        if (typeof pct === 'number' && minPct && pct < minPct) {
            continue;
        }

        const div = document.createElement('div');
        div.className = 'similar-item';

        // Thumbnail from Cloudinary CDN
        if (item.cloudinary_url) {
            const img = document.createElement('img');
            img.src = item.cloudinary_url;
            img.alt = item.id;
            img.loading = 'lazy';
            img.style.cssText = 'width:100%;border-radius:6px;object-fit:cover;max-height:120px;';
            div.appendChild(img);
        } else {
            const ph = document.createElement('div');
            ph.style.cssText = 'height:80px;display:flex;align-items:center;justify-content:center;background:var(--bg-input);border-radius:6px;color:var(--text-muted);font-size:11px;';
            ph.textContent = 'No preview';
            div.appendChild(ph);
        }

        const cap = document.createElement('div');
        cap.className = 'cap';
        cap.style.marginTop = '6px';
        const ts  = item.timestamp_iso ? formatTimestamp(item.timestamp_iso) : '';
        const simTxt = typeof pct === 'number' ? `<span style="color:var(--success);font-size:11px">${pct.toFixed(1)}% match</span>` : '';
        const lblTxt = item.violence_label && item.violence_label !== 'Normal'
            ? `<span class="pill pill-error" style="font-size:10px">${item.violence_label}</span> `
            : '';
        cap.innerHTML = `
            <div style="font-weight:600;font-size:12px">${item.camera_id || ''}</div>
            <div style="font-size:11px;color:var(--text-muted)">${ts}</div>
            <div style="margin-top:3px">${lblTxt}${simTxt}</div>`;

        if (item.id && item.camera_id) {
            const btn = document.createElement('button');
            btn.className = 'btn btn-sm primary';
            btn.style.cssText = 'width:100%;margin-top:8px;font-size:11px;';
            btn.innerHTML = '🎬 Generate Clip';
            btn.onclick = () => openClipModal(item.camera_id, item.timestamp_iso);
            cap.appendChild(btn);
        }

        div.appendChild(cap);
        wrap.appendChild(div);
        shown++;
    }

    const card = document.getElementById('similar-card');
    if (shown === 0) {
        if (items.length > 0) {
            const fallback = items
                .slice()
                .sort((a, b) => (b.score || 0) - (a.score || 0))
                .slice(0, 3);

            wrap.innerHTML = '<div class="muted" style="padding:10px;">No results met the match threshold. Showing the closest visual matches instead.</div>';
            fallback.forEach((item) => {
                const div = document.createElement('div');
                div.className = 'similar-item';

                if (item.cloudinary_url) {
                    const img = document.createElement('img');
                    img.src = item.cloudinary_url;
                    img.alt = item.id;
                    img.loading = 'lazy';
                    img.style.cssText = 'width:100%;border-radius:6px;object-fit:cover;max-height:120px;';
                    div.appendChild(img);
                }

                const cap = document.createElement('div');
                cap.className = 'cap';
                cap.style.marginTop = '6px';
                const ts = item.timestamp_iso ? formatTimestamp(item.timestamp_iso) : '';
                const pct = typeof item.score === 'number' ? Math.max(0, Math.min(1, item.score)) * 100 : undefined;
                const simTxt = typeof pct === 'number' ? `<span style="color:var(--warning);font-size:11px">${pct.toFixed(1)}% match</span>` : '';
                const lblTxt = item.violence_label && item.violence_label !== 'Normal'
                    ? `<span class="pill pill-error" style="font-size:10px">${item.violence_label}</span> `
                    : '';
                cap.innerHTML = `
                    <div style="font-weight:600;font-size:12px">${item.camera_id || ''}</div>
                    <div style="font-size:11px;color:var(--text-muted)">${ts}</div>
                    <div style="margin-top:3px">${lblTxt}${simTxt}</div>`;
                div.appendChild(cap);
                wrap.appendChild(div);
            });
        } else {
            wrap.innerHTML = '<div class="muted" style="padding:10px;">No high-confidence matches found.</div>';
        }
    }
    if (card) {
        card.style.display = 'block';
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

// ════════════════════════════════════════════════════════════════════════════
// 3-Mode Clip Generation Panel
// Modes: 1) NLP Query  2) Auto Event Capture  3) Custom Time Frame
// ════════════════════════════════════════════════════════════════════════════

// openClipFromFrameModal removed in favor of independent generators


// Timezone Helper
let APP_TIMEZONE = 'UTC+0';
async function refreshTimezone() {
    try {
        const settings = await fetchJSON('/api/admin/settings');
        const tz = settings.settings.find(s => s.key === 'timezone');
        if (tz) APP_TIMEZONE = tz.value;
    } catch(e) {}
}

function formatTimestamp(isoStr) {
    if (!isoStr) return '';
    try {
        const date = new Date(isoStr);
        if (isNaN(date.getTime())) return isoStr;
        
        // Parse UTC offset (e.g. UTC+5 or UTC-3)
        let offsetHours = 0;
        const match = (APP_TIMEZONE || 'UTC+0').match(/UTC([+-]\d+)/);
        if (match) {
            offsetHours = parseInt(match[1]);
        }
        
        // date.getTime() is absolute UTC ms. 
        // We add the offset to get the nominal ms in the target timezone.
        const targetMs = date.getTime() + (offsetHours * 3600000);
        
        // To format this without browser local timezone interference:
        // We use a date object that "looks" like the target time in UTC.
        const displayDate = new Date(targetMs);
        
        // Use UTC methods to avoid local browser offset
        const y = displayDate.getUTCFullYear();
        const m = String(displayDate.getUTCMonth() + 1).padStart(2, '0');
        const d = String(displayDate.getUTCDate()).padStart(2, '0');
        const hh = String(displayDate.getUTCHours()).padStart(2, '0');
        const mm = String(displayDate.getUTCMinutes()).padStart(2, '0');
        const ss = String(displayDate.getUTCSeconds()).padStart(2, '0');
        
        return `${y}-${m}-${d} ${hh}:${mm}:${ss}`;
    } catch(e) {
        return isoStr;
    }
}


// --- Admin Stats & Management ---
async function fetchAdminOverview() {
    if (document.hidden) return;
    try {
        const ov = await fetchJSON('/api/stats/overview');
        setText('stat-total-cameras', ov.total_cameras);
        setText('stat-streams', ov.active_streams);
        setText('stat-events', ov.events_24h);
        setText('stat-active-users', ov.active_users);

        const critEl = document.getElementById('stat-critical');
        if (critEl) {
            critEl.textContent = ov.critical_alerts;
            if (ov.critical_alerts > 0) critEl.classList.add('pulse-text');
            else critEl.classList.remove('pulse-text');
        }

        if (ov.charts) renderCharts(ov.charts);
    } catch (e) {
        console.error('Overview poll error', e);
    }
}

async function fetchAdminHealth() {
    if (document.hidden || getRole() !== 'admin') return;
    try {
        const he = await fetchJSON('/api/stats/health');
        setText('meter-cpu', he.cpu === null ? '—' : he.cpu + '%');
        setText('meter-ram', he.ram === null ? '—' : (he.ram.percent || he.ram) + '%');
        setText('meter-disk', he.disk === null ? '—' : (he.disk.percent || he.disk) + '%');
        setText('meter-net', he.net === null ? '—' : (typeof he.net === 'string' ? he.net : 'Online'));

        let dev = he.device || 'CPU';
        // Add tooltip or extra info if available
        if (he.torch_version) {
            const card = document.getElementById('meter-device')?.parentElement;
            if (card) card.title = `Torch: ${he.torch_version}, CUDA: ${he.cuda_available}`;
        }
        setText('meter-device', dev);
    } catch (e) {
        console.error('Health poll error', e);
    }
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

const CLASS_COLORS = {
    'Normal': 'var(--success)',
    'Fighting': 'var(--error)',
    'Shooting': '#ff4500',
    'Abuse': '#ff8c00',
    'Stealing': '#ffd700',
    'Burglary': '#ba55d3',
    'Vandalism': '#cd5c5c',
    'Fire': '#ff0000',
    'Explosion': '#8b0000',
    'Robbery': '#ff6347',
    'Initializing...': '#777'
};

function formatHour(hStr) {
    if (!hStr || !hStr.includes(':')) return hStr;
    const h = parseInt(hStr.split(':')[0]);
    const ampm = h >= 12 ? 'PM' : 'AM';
    const hour12 = (h % 12) || 12;
    return `${hour12} ${ampm}`;
}

function renderCharts(charts) {
    if (!charts) return;

    // 1. Time Chart (Events per Hour) - 24H Histogram
    const timeEl = document.getElementById('chart-time');
    if (timeEl && charts.by_time) {
        const data = charts.by_time;
        // Sort keys chronologically (they are in HH:00 format, so we need to handle the wrapping)
        // For simplicity, we'll just sort them as strings or handle 24h window
        const keys = Object.keys(data).sort(); 

        // Get all unique labels across all buckets for legend/stacking
        const allLabels = new Set();
        keys.forEach(k => Object.keys(data[k]).forEach(l => allLabels.add(l)));
        const labelsArr = Array.from(allLabels);

        // Find max total for scaling
        let max = 1;
        keys.forEach(k => {
            const v = data[k] || {};
            const total = Object.values(v).reduce((a, b) => a + b, 0);
            if (total > max) max = total;
        });

        timeEl.innerHTML = `
        <div class="chart-bars">
            ${keys.map(k => {
            const v = data[k] || {};
            const entries = Object.entries(v);
            
            // Helper to build title for tooltip
            const tooltip = `Time: ${k}\n` + entries.map(([l, count]) => `${l}: ${count}`).join('\n');

            return `
                <div class="chart-col" title="${tooltip}">
                    <div class="bar-stack">
                        ${entries.map(([lbl, count]) => {
                            const h = (count / max) * 100;
                            const color = CLASS_COLORS[lbl] || 'var(--primary)';
                            return h > 0 ? `<div class="seg" style="height:${h}%; background-color:${color};"></div>` : '';
                        }).join('')}
                    </div>
                    <span class="chk-label">${formatHour(k)}</span>
                </div>`;
        }).join('')}
        </div>`;
    }

    // 2. Type/Severity Chart - Sequential Summary
    const typeEl = document.getElementById('chart-type');
    if (typeEl && charts.by_type) {
        const data = charts.by_type;
        const keys = Object.keys(data).filter(k => k !== 'Initializing...');
        const total = keys.reduce((a, k) => a + (data[k] || 0), 0) || 1;

        // Sort by volume
        keys.sort((a,b) => data[b] - data[a]);

        typeEl.innerHTML = `
        <div class="chart-rows">
            ${keys.map(k => {
            const v = data[k];
            const pct = Math.round((v / total) * 100);
            const color = CLASS_COLORS[k] || 'var(--primary)';

            return `
                <div class="chart-row">
                    <div class="label" style="color:${color}; font-weight:bold;">${k}</div>
                    <div class="track">
                        <div class="fill" style="width: ${pct}%; background-color:${color};"></div>
                    </div>
                    <div class="val">${v}</div>
                </div>`;
        }).join('')}
        </div>`;
    }
}

async function loadAdminUsers() {
    try {
        const data = await fetchJSON('/admin/users');
        const tbody = document.getElementById('admin-users-tbody');
        if (!tbody) return;
        const users = data.users || [];
        const totalEl = document.getElementById('admin-users-total');
        const adminsEl = document.getElementById('admin-users-admins');
        const activeEl = document.getElementById('admin-users-active');
        if (totalEl) totalEl.textContent = String(users.length);
        if (adminsEl) adminsEl.textContent = String(users.filter(u => String(u.role).toLowerCase() === 'admin').length);
        if (activeEl) activeEl.textContent = String(users.filter(u => !u.disabled).length);
        tbody.innerHTML = '';
        users.forEach(u => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><div class="user-row"><span class="u-name">${u.username}</span></div></td>
                <td><span class="pill ${u.role === 'admin' ? 'pill-admin' : 'pill-user'}">${u.role}</span></td>
                <td><span class="status-chip"><span class="status-dot ${u.disabled ? 'status-offline' : 'status-online'}"></span>${u.disabled ? 'Disabled' : 'Active'}</span></td>
                <td>
                    <div class="actions">
                        <button class="btn btn-sm" data-act="reset" title="Reset PW">🔑</button>
                        <button class="btn btn-sm" data-act="toggle" title="Toggle">${u.disabled ? '✅' : '🚫'}</button>
                        <button class="btn btn-sm" data-act="logout" title="Logout">🚪</button>
                        <button class="btn btn-sm btn-danger" data-act="delete" title="Delete">🗑️</button>
                    </div>
                </td>`;

            // Note: Arrow functions maintain 'this' from text, so we use closures or event.target
            const bindBtn = (sel, fn) => {
                const b = tr.querySelector(sel);
                if (b) b.onclick = function () { fn(u, this); };
            };

            bindBtn('[data-act="reset"]', (user, btn) => {
                const npw = prompt(`New password for ${user.username}:`);
                if (!npw) return;
                runAsyncAction(btn, async () => {
                    await fetchJSON('/admin/users/reset_password', { method: 'POST', body: JSON.stringify({ username: user.username, new_password: npw }) });
                    loadAdminUsers();
                }, '...');
            });

            bindBtn('[data-act="toggle"]', (user, btn) => {
                runAsyncAction(btn, async () => {
                    await fetchJSON('/admin/users/disable', { method: 'POST', body: JSON.stringify({ username: user.username, disabled: !user.disabled }) });
                    loadAdminUsers();
                }, '...');
            });

            bindBtn('[data-act="logout"]', (user, btn) => {
                runAsyncAction(btn, async () => {
                    await fetchJSON('/admin/users/force_logout', { method: 'POST', body: JSON.stringify({ username: user.username }) });
                    alert('Session cleared');
                }, '...');
            });

            bindBtn('[data-act="delete"]', (user, btn) => {
                if (!confirm(`Delete user ${user.username}?`)) return;
                runAsyncAction(btn, async () => {
                    await fetchJSON('/admin/users/delete', { method: 'POST', body: JSON.stringify({ username: user.username }) });
                    loadAdminUsers();
                }, '...');
            });

            tbody.appendChild(tr);
        });
    } catch (e) { console.error(e); }
}

// --- Settings ---
async function loadAdminSettings() {
    try {
        const data = await fetchJSON('/api/admin/settings');
        const form = document.getElementById('settings-form');
        if (!form) return;
        form.innerHTML = '';
        (data.settings || []).forEach(s => {
            if (s.key === 'detector_enabled') return; // Hide this setting completely as requested
            
            const isBool = s.value === 'true' || s.value === 'false';
            const div = document.createElement('div');
            
            if (isBool) {
                div.className = 'form-group switch-row';
                div.innerHTML = `
                    <div class="info">
                        <label>${s.description || s.key.replace(/_/g, ' ').toUpperCase()}</label>
                        <div class="muted" style="font-size:10px;">Last updated: ${new Date(s.updated_at).toLocaleString()}</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" data-key="${s.key}" ${s.value === 'true' ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                `;
            } else {
                div.className = 'form-group';
                div.innerHTML = `
                    <label>${s.description || s.key.replace(/_/g, ' ').toUpperCase()}</label>
                    <input class="input" data-key="${s.key}" value="${s.value}">
                    <div class="muted" style="font-size:10px; margin-top:2px;">Last updated: ${new Date(s.updated_at).toLocaleString()}</div>
                `;
            }
            form.appendChild(div);
        });
    } catch (e) { console.error(e); }
}

async function updateAdminSettings() {
    const btn = document.getElementById('btn-save-settings');
    const inputs = document.querySelectorAll('#settings-form input');
    const payload = {};
    inputs.forEach(i => {
        if (i.type === 'checkbox') {
            payload[i.dataset.key] = i.checked ? 'true' : 'false';
        } else {
            payload[i.dataset.key] = i.value;
        }
    });

    runAsyncAction(btn, async () => {
        await fetchJSON('/api/admin/settings', { method: 'POST', body: JSON.stringify(payload) });
        showToast('Settings saved', 'success');
        loadAdminSettings();
    }, 'Saving...');
}

async function loadAdminCameras() {
    try {
        const [data, limits] = await Promise.all([
            fetchJSON('/admin/cameras'),
            fetchJSON('/api/system/limits').catch(() => ({ max_cameras: '?', current_cameras: '?', at_limit: false, slots_remaining: 1 }))
        ]);
        const grid = document.getElementById('admin-cam-grid');
        if (!grid) return;

        // ── Capacity banner ──────────────────────────────────────────────
        const bannerMsg = limits.at_limit
            ? `⚠️ Camera limit reached (${limits.current_cameras}/${limits.max_cameras}) — remove a camera to add a new one`
            : `📹 ${limits.current_cameras} / ${limits.max_cameras} cameras in use · ${limits.slots_remaining} slot${limits.slots_remaining === 1 ? '' : 's'} remaining`;
        const bannerElClass = limits.at_limit
            ? 'capacity-banner error'
            : limits.slots_remaining <= 1
              ? 'capacity-banner warn'
              : 'capacity-banner';

        // Disable/enable the Add Camera button
        const addBtn = document.getElementById('btn-add-camera');
        if (addBtn) {
            addBtn.disabled = limits.at_limit;
            addBtn.title = limits.at_limit
                ? `Limit reached (${limits.max_cameras}). Raise max_cameras in Settings to add more.`
                : '';
            addBtn.style.opacity = limits.at_limit ? '0.45' : '';
        }

        const usedEl = document.getElementById('admin-cameras-used');
        const remainingEl = document.getElementById('admin-cameras-remaining');
        const limitEl = document.getElementById('admin-cameras-limit');
        const banner = document.getElementById('admin-cam-banner');
        if (usedEl) usedEl.textContent = String(limits.current_cameras);
        if (remainingEl) remainingEl.textContent = String(limits.slots_remaining);
        if (limitEl) limitEl.textContent = String(limits.max_cameras);
        if (banner) {
            banner.className = bannerElClass;
            banner.textContent = bannerMsg;
        }

        grid.innerHTML = '';

        (data.cameras || []).forEach(c => {
            const d = document.createElement('div');
            d.className = 'card cam-admin-card';
            d.innerHTML = `
                <div class="card-header">
                    <h4>${c.name || 'Cam ' + c.id}</h4>
                    <span class="pill ${c.enabled ? 'pill-user' : 'pill-admin'}">${c.enabled ? 'Online' : 'Offline'}</span>
                </div>
                <div class="card-body">
                    <p><strong>ID:</strong> ${c.id}</p>
                    <p><strong>Zone:</strong> ${c.zone || 'None'}</p>
                    <p><strong>Source:</strong> ${c.source_url || 'Not set'}</p>
                </div>
                <div class="card-actions">
                    <button class="btn btn-sm" data-act="edit">Edit</button>
                    <button class="btn btn-sm" data-act="test">Test</button>
                    <button class="btn btn-sm btn-danger" data-act="remove">Remove</button>
                </div>
            `;

            const bindBtn = (sel, fn) => { d.querySelector(sel).onclick = function () { fn(c, this); }; };

            bindBtn('[data-act="edit"]', (cam) => openEditCameraModal(cam));

            bindBtn('[data-act="test"]', (cam, btn) => {
                runAsyncAction(btn, async () => {
                    await fetchJSON(`/admin/cameras/${cam.id}/test`, { method: 'POST' });
                    alert('Connection OK');
                }, 'Testing...');
            });

            bindBtn('[data-act="remove"]', (cam, btn) => {
                if (!confirm('Remove camera?')) return;
                runAsyncAction(btn, async () => {
                    await fetchJSON(`/admin/cameras/${cam.id}`, { method: 'DELETE' });
                    loadAdminCameras();
                }, 'Removing...');
            });

            grid.appendChild(d);
        });
    } catch (e) { console.error(e); }
}

// --- Modals ---
function closeModal() {
    const overlay = document.getElementById('modal-overlay');
    if (overlay) overlay.style.display = 'none';
    const sub = document.getElementById('modal-submit');
    if (sub) sub.onclick = null;
}

function openCameraModal({ title, init = {}, onSubmit }) {
    const overlay = document.getElementById('modal-overlay');
    const body = document.getElementById('modal-body');
    const titleEl = document.getElementById('modal-title');

    if (!overlay || !body) return;
    titleEl.textContent = title;

    // ... (HTML Generation for Modal Content - simplified for brevity, assume slightly cleaner HTML)
    // Reuse the previous innerHTML generation but wrap it in a function if needed.
    // Ideally, we move this big HTML string to a separate render function or template.
    // I'll keep the logic inline but clean for this artifact.

    // ... [HTML Generation Logic from before] ... 
    // Just ensuring specific "Save" button logic uses runAsyncAction

    // For this rewrite, I should probably implement the FULL modal content otherwise the file is incomplete.

    const { name = '', source_url = '', zone = '', enabled = true, embed_fps = 15 } = init;
    let initialTab = 'url';
    if ((source_url || '').startsWith('device://')) initialTab = 'device';
    else if ((source_url || '').startsWith('file://') || /[\\/]/.test(source_url || '')) initialTab = 'file';

    body.innerHTML = `
    <div class="modal-form">
        <div class="form-group">
            <label>Camera Name</label>
            <input id="cam-name" class="input" value="${name || ''}" placeholder="e.g. Front Door">
        </div>
        
        <div class="tabs-nav">
            <button class="tab-btn active" data-tab="url">URL/RTSP</button>
            <button class="tab-btn" data-tab="device">Device</button>
            <button class="tab-btn" data-tab="file">File</button>
        </div>

        <div class="tab-content active" id="pane-url">
            <div class="form-group">
                <label>Stream URL</label>
                <input id="cam-src-url" class="input" value="${initialTab === 'url' ? (source_url || '') : ''}" placeholder="rtsp://...">
            </div>
        </div>

        <div class="tab-content" id="pane-device">
            <div class="form-group row">
                <select id="cam-src-device" class="select expanded"></select>
                <button id="cam-device-refresh" class="btn">↻</button>
            </div>
        </div>

        <div class="tab-content" id="pane-file">
             <div class="form-group" style="display:none;">
                <label>File Path</label>
                <input id="cam-src-file" class="input" value="${initialTab === 'file' ? (source_url || '').replace(/^file:\/\//i, '') : ''}">
             </div>
             <div class="form-group">
                <label>Select Video File</label>
                <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap;">
                    <label class="btn secondary" style="cursor:pointer; min-width:112px;">
                        Choose File
                        <input id="cam-file-picker" type="file" accept="video/*" style="display:none;">
                    </label>
                    <div id="cam-file-status" class="muted" style="font-size:0.9em; padding-top:2px;">
                        ${initialTab === 'file' ? '✅ current file set' : 'No file selected'}
                    </div>
                </div>
             </div>
        </div>

        <div class="form-row" style="margin-top: 2px;">
            <div class="form-group">
                <label>Zone</label>
                <input id="cam-zone" class="input" value="${zone || ''}">
            </div>
            <div class="form-group">
                <label>FPS</label>
                <input id="cam-embed-fps" class="input" type="number" step="0.5" value="${embed_fps}">
            </div>
        </div>
        
        <div class="form-group checkbox-group">
            <input id="cam-enabled" type="checkbox" ${enabled ? 'checked' : ''}>
            <label for="cam-enabled">Enable Camera</label>
        </div>
    </div>`;

    // Tab Logic
    const tabs = body.querySelectorAll('.tab-btn');
    const panes = body.querySelectorAll('.tab-content');
    let curTab = initialTab;

    function setTab(t) {
        curTab = t;
        tabs.forEach(b => b.classList.toggle('active', b.dataset.tab === t));
        panes.forEach(p => p.classList.toggle('active', p.id === `pane-${t}`));
        if (t === 'device') loadDevices();
    }

    tabs.forEach(b => b.onclick = () => setTab(b.dataset.tab));
    setTab(initialTab);

    // Device Loader
    async function loadDevices() {
        const sel = document.getElementById('cam-src-device');
        sel.innerHTML = '<option>Loading...</option>';
        try {
            const d = await fetchJSON('/admin/devices');
            sel.innerHTML = '';
            if (!d.devices?.length) sel.innerHTML = '<option>No devices found</option>';
            else {
                d.devices.forEach(dev => {
                    const opt = document.createElement('option');
                    opt.value = dev.index;
                    opt.textContent = dev.name;
                    sel.appendChild(opt);
                });
            }
        } catch { sel.innerHTML = '<option>Error</option>'; }
    }
    document.getElementById('cam-device-refresh').onclick = loadDevices;

    // Auto-Upload on Select
    const filePicker = document.getElementById('cam-file-picker');
    if (filePicker) {
        filePicker.onchange = function () {
            const file = this.files[0];
            if (!file) return;

            const statusEl = document.getElementById('cam-file-status');
            statusEl.textContent = `Uploading ${file.name}...`;
            statusEl.style.color = 'var(--text-muted)'; // reset color

            const fd = new FormData();
            fd.append('file', file);

            // Using fetch directly or runAsyncAction wrapper if we had a button context
            // Here we just do it async
            fetch('/admin/upload_video', { 
                method: 'POST', 
                body: fd, 
                headers: localStorage.getItem('authToken') ? { 'Authorization': 'Bearer ' + localStorage.getItem('authToken') } : {} 
            })
                .then(r => r.json())
                .then(j => {
                    if (j.path) {
                        document.getElementById('cam-src-file').value = j.path;
                        statusEl.textContent = `✅ Ready: ${file.name}`;
                        statusEl.style.color = 'var(--success)';
                        // Auto-set name if empty
                        const nameInp = document.getElementById('cam-name');
                        if (!nameInp.value) nameInp.value = file.name;
                    } else {
                        throw new Error('No path returned');
                    }
                })
                .catch(e => {
                    console.error(e);
                    statusEl.textContent = '❌ Upload failed';
                    statusEl.style.color = 'var(--error)';
                });
        };
    }

    overlay.style.display = 'flex';

    document.getElementById('modal-submit').onclick = function () {
        const btn = this;
        // Construct payload logic
        let src = '';
        if (curTab === 'url') src = document.getElementById('cam-src-url').value.trim();
        else if (curTab === 'file') {
            const f = document.getElementById('cam-src-file').value.trim();
            if (f) src = `file://${f}`;
        } else if (curTab === 'device') {
            const v = document.getElementById('cam-src-device').value;
            if (v) src = `device://${v}`;
        }

        let n = document.getElementById('cam-name').value.trim();
        if (!n) n = `Camera ${src}`; // Simplification

        if (!src) return alert('Source required');

        const payload = {
            name: n,
            source_url: src,
            zone: document.getElementById('cam-zone').value.trim() || null,
            enabled: document.getElementById('cam-enabled').checked,
            embed_fps: parseFloat(document.getElementById('cam-embed-fps').value) || 1
        };

        runAsyncAction(btn, async () => {
            await onSubmit(payload);
            closeModal();
            loadAdminCameras();
        }, 'Saving...');
    };
}

function openAddCameraModal() {
    // Pre-check limit before opening the form
    fetchJSON('/api/system/limits').then(limits => {
        if (limits.at_limit) {
            showToast(
                `⚠️ Camera limit reached (${limits.current_cameras}/${limits.max_cameras}). ` +
                `Remove a camera or raise 'max_cameras' in Settings.`,
                'error',
                6000
            );
            return;
        }
        openCameraModal({
            title: 'Add Camera',
            init: { enabled: true, embed_fps: parseFloat(limits.detection_fps) || 15 },
            onSubmit: async (p) => {
                try {
                    return await fetchJSON('/admin/cameras', { method: 'POST', body: JSON.stringify(p) });
                } catch (err) {
                    const msg = err?.detail || err?.message || 'Failed to add camera';
                    showToast(`❌ ${msg}`, 'error', 7000);
                    throw err;  // Re-throw so runAsyncAction doesn't close the modal
                }
            }
        });
    }).catch(() => {
        // Fallback if /api/system/limits fails — open normally
        openCameraModal({
            title: 'Add Camera',
            init: { enabled: true },
            onSubmit: (p) => fetchJSON('/admin/cameras', { method: 'POST', body: JSON.stringify(p) })
        });
    });
}

function openEditCameraModal(cam) {
    openCameraModal({
        title: 'Edit Camera',
        init: cam,
        onSubmit: (p) => fetchJSON(`/admin/cameras/${cam.id}`, { method: 'PATCH', body: JSON.stringify(p) })
    });
}

function openAddUserModal() {
    const overlay = document.getElementById('modal-overlay');
    const body = document.getElementById('modal-body');
    const title = document.getElementById('modal-title');
    if (!overlay) return;

    title.textContent = 'Add User';
    body.innerHTML = `
    <div class="modal-form">
        <div class="form-group"><label>Username</label><input id="u-user" class="input"></div>
        <div class="form-group"><label>Password</label><input id="u-pass" class="input" type="password"></div>
        <div class="form-group"><label>Role</label>
        <select id="u-role" class="select"><option value="user">User</option><option value="admin">Admin</option></select>
        </div>
    </div>`;

    overlay.style.display = 'flex';

    document.getElementById('modal-submit').onclick = function () {
        const btn = this;
        const u = document.getElementById('u-user').value.trim();
        const p = document.getElementById('u-pass').value;
        const r = document.getElementById('u-role').value;

        if (!u || !p) return alert('Required fields missing');

        runAsyncAction(btn, async () => {
            await fetchJSON('/admin/users', { method: 'POST', body: JSON.stringify({ username: u, password: p, role: r }) });
            closeModal();
            loadAdminUsers();
        }, 'Creating...');
    };
}


// --- Detection & Logs ---
async function fetchDetections() {
    try {
        const r = await fetchJSON('/api/detections');
        DETECTION_STATE = r.detections || {};
        renderDetectionTabs();
        // Ensure badges update
        updateDetectionBadges();
    } catch (e) { }
}

function renderDetectionTabs() {
    const tabs = document.getElementById('cam-tabs');
    if (!tabs) return;
    tabs.innerHTML = '';

    CAMS.forEach(c => {
        const d = DETECTION_STATE[c.id] || {};
        const label = d.label || 'Normal';
        const isAlert = d.is_alert;

        const chip = document.createElement('div');
        chip.className = `status-chip ${isAlert ? 'alert' : 'normal'}`;
        chip.textContent = `${c.name || c.id}: ${label}`;
        chip.onclick = () => {
            const sel = document.getElementById('camera-select');
            sel.value = c.id;
            VIEW_MODE = 'single';
            localStorage.setItem('viewMode', VIEW_MODE);
            applyViewMode();
        };
        tabs.appendChild(chip);
    });
}

function updateDetectionBadges() {
    // Add red borders/badges to cards based on DETECTION_STATE
    const cards = document.querySelectorAll('.cam-card');
    cards.forEach(c => {
        const id = c.dataset.camId;
        const d = DETECTION_STATE[id] || {};
        const isAlert = d.is_alert;
        const label = d.label || 'Normal';
        const badge = c.querySelector('.cam-badge');
        const confBadge = c.querySelector('.cam-conf');
        const aiToggleChk = c.querySelector('.cam-ai-toggle-chk');

        if (aiToggleChk && typeof d.ai_enabled === 'boolean') {
            aiToggleChk.checked = d.ai_enabled;
            const span = aiToggleChk.previousElementSibling;
            if (span) span.style.color = d.ai_enabled ? 'var(--accent-secondary)' : 'var(--text-muted)';
        }

        c.classList.toggle('card-alert', !!isAlert);
        if (badge) {
            badge.textContent = label;
            // Map colors
            if (isAlert) badge.className = 'pill cam-badge pill-error pulse-text';
            else if (label === 'Normal') badge.className = 'pill cam-badge pill-success';
            else badge.className = 'pill cam-badge pill-warn';
        }

        // Confidence Display
        if (confBadge) {
            if (d.score && label !== 'Normal') {
                confBadge.textContent = `${Math.round(d.score * 100)}%`;
                confBadge.classList.remove('hidden');
            } else {
                confBadge.classList.add('hidden');
            }
        }

        // Detailed Class Probabilities Logic
        const probsDiv = c.querySelector('.cam-probs');
        const statsBox = c.querySelector('.cam-stats-box');
        const statsGrid = c.querySelector('.cam-stats-grid');
        const isSingleTarget = VIEW_MODE === 'single' && c.classList.contains('focused');

        if (d.class_probs && SHOW_CONFIDENCES) {
            const sorted = Object.entries(d.class_probs).sort((a, b) => b[1] - a[1]);

            // 1. Frame Overlay (Grid View only)
            if (probsDiv) {
                if (!isSingleTarget && sorted.length > 0) {
                    probsDiv.innerHTML = sorted.map(([k, v]) => `
                        <div class="probs-row">
                            <span class="probs-label">${k}</span>
                            <div class="probs-bar-track">
                                <div class="probs-bar-fill" style="width:${v * 100}%"></div>
                            </div>
                            <span class="probs-val">${Math.round(v * 100)}%</span>
                        </div>
                    `).join('');
                    probsDiv.classList.remove('hidden');
                } else {
                    probsDiv.classList.add('hidden');
                }
            }

            // 2. Dedicated Stats Box Below Video (Single View Target only)
            if (statsBox && statsGrid) {
                if (isSingleTarget && sorted.length > 0) {
                    statsGrid.innerHTML = sorted.map(([k, v]) => `
                        <div class="stats-bar-item">
                            <div class="stats-bar-top">
                                <span style="color:${CLASS_COLORS[k] || 'var(--text-primary)'}">${k}</span>
                                <span>${Math.round(v * 100)}%</span>
                            </div>
                            <div class="stats-bar-track">
                                <div class="stats-bar-fill" style="width:${v * 100}%; background-color:${CLASS_COLORS[k] || 'var(--success)'};"></div>
                            </div>
                        </div>
                    `).join('');
                    statsBox.classList.remove('hidden');
                } else {
                    statsBox.classList.add('hidden');
                }
            }
        } else {
            if (probsDiv) probsDiv.classList.add('hidden');
            if (statsBox) statsBox.classList.add('hidden');
        }

        // Alert Trigger logic
        if (isAlert && label !== 'Normal') {
            const lastState = c.dataset.alerted;
            // Throttle alerts: only alert if state (label) changed OR it's been > 15s since last alert
            // We use a simple timestamp check if storing ts in dataset
            const now = Date.now();
            const lastTs = parseInt(c.dataset.alertTs || '0');

            if (label !== lastState || (now - lastTs > 15000)) {
                const cam = CAMS.find(c => c.id == id);
                const camName = cam ? (cam.name || id) : id;
                ALERTS.add(`${label} detected on ${camName}`, 'critical');
                c.dataset.alerted = label;
                c.dataset.alertTs = now;
            }
        }
    });
}

// Logs, AlertSound and BigAlert same as before...
// (Including condensed versions for brevity)
function playAlertSound() {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.connect(g); g.connect(ctx.destination);
    osc.type = 'sawtooth'; osc.frequency.value = 440;
    osc.start(); g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
    osc.stop(ctx.currentTime + 0.5);
}

function showBigAlert(msg) {
    // Check if alert popup is disabled in settings
    if (db_manager.get_setting("show_alert_popup", "true") !== "true") return;

    let mod = document.getElementById('alert-modal');
    if (!mod) {
        mod = document.createElement('div');
        mod.id = 'alert-modal';
        document.body.appendChild(mod);
    }

    mod.innerHTML = `
        <div style="font-size:48px;">⚠️</div>
        <div style="font-size:24px; font-weight:bold;">CRITICAL ALERT</div>
        <div style="font-size:18px;">${msg}</div>
        <button class="btn" style="background:white; color:black; border:none; margin-top:10px;" onclick="document.getElementById('alert-modal').style.display='none'">DISMISS</button>
    `;

    mod.style.display = 'flex';

    // Auto-dismiss safely
    if (window.alertTimeout) clearTimeout(window.alertTimeout);
    window.alertTimeout = setTimeout(() => {
        mod.style.display = 'none';
    }, 10000);
}

function zoomChart(chartId, title) {
    const original = document.getElementById(chartId);
    if (!original) return;

    const overlay = document.getElementById('modal-overlay');
    const body = document.getElementById('modal-body');
    const head = document.getElementById('modal-title');
    if (!overlay || !body) return;

    head.textContent = title;
    // Clone the inner content to the modal body
    body.innerHTML = `
        <div class="zoomed-chart-wrap" style="height: 500px; padding: 20px; background: var(--bg-card); border-radius: 8px;">
            ${original.innerHTML}
        </div>
    `;

    // Adjust styles for the zoomed version (e.g., make bars wider)
    const wrap = body.querySelector('.chart-bars');
    overlay.style.display = 'flex';
    const subBtn = document.getElementById('modal-submit');
    if (subBtn) subBtn.style.display = 'none';

    // Temporary close handler to restore button
    const cancelBtn = document.getElementById('modal-cancel');
    if (cancelBtn) {
        const oldCancel = cancelBtn.onclick;
        cancelBtn.onclick = () => {
            if (subBtn) subBtn.style.display = '';
            closeModal();
            cancelBtn.onclick = oldCancel;
        };
    }
}

async function pollEmbeddings() {
    try {
        const r = await fetchJSON('/api/embeddings/stats');
        setText('embed-count', `${r.count} Embeddings`);
    } catch { }
}


// --- User Event Feed ---
async function fetchUserEvents() {
    // Only valid for dashboard tab
    if (document.hidden || !document.getElementById('user-events')) return;
    try {
        const feed = document.getElementById('events-feed');
        if (!feed) return;

        const limit = document.getElementById('events-limit')?.value || 20;
        // const events = await fetchJSON(`/api/events/feed?limit=${limit}`);
        // Endpoint might be /api/events or /api/detections/feed?
        // Checking app.py found /api/events/feed
        const events = await fetchJSON(`/api/events/feed?limit=${limit}`);

        feed.innerHTML = '';
        if (!events || events.length === 0) {
            feed.innerHTML = '<div class="muted" style="padding:20px;">No recent events found.</div>';
            return;
        }

        events.forEach(ev => {
            const card = document.createElement('div');
            card.className = 'card event-card';
            if (ev.label !== 'Normal') card.classList.add('card-alert');

            // Format time
            const ts = formatTimestamp(ev.timestamp);

            card.innerHTML = `
                <div class="card-header" style="justify-content:space-between;">
                    <span class="pill ${ev.label === 'Normal' ? 'pill-success' : 'pill-error'}">${ev.label}</span>
                    <span class="muted" style="font-size:12px;">${ts}</span>
                </div>
                <div class="card-body" style="padding-top:8px;">
                    <p><strong>Cam:</strong> ${ev.camera_id}</p>
                    ${ev.label !== 'Normal' ? `<p class="error-text">Confidence: ${(ev.confidence * 100).toFixed(0)}%</p>` : ''}
                    <p class="muted" style="font-size:11px;">Duration: ${ev.duration ? ev.duration.toFixed(1) : 0}s</p>
                </div>
                <div class="card-footer">
                    <button class="btn btn-sm primary" style="width:100%;">📹 Play Clip</button>
                </div>
            `;
            const playBtn = card.querySelector('.btn');
            playBtn.onclick = () => openClipModal(ev.camera_id, ev.timestamp);
            feed.appendChild(card);
        });

    } catch (e) {
        console.error('Fetch user events failed', e);
    }
}

// Bind refresh button
const refreshEventsBtn = document.getElementById('events-refresh');
if (refreshEventsBtn) {
    refreshEventsBtn.onclick = () => {
        const icon = refreshEventsBtn.innerHTML;
        refreshEventsBtn.textContent = '...';
        fetchUserEvents().finally(() => refreshEventsBtn.innerHTML = icon);
    };
}

// --- Video Clip Modal ---
function openClipModal(cameraId, timestampIso) {
    const overlay = document.getElementById('clip-modal-overlay');
    const player = document.getElementById('clip-video-player');
    const beforeInp = document.getElementById('clip-before');
    const afterInp = document.getElementById('clip-after');
    const reloadBtn = document.getElementById('clip-reload-btn');
    const title = document.getElementById('clip-modal-title');

    if (!overlay || !player || !beforeInp || !afterInp || !reloadBtn) return;

    title.textContent = `📹 Clip: ${cameraId} at ${formatTimestamp(timestampIso)}`;

    const loadVideo = () => {
        const b = parseInt(beforeInp.value) || 5;
        const a = parseInt(afterInp.value) || 5;
        const url = `/api/video/clip?camera_id=${encodeURIComponent(cameraId)}&timestamp=${encodeURIComponent(timestampIso)}&before=${b}&after=${a}`;
        player.src = url;
        player.play().catch(e => console.warn("Autoplay prevented:", e));
    };

    reloadBtn.onclick = () => {
        const originalText = reloadBtn.textContent;
        reloadBtn.textContent = 'Loading...';
        reloadBtn.disabled = true;
        loadVideo();
        player.onloadeddata = () => {
            reloadBtn.textContent = originalText;
            reloadBtn.disabled = false;
        };
        player.onerror = () => {
            reloadBtn.textContent = originalText;
            reloadBtn.disabled = false;
            showToast('Failed to load video clip', 'error');
        };
    };

    // Initial load
    loadVideo();

    // Show modal
    overlay.style.display = 'flex';
}

document.addEventListener('DOMContentLoaded', () => {
    // Attach close Modal for Clip
    const clipOverlay = document.getElementById('clip-modal-overlay');
    const clipClose = document.getElementById('clip-modal-close');
    const clipPlayer = document.getElementById('clip-video-player');

    const closeClip = () => {
        if (clipOverlay) clipOverlay.style.display = 'none';
        if (clipPlayer) {
            clipPlayer.pause();
            clipPlayer.src = '';
        }
    };

    if (clipClose) clipClose.addEventListener('click', closeClip);
    if (clipOverlay) {
        clipOverlay.addEventListener('click', (e) => {
            if (e.target === clipOverlay) closeClip();
        });
    }

    // Standalone Custom Video View page implementation
    window.initCustomVideoPage = () => {
        const camSel = document.getElementById('page-custom-cam');
        const startInp = document.getElementById('page-custom-start');
        const endInp = document.getElementById('page-custom-end');
        const submitBtn = document.getElementById('page-custom-submit');
        const loaderBox = document.getElementById('page-custom-loader');
        const progressBar = document.getElementById('page-custom-progress-bar');
        const progressText = document.getElementById('page-custom-progress-text');
        const outputBlock = document.getElementById('page-custom-output');
        const player = document.getElementById('page-custom-player');

        if (camSel) {
            camSel.innerHTML = CAMS.map(c => `<option value="${c.id}">${c.name || c.id}</option>`).join('');
            const activeCam = document.getElementById('camera-select')?.value;
            if (activeCam) camSel.value = activeCam;
        }

        const pad = n => String(n).padStart(2, '0');
        const fmtLocal = dt => `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;

        if (startInp && !startInp.value) {
            const startDt = new Date(Date.now() - 5 * 60000);
            startInp.value = fmtLocal(startDt);
        }

        if (endInp && !endInp.value) {
            endInp.value = fmtLocal(new Date());
        }

        if (submitBtn && !submitBtn.dataset.bound) {
            submitBtn.dataset.bound = 'true';
            submitBtn.addEventListener('click', async () => {
                const camId = camSel?.value;
                const startVal = startInp?.value;
                const endVal = endInp?.value;

                if (!camId || !startVal || !endVal) {
                    showToast('Please specify target camera stream, start time, and end time.', 'warn');
                    return;
                }

                const startDt = new Date(startVal);
                const endDt = new Date(endVal);
                const nowDt = new Date();

                if (startDt >= endDt) {
                    showToast('Start time must be strictly prior to end time.', 'warn');
                    return;
                }

                if (startDt > nowDt || endDt > nowDt) {
                    showToast('Requested temporal window cannot exceed current live clock time.', 'warn');
                    return;
                }

                submitBtn.disabled = true;
                const originalText = submitBtn.innerHTML;
                submitBtn.innerHTML = '⏳ Compiling Custom MP4... Please Wait';
                if (outputBlock) outputBlock.style.display = 'none';
                
                if (loaderBox && progressBar && progressText) {
                    loaderBox.style.display = 'flex';
                    progressBar.style.width = '10%';
                    progressText.textContent = 'Querying Frames...';
                }

                // Simulate incremental processing animation while backend compiles frames synchronously
                let simProgress = 10;
                const simInterval = setInterval(() => {
                    if (simProgress < 90) {
                        simProgress += Math.floor(Math.random() * 8) + 2;
                        if (simProgress > 90) simProgress = 90;
                        if (progressBar) progressBar.style.width = simProgress + '%';
                        if (progressText) {
                            if (simProgress > 60) progressText.textContent = 'Rendering Video Encoding...';
                            else if (simProgress > 30) progressText.textContent = 'Assembling Temporal Chunks...';
                        }
                    }
                }, 400);

                try {
                    const startIso = startDt.toISOString();
                    const endIso = endDt.toISOString();
                    const url = `/api/video/custom?camera_id=${encodeURIComponent(camId)}&start_time=${encodeURIComponent(startIso)}&end_time=${encodeURIComponent(endIso)}`;

                    // Check output video source link via redirect
                    const res = await fetch(url);
                    clearInterval(simInterval);

                    if (!res.ok) {
                        throw new Error('Failed to render temporal stream sequence.');
                    }
                    
                    if (progressBar && progressText) {
                        progressBar.style.width = '100%';
                        progressText.textContent = 'Complete!';
                    }

                    // The server redirected to the video url
                    const finalUrl = res.url;
                    if (outputBlock && player) {
                        player.src = finalUrl;
                        outputBlock.style.display = 'flex';
                        showToast('Custom video output stream compiled successfully!', 'success');
                    } else {
                        window.open(finalUrl, '_blank');
                    }
                } catch (err) {
                    clearInterval(simInterval);
                    if (progressBar && progressText) {
                        progressBar.style.width = '100%';
                        progressBar.style.background = 'var(--error)';
                        progressText.textContent = 'Failed';
                    }
                    console.error(err);
                    showToast(err.message || 'Error compiling custom MP4 stream.', 'error');
                } finally {
                    setTimeout(() => {
                        if (loaderBox) loaderBox.style.display = 'none';
                        if (progressBar) {
                            progressBar.style.width = '0%';
                            progressBar.style.background = '';
                        }
                    }, 1500);
                    submitBtn.disabled = false;
                    submitBtn.innerHTML = originalText;
                }
            });
        }
    };
});

