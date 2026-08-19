/**
 * Side panel controller — Modular Fact & AI Verification UI for Authenix.
 *
 * Modes:
 *   - 'both': Verifies factual claims + scans AI text likelihood simultaneously (Default)
 *   - 'claims': Fact-checks factual claims only
 *   - 'ai': Forensic AI Spotter only
 *
 * @module sidepanel
 */

import { getToken, getModePreference, saveModePreference } from '../lib/storage.js';
import {
    getBalance,
    syncWebAuthSession,
    initiateGoogleLogin,
    analyzeModular,
    createCheckout,
    MIN_AI_WORDS
} from '../lib/api.js';

// --- DOM Elements ---
const balanceEl = document.getElementById('sp-balance');
const streakEl = document.getElementById('sp-streak');
const authSection = document.getElementById('sp-auth-prompt');
const mainSection = document.getElementById('sp-main-content');
const authStatus = document.getElementById('sp-auth-status');
const googleLoginBtn = document.getElementById('sp-google-login-btn');
const modeButtons = document.querySelectorAll('.sp-mode-btn');

const inputText = document.getElementById('sp-input-text');
const wordCountEl = document.getElementById('sp-word-count');
const clearBtn = document.getElementById('sp-clear-btn');
const verifyBtn = document.getElementById('sp-verify-btn');
const resultsFeed = document.getElementById('sp-results');
const lowBalanceBar = document.getElementById('sp-low-balance');
const buyLink = document.getElementById('sp-buy-link');

// --- State ---
let cardCounter = 0;
let currentBalance = null;
let currentMode = 'both';
let isProcessing = false;

const BALANCE_CACHE_KEY = 'authenix_balance_cache';
const BALANCE_CACHE_TTL_MS = 60000;

// --- Initialization ---
document.addEventListener('DOMContentLoaded', async () => {
    currentMode = await getModePreference();
    updateModeButtonsUI(currentMode);
    setupEventListeners();

    const isAuthenticated = await ensureAuthentication();
    if (!isAuthenticated) {
        return;
    }

    await loadUserInfo();

    // Handshake: pick up queued selection from right-click context menu
    chrome.runtime.sendMessage({ type: 'SIDE_PANEL_READY' }, (response) => {
        if (response?.text) {
            inputText.value = response.text;
            updateInputState();
            handleVerify();
        }
    });

    // Fast path: receives selection directly if side panel was already open
    chrome.runtime.onMessage.addListener((message) => {
        if (message.type === 'FACTCHECK_SELECTION' && message.text) {
            inputText.value = message.text;
            updateInputState();
            handleVerify();
        }
    });
});

/**
 * Check authentication or auto-sync with active web session cookies.
 * @returns {Promise<boolean>}
 */
async function ensureAuthentication() {
    let token = await getToken();
    if (token) {
        showAuthenticatedUI();
        return true;
    }

    // Try auto-detecting existing web session from browser cookies (0 clicks)
    if (authStatus) authStatus.textContent = 'Auto-detecting web session…';
    const syncedUser = await syncWebAuthSession();
    if (syncedUser) {
        showAuthenticatedUI();
        return true;
    }

    showDisconnectedUI();
    return false;
}

function showAuthenticatedUI() {
    authSection.classList.add('hidden');
    mainSection.classList.remove('hidden');
}

function showDisconnectedUI() {
    mainSection.classList.add('hidden');
    authSection.classList.remove('hidden');
    if (authStatus) authStatus.textContent = 'Sign in to access Authenix verification';
}

// --- Load user info (balance & streak) ---
async function loadUserInfo() {
    try {
        const cached = await chrome.storage.local.get(BALANCE_CACHE_KEY);
        const cache = cached[BALANCE_CACHE_KEY];
        if (cache && (Date.now() - cache.ts < BALANCE_CACHE_TTL_MS)) {
            applyBalanceUI(cache.balance, cache.streak);
            return;
        }

        const response = await getBalance();
        const balance = response.balance ?? 0;
        const streak = response.streak?.current_streak ?? 0;

        await chrome.storage.local.set({
            [BALANCE_CACHE_KEY]: { balance, streak, ts: Date.now() },
        });

        applyBalanceUI(balance, streak);
    } catch (error) {
        console.error('[Authenix] Failed to load balance:', error);
        balanceEl.textContent = '— CP';
    }
}

function applyBalanceUI(balance, streak) {
    currentBalance = balance;
    balanceEl.textContent = `${balance} CP`;
    streakEl.textContent = `🔥 ${streak}`;
    if (balance <= 0) {
        lowBalanceBar.classList.remove('hidden');
    } else {
        lowBalanceBar.classList.add('hidden');
    }
}

// --- Event Listeners Setup ---
function setupEventListeners() {
    // 1-Click Google OAuth
    googleLoginBtn?.addEventListener('click', async () => {
        googleLoginBtn.disabled = true;
        googleLoginBtn.innerHTML = '<span class="sp-spinner"></span> Connecting Google…';
        try {
            const user = await initiateGoogleLogin();
            if (user) {
                showAuthenticatedUI();
                await loadUserInfo();
            } else {
                if (authStatus) authStatus.textContent = 'Sign in canceled or timed out. Please retry.';
            }
        } catch (err) {
            if (authStatus) authStatus.textContent = `Sign in error: ${err.message}`;
        } finally {
            googleLoginBtn.disabled = false;
            googleLoginBtn.innerHTML = `
                <svg class="google-icon" viewBox="0 0 24 24" width="18" height="18">
                    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/>
                    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/>
                </svg>
                Sign in with Google
            `;
        }
    });

    // 3-Way Mode selector
    modeButtons.forEach((btn) => {
        btn.addEventListener('click', async () => {
            currentMode = btn.dataset.mode;
            updateModeButtonsUI(currentMode);
            await saveModePreference(currentMode);
        });
    });

    // Input handlers
    inputText.addEventListener('input', () => updateInputState());
    clearBtn.addEventListener('click', () => {
        inputText.value = '';
        updateInputState();
        inputText.focus();
    });

    // Submit handler
    verifyBtn.addEventListener('click', () => handleVerify());
    inputText.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !verifyBtn.disabled) {
            e.preventDefault();
            handleVerify();
        }
    });

    // Stripe checkout triggers
    balanceEl.addEventListener('click', () => triggerCheckout());
    buyLink.addEventListener('click', () => triggerCheckout());
}

function updateModeButtonsUI(activeMode) {
    modeButtons.forEach((btn) => {
        const isActive = btn.dataset.mode === activeMode;
        btn.classList.toggle('active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
}

function updateInputState() {
    const text = inputText.value.trim();
    const words = text ? text.split(/\s+/).filter(Boolean).length : 0;

    wordCountEl.textContent = `${words} word${words === 1 ? '' : 's'}`;
    clearBtn.classList.toggle('hidden', text.length === 0);
    verifyBtn.disabled = text.length === 0;
}

// --- Main Verification Flow ---
async function handleVerify() {
    if (isProcessing) return;
    const text = inputText.value.trim();
    if (!text) return;

    isProcessing = true;
    clearEmptyState();
    const cardId = createPendingCard(text, currentMode);
    setButtonLoading(verifyBtn, 'Verifying…');

    let rateLimited = false;
    try {
        const result = await analyzeModular(text, currentMode);
        updateCardWithResult(cardId, result);

        if (result.new_balance !== undefined) {
            currentBalance = result.new_balance;
            balanceEl.textContent = `${currentBalance} CP`;
            chrome.storage.local.remove(BALANCE_CACHE_KEY);
        }
    } catch (error) {
        updateCardError(cardId, error.message);
        if (error.status === 429) {
            rateLimited = true;
            showRateLimitCountdown(verifyBtn, 'Verify', error.retryAfterSec);
        } else if (error.status === 402 || error.status === 403) {
            triggerCheckout();
        }
    } finally {
        isProcessing = false;
        if (!rateLimited) {
            resetButton(verifyBtn, 'Verify');
            inputText.value = '';
            updateInputState();
        }
    }
}

// --- Card Rendering ---

/** Create a pending skeleton card */
function createPendingCard(text, mode) {
    const id = `sp-card-${++cardCounter}`;
    const card = document.createElement('div');
    card.className = 'sp-card pending';
    card.id = id;

    const preview = text.length > 140 ? text.substring(0, 140) + '…' : text;
    const modeBadgeLabel = mode === 'both' ? 'AI + Fact Check' : mode === 'claims' ? 'Fact Check' : 'AI Spotter';

    card.innerHTML = `
        <div class="sp-card-header">
            <div class="sp-badges-group">
                <span class="sp-verdict PENDING">
                    <span class="sp-spinner"></span> ${modeBadgeLabel}…
                </span>
            </div>
            <span class="sp-card-time">${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
        </div>
        <p class="sp-claim-text">"${escapeHtml(preview)}"</p>
        <div class="sp-card-details"></div>
    `;

    resultsFeed.insertBefore(card, resultsFeed.firstChild);
    return id;
}

/** Update card with final modular verification results */
function updateCardWithResult(cardId, data) {
    const card = document.getElementById(cardId);
    if (!card) return;

    card.classList.remove('pending');
    const headerBadges = card.querySelector('.sp-badges-group');
    const detailsContainer = card.querySelector('.sp-card-details');

    let badgesHtml = '';
    let detailsHtml = '';

    // 1. Claims Verdict Badge (for 'both' or 'claims' mode)
    if (data.claims) {
        const verdict = data.claims.verdict || 'NOT_VERIFIED';
        badgesHtml += `<span class="sp-verdict ${verdict}">${verdict.replace(/_/g, ' ')}</span>`;

        if (data.claims.explanation) {
            const safeExp = typeof DOMPurify !== 'undefined'
                ? DOMPurify.sanitize(data.claims.explanation)
                : escapeHtml(data.claims.explanation);
            detailsHtml += `<p class="sp-explanation">${safeExp}</p>`;
        }

        if (data.claims.fallacy) {
            detailsHtml += `<span class="sp-fallacy">⚠️ ${escapeHtml(data.claims.fallacy)}</span>`;
        }

        if (data.claims.sources && Array.isArray(data.claims.sources) && data.claims.sources.length > 0) {
            const validSources = data.claims.sources.filter(u => sanitizeUrl(u));
            if (validSources.length > 0) {
                detailsHtml += `<div class="sp-sources"><strong>Verified Sources:</strong>${validSources.map(u =>
                    `<a href="${sanitizeUrl(u)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(u)}">🔗 ${formatUrl(u)}</a>`
                ).join('')}</div>`;
            }
        }
    }

    // 2. AI Probability Pill & Forensics (for 'both' or 'ai' mode)
    if (data.ai) {
        if (data.ai.skipped) {
            badgesHtml += `<span class="sp-ai-pill ai-short" title="${escapeHtml(data.ai.reason)}">🤖 Short (&lt;${MIN_AI_WORDS}w)</span>`;
        } else {
            const prob = data.ai.ai_probability ?? 50;
            const conf = data.ai.confidence || 'MED';
            const pillClass = prob >= 70 ? 'ai-high' : prob >= 40 ? 'ai-med' : 'ai-low';
            const probLabel = prob >= 70 ? `${prob}% AI Likely` : prob <= 30 ? `${100 - prob}% Human` : `${prob}% AI Prob`;

            badgesHtml += `<span class="sp-ai-pill ${pillClass}">🤖 ${probLabel}</span>`;

            // Forensics breakdown box
            const aiIndicators = (data.ai.ai_indicators || []).slice(0, 3);
            const humanIndicators = (data.ai.human_indicators || []).slice(0, 3);

            detailsHtml += `
                <div class="sp-ai-forensics">
                    <div class="sp-ai-forensics-header">
                        <span class="sp-ai-forensics-title">🤖 AI Forensics (${prob}%)</span>
                        <span class="sp-ai-confidence-tag">Confidence: ${conf}</span>
                    </div>
                    <div class="sp-ai-bar-track">
                        <div class="sp-ai-bar-fill ${pillClass}" style="width: ${prob}%"></div>
                    </div>
                    ${data.ai.explanation ? `<p class="sp-explanation" style="font-size: 0.72rem; margin-top: 4px;">${escapeHtml(data.ai.explanation)}</p>` : ''}
                    <div class="sp-ai-indicators">
                        ${aiIndicators.map(ind => `<span class="sp-indicator-tag">Marker: ${escapeHtml(ind)}</span>`).join('')}
                        ${humanIndicators.map(ind => `<span class="sp-indicator-tag" style="background: rgba(16, 185, 129, 0.15); color: #a7f3d0;">Human trait: ${escapeHtml(ind)}</span>`).join('')}
                    </div>
                </div>
            `;
        }
    }

    headerBadges.innerHTML = badgesHtml;
    detailsContainer.innerHTML = detailsHtml;

    // Card expand toggle
    card.classList.add('expanded'); // Auto-expand upon arrival
    card.addEventListener('click', () => card.classList.toggle('expanded'));
}

/** Show error state on card */
function updateCardError(cardId, message) {
    const card = document.getElementById(cardId);
    if (!card) return;

    card.classList.remove('pending');
    const headerBadges = card.querySelector('.sp-badges-group');
    headerBadges.innerHTML = '<span class="sp-verdict FALSE">ERROR</span>';

    const details = card.querySelector('.sp-card-details');
    details.innerHTML = `<p class="sp-explanation" style="color: var(--color-error);">${escapeHtml(message)}</p>`;
    card.classList.add('expanded');
}

// --- Helpers ---

function clearEmptyState() {
    const empty = resultsFeed.querySelector('.sp-empty-state');
    if (empty) empty.remove();
}

function setButtonLoading(btn, label) {
    btn.disabled = true;
    btn.innerHTML = `<span class="sp-spinner"></span> <span class="btn-text">${label}</span>`;
}

function resetButton(btn, label) {
    btn.innerHTML = `<span class="btn-icon">⚡</span> <span class="btn-text">${label}</span>`;
    btn.disabled = inputText.value.trim().length === 0;
}

function showRateLimitCountdown(btn, defaultLabel, seconds = 60) {
    let remaining = Math.min(seconds, 120);
    btn.disabled = true;
    const tick = () => {
        if (remaining <= 0) {
            resetButton(btn, defaultLabel);
            return;
        }
        btn.innerHTML = `<span class="btn-text">Wait ${remaining}s</span>`;
        remaining--;
        setTimeout(tick, 1000);
    };
    tick();
}

async function triggerCheckout() {
    try {
        const result = await createCheckout();
        if (result?.url) {
            chrome.tabs.create({ url: result.url });
        }
    } catch {
        chrome.tabs.create({ url: 'https://satoricheck-829698588154.europe-west6.run.app' });
    }
}

function formatUrl(url) {
    try {
        const parsed = new URL(url);
        return parsed.hostname.replace('www.', '');
    } catch {
        return url.substring(0, 30);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text || '';
    return div.innerHTML;
}

function sanitizeUrl(url) {
    if (!url || typeof url !== 'string') return null;
    try {
        const parsed = new URL(url);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
            return parsed.href;
        }
    } catch { /* invalid URL */ }
    return null;
}
