/**
 * Side panel script — multi-mode fact-check UI for the Authenix extension.
 *
 * Modes:
 *   - factcheck: Standard fact-check (verdict cards)
 *   - ai: AI-generated text detection (probability bar)
 *   - media: Media URL authenticity analysis
 *   - history: Past fact-checks from the API
 */

import { getToken } from '../lib/storage.js';
import { getBalance, analyzeText, analyzeAI, analyzeMediaUrl, createCheckout } from '../lib/api.js';

// --- DOM refs ---
const balanceEl = document.getElementById('sp-balance');
const streakEl = document.getElementById('sp-streak');
const resultsContainer = document.getElementById('sp-results');
const lowBalanceBar = document.getElementById('sp-low-balance');
const buyLink = document.getElementById('sp-buy-link');

// Mode-specific inputs
const claimInput = document.getElementById('sp-claim');
const checkBtn = document.getElementById('sp-check-btn');
const aiTextInput = document.getElementById('sp-ai-text');
const aiBtn = document.getElementById('sp-ai-btn');
const mediaUrlInput = document.getElementById('sp-media-url');
const mediaBtn = document.getElementById('sp-media-btn');

let cardCounter = 0;
let currentBalance = null;
let currentMode = 'factcheck';
let isProcessing = false;

const BALANCE_CACHE_KEY = 'satori_balance_cache';
const BALANCE_CACHE_TTL_MS = 60000; // 60 seconds

// --- Init ---
document.addEventListener('DOMContentLoaded', async () => {
    const token = await getToken();
    if (!token) {
        // Hide inputs — they'd produce a confusing ERROR card without a valid token
        document.querySelectorAll('.sp-input-section').forEach(el => el.classList.add('hidden'));
        document.querySelector('.sp-tabs')?.classList.add('hidden');

        // Render an actionable CTA instead of a dead-end lock icon
        resultsContainer.innerHTML = `
            <div class="sp-empty-state">
                <span class="sp-empty-icon">🔒</span>
                <p>Connect your account to start fact-checking.</p>
                <button id="sp-connect-cta" class="sp-check-btn sp-connect-cta-btn">
                    Get your token →
                </button>
            </div>
        `;
        document.getElementById('sp-connect-cta')?.addEventListener('click', () => {
            chrome.tabs.create({ url: 'https://authenix.ai?ext=1' });
        });
        return;
    }

    await loadUserInfo();
    setupEventListeners();

    // Handshake: tell the service worker we're ready and pick up
    // any queued selection from a context-menu click that opened us.
    chrome.runtime.sendMessage({ type: 'SIDE_PANEL_READY' }, (response) => {
        if (response?.text) {
            switchMode('factcheck');
            claimInput.value = response.text;
            checkBtn.disabled = false;
            handleFactCheck();
        }
    });

    // Fast path: receives FACTCHECK_SELECTION directly when panel is
    // already open and the user right-clicks a new selection.
    chrome.runtime.onMessage.addListener((message) => {
        if (message.type === 'FACTCHECK_SELECTION' && message.text) {
            switchMode('factcheck');
            claimInput.value = message.text;
            checkBtn.disabled = false;
            handleFactCheck();
        }
    });
});

// --- Load user info (with 60s cache to reduce API calls) ---
async function loadUserInfo() {
    try {
        // Skip API call if cache is fresh
        const cached = await chrome.storage.local.get(BALANCE_CACHE_KEY);
        const cache = cached[BALANCE_CACHE_KEY];
        if (cache && (Date.now() - cache.ts < BALANCE_CACHE_TTL_MS)) {
            applyBalanceUI(cache.balance, cache.streak);
            return;
        }

        const response = await getBalance();
        const balance = response.balance ?? 0;
        const streak = response.streak?.current_streak ?? 0;

        // Persist to cache
        await chrome.storage.local.set({
            [BALANCE_CACHE_KEY]: { balance, streak, ts: Date.now() },
        });

        applyBalanceUI(balance, streak);
    } catch (error) {
        console.error('[Authenix] Failed to load user info:', error);
        // Show a visible error state — "— CP" is indistinguishable from a zero balance
        balanceEl.textContent = '⚠ CP';
        balanceEl.title = 'Failed to load balance — click to retry';
    }
}

/** Apply balance and streak values to the header badges. */
function applyBalanceUI(balance, streak) {
    currentBalance = balance;
    balanceEl.textContent = `${balance} CP`;
    streakEl.textContent = `🔥 ${streak}`;
    if (balance <= 0) {
        lowBalanceBar.classList.remove('hidden');
    }
}

// --- Event listeners ---
function setupEventListeners() {
    // Mode tabs
    document.querySelectorAll('.sp-tab').forEach(tab => {
        tab.addEventListener('click', () => switchMode(tab.dataset.mode));
    });

    // Fact-check mode
    claimInput.addEventListener('input', () => {
        checkBtn.disabled = claimInput.value.trim().length === 0;
    });
    checkBtn.addEventListener('click', () => handleFactCheck());
    claimInput.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !checkBtn.disabled) {
            handleFactCheck();
        }
    });

    // AI detection mode
    aiTextInput.addEventListener('input', () => {
        aiBtn.disabled = aiTextInput.value.trim().length === 0;
    });
    aiBtn.addEventListener('click', () => handleAIDetect());
    aiTextInput.addEventListener('keydown', (e) => {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !aiBtn.disabled) {
            handleAIDetect();
        }
    });

    // Media mode
    mediaUrlInput.addEventListener('input', () => {
        mediaBtn.disabled = mediaUrlInput.value.trim().length === 0;
    });
    mediaBtn.addEventListener('click', () => handleMediaCheck());
    mediaUrlInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !mediaBtn.disabled) {
            handleMediaCheck();
        }
    });

    // Buy link and CP badge both open Stripe checkout
    buyLink.addEventListener('click', (e) => {
        e.preventDefault();
        openCheckout();
    });
    balanceEl.addEventListener('click', () => openCheckout());
}

// --- Mode switching ---
function switchMode(mode) {
    currentMode = mode;

    // Update tabs
    document.querySelectorAll('.sp-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.mode === mode);
    });

    // Show/hide input sections
    document.getElementById('input-factcheck').classList.toggle('hidden', mode !== 'factcheck');
    document.getElementById('input-ai').classList.toggle('hidden', mode !== 'ai');
    document.getElementById('input-media').classList.toggle('hidden', mode !== 'media');
}

// --- Fact-check handler ---
async function handleFactCheck() {
    if (isProcessing) return;
    const text = claimInput.value.trim();
    if (!text) return;

    isProcessing = true;
    clearEmptyState();
    const cardId = createPendingCard(text);
    setButtonLoading(checkBtn, 'Checking…');

    let rateLimited = false;
    try {
        const result = await analyzeText(text);
        if (result.success !== false) {
            updateFactCheckCard(cardId, result.result || result);
            updateBalanceFromResponse(result);
        } else {
            updateCardError(cardId, result.error || 'Analysis failed');
        }
    } catch (error) {
        updateCardError(cardId, error.message);
        if (error.status === 429) {
            rateLimited = true;
            showRateLimitCountdown(checkBtn, 'Check', error.retryAfterSec);
        } else if (error.status === 403) {
            openCheckout();
        }
    } finally {
        isProcessing = false;
        if (!rateLimited) {
            resetButton(checkBtn, 'Check', claimInput);
            claimInput.value = '';
            checkBtn.disabled = true;
        }
    }
}

// --- AI detection handler ---
async function handleAIDetect() {
    if (isProcessing) return;
    const text = aiTextInput.value.trim();
    if (!text) return;

    isProcessing = true;
    clearEmptyState();
    const cardId = createPendingCard(text, 'ai');
    setButtonLoading(aiBtn, 'Analyzing…');

    let rateLimited = false;
    try {
        const result = await analyzeAI(text);
        if (result.success !== false) {
            updateAICard(cardId, result);
            updateBalanceFromResponse(result);
        } else {
            updateCardError(cardId, result.error || 'Analysis failed');
        }
    } catch (error) {
        updateCardError(cardId, error.message);
        if (error.status === 429) {
            rateLimited = true;
            showRateLimitCountdown(aiBtn, 'Detect AI', error.retryAfterSec);
        } else if (error.status === 403) {
            openCheckout();
        }
    } finally {
        isProcessing = false;
        if (!rateLimited) {
            resetButton(aiBtn, 'Detect AI', aiTextInput);
            aiTextInput.value = '';
            aiBtn.disabled = true;
        }
    }
}

// --- Media check handler ---
async function handleMediaCheck() {
    if (isProcessing) return;
    const url = mediaUrlInput.value.trim();
    if (!url) return;

    isProcessing = true;
    clearEmptyState();
    const cardId = createPendingCard(url, 'media');
    setButtonLoading(mediaBtn, 'Analyzing…');

    let rateLimited = false;
    try {
        const result = await analyzeMediaUrl(url);
        if (result.success !== false) {
            updateFactCheckCard(cardId, result.result || result);
            updateBalanceFromResponse(result);
        } else {
            updateCardError(cardId, result.error || 'Analysis failed');
        }
    } catch (error) {
        updateCardError(cardId, error.message);
        if (error.status === 429) {
            rateLimited = true;
            showRateLimitCountdown(mediaBtn, 'Analyze', error.retryAfterSec);
        } else if (error.status === 403) {
            openCheckout();
        }
    } finally {
        isProcessing = false;
        if (!rateLimited) {
            resetButton(mediaBtn, 'Analyze', mediaUrlInput);
            mediaUrlInput.value = '';
            mediaBtn.disabled = true;
        }
    }
}

// --- Card creation ---

/** Create a pending card. */
function createPendingCard(text, type = 'fact') {
    const id = `sp-card-${++cardCounter}`;
    const card = document.createElement('div');
    card.className = `sp-card pending ${type === 'ai' ? 'ai-card' : ''}`;
    card.id = id;

    const previewText = text.length > 120 ? text.substring(0, 120) + '…' : text;

    card.innerHTML = `
        <div class="sp-card-header">
            <span class="sp-verdict PENDING">
                <span class="sp-spinner"></span> ${type === 'ai' ? 'Analyzing…' : 'Checking…'}
            </span>
            <span class="sp-card-time">${new Date().toLocaleTimeString()}</span>
        </div>
        <p class="sp-claim-text">"${escapeHtml(previewText)}"</p>
        <div class="sp-card-details"></div>
    `;

    resultsContainer.insertBefore(card, resultsContainer.firstChild);
    return id;
}

/** Update a card with fact-check result. */
function updateFactCheckCard(cardId, result) {
    const card = document.getElementById(cardId);
    if (!card) return;

    card.classList.remove('pending');

    const verdictBadge = card.querySelector('.sp-verdict');
    const verdict = result.verdict || 'NOT_VERIFIED';
    verdictBadge.className = `sp-verdict ${verdict}`;
    verdictBadge.textContent = verdict.replace(/_/g, ' ');

    if (verdict === 'NOT_A_CLAIM') {
        card.style.cursor = 'default';
        return;
    }

    const details = card.querySelector('.sp-card-details');
    let html = '';

    if (result.explanation) {
        const safe = typeof DOMPurify !== 'undefined'
            ? DOMPurify.sanitize(result.explanation) : escapeHtml(result.explanation);
        html += `<p class="sp-explanation">${safe}</p>`;
    }
    if (result.fallacy) {
        html += `<span class="sp-fallacy">⚠️ ${escapeHtml(result.fallacy)}</span>`;
    }
    if (result.sources?.length > 0) {
        const valid = result.sources.filter(u => sanitizeUrl(u));
        if (valid.length > 0) {
            html += `<div class="sp-sources"><strong>Sources:</strong>${valid.map(u =>
                `<a href="${sanitizeUrl(u)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(u)}">${formatUrl(u)}</a>`
            ).join('')}</div>`;
        }
    }

    details.innerHTML = html;
    card.addEventListener('click', () => card.classList.toggle('expanded'));
}

/** Update a card with AI detection result. */
function updateAICard(cardId, result) {
    const card = document.getElementById(cardId);
    if (!card) return;

    card.classList.remove('pending');

    const probability = result.ai_probability ?? 50;
    const confidence = result.confidence || 'LOW';
    const colorClass = probability >= 70 ? 'sp-ai-high' : probability >= 40 ? 'sp-ai-med' : 'sp-ai-low';

    // Replace header
    const header = card.querySelector('.sp-card-header');
    header.innerHTML = `
        <span class="sp-ai-header">🤖 AI Detection <span class="beta-tag">β</span></span>
        <span class="sp-card-time">${new Date().toLocaleTimeString()}</span>
    `;

    // Build details
    const details = card.querySelector('.sp-card-details');
    const safe = typeof DOMPurify !== 'undefined'
        ? DOMPurify.sanitize(result.explanation || '') : escapeHtml(result.explanation || '');

    details.innerHTML = `
        <div class="sp-ai-prob ${colorClass}">
            <div class="sp-ai-prob-header">
                <span class="sp-ai-prob-value">${probability}%</span>
                <span class="sp-ai-prob-label">AI-Generated</span>
                <span class="sp-ai-confidence">${confidence}</span>
            </div>
            <div class="sp-ai-bar">
                <div class="sp-ai-fill" style="width: ${probability}%"></div>
            </div>
        </div>
        <p class="sp-explanation">${safe}</p>
        <em style="color: var(--color-text-muted); font-size: 0.7rem;">Beta feature — results are estimates only.</em>
    `;

    // Auto-expand AI cards
    card.classList.add('expanded');
    card.addEventListener('click', () => card.classList.toggle('expanded'));
}

/** Show an error on a card. */
function updateCardError(cardId, message) {
    const card = document.getElementById(cardId);
    if (!card) return;

    card.classList.remove('pending');
    const verdictBadge = card.querySelector('.sp-verdict');
    verdictBadge.className = 'sp-verdict FALSE';
    verdictBadge.textContent = 'ERROR';

    card.querySelector('.sp-card-details').innerHTML =
        `<p class="sp-explanation">${escapeHtml(message)}</p>`;
    card.classList.add('expanded');
}

// --- Helpers ---

function clearEmptyState() {
    const empty = resultsContainer.querySelector('.sp-empty-state');
    if (empty) empty.remove();
}

function setButtonLoading(btn, label) {
    btn.disabled = true;
    btn.innerHTML = `<span class="sp-spinner"></span> ${label}`;
}

function resetButton(btn, label, input) {
    btn.disabled = input ? input.value.trim().length === 0 : false;
    btn.textContent = label;
}

function updateBalanceFromResponse(result) {
    if (result.new_balance !== undefined) {
        currentBalance = result.new_balance;
        balanceEl.textContent = `${currentBalance} CP`;
        if (currentBalance <= 0) lowBalanceBar.classList.remove('hidden');
        // Invalidate cache so next loadUserInfo() fetches fresh data
        chrome.storage.local.remove(BALANCE_CACHE_KEY);
    }
}

function formatUrl(url) {
    try {
        const parsed = new URL(url);
        const domain = parsed.hostname.replace('www.', '');
        const path = parsed.pathname.substring(0, 15);
        return domain.substring(0, 25) + (path.length > 1 ? path + '…' : '');
    } catch {
        return url.substring(0, 30) + '…';
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Validate and sanitize a URL for safe use in href attributes.
 * Returns the properly-encoded href from the URL constructor,
 * or null if the URL is invalid or non-HTTP(S).
 * @param {*} url - Value to validate
 * @returns {string|null}
 */
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

/**
 * Disable a button and show a countdown until the rate-limit window expires.
 * @param {HTMLButtonElement} btn - Button to disable
 * @param {string} defaultLabel - Label to restore after countdown
 * @param {number} [seconds=60] - Seconds to count down (capped at 120)
 */
function showRateLimitCountdown(btn, defaultLabel, seconds = 60) {
    let remaining = Math.min(seconds, 120);
    btn.disabled = true;
    const tick = () => {
        if (remaining <= 0) {
            btn.textContent = defaultLabel;
            btn.disabled = false;
            return;
        }
        btn.textContent = `Wait ${remaining}s`;
        remaining--;
        setTimeout(tick, 1000);
    };
    tick();
}

/**
 * Open Stripe checkout for the default CP package (battery_medium).
 * Calls the backend to create a session, then opens the Stripe URL
 * in a new browser tab. Shows the low-balance bar on failure.
 */
async function openCheckout() {
    try {
        const result = await createCheckout();
        if (result.url) {
            chrome.tabs.create({ url: result.url });
        }
    } catch (error) {
        // If checkout fails (e.g. Stripe not configured in TEST_MODE),
        // fall back to opening the web app.
        console.error('[Authenix] Checkout failed:', error.message);
        chrome.tabs.create({ url: 'https://authenix.ai' });
    }
}
