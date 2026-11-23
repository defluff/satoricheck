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
const topupModalOverlay = document.getElementById('sp-topup-modal-overlay');
const topupModalClose = document.getElementById('sp-modal-close');

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

    // Auto-open top-up modal if requested (e.g. from popup click)
    try {
        chrome.storage.local.get('authenix_open_topup', (res) => {
            if (res?.authenix_open_topup) {
                chrome.storage.local.remove('authenix_open_topup');
                openTopupModal();
            }
        });
    } catch {
        // Ignore storage errors
    }

    // 1. Reactive storage listener for instant push when side panel is open
    try {
        chrome.storage.session.onChanged.addListener((changes) => {
            if (changes.activeSelection?.newValue?.text) {
                const text = changes.activeSelection.newValue.text;
                if (text && text !== inputText.value) {
                    inputText.value = text;
                    updateInputState();
                    handleVerify();
                }
            }
        });

        // 2. Check for recent active selection on open
        const sessionData = await chrome.storage.session.get(['activeSelection', 'pendingSelection']);
        const initialText = sessionData.activeSelection?.text || sessionData.pendingSelection;
        if (initialText && (!sessionData.activeSelection?.timestamp || (Date.now() - sessionData.activeSelection.timestamp < 15000))) {
            inputText.value = initialText;
            updateInputState();
            handleVerify();
            chrome.storage.session.remove(['activeSelection', 'pendingSelection']);
        }
    } catch (err) {
        console.warn('[Authenix] Session storage access warning:', err);
    }

    // 3. Fallback Handshake message listener
    chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
        if (message.type === 'FACTCHECK_SELECTION' && message.text) {
            inputText.value = message.text;
            updateInputState();
            handleVerify();
            if (typeof sendResponse === 'function') {
                sendResponse({ received: true });
            }
        }
        return false;
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
    if (authStatus) authStatus.textContent = 'Checking session…';
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
    if (authStatus) authStatus.textContent = 'Sign in with Google to use Authenix';
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

    // Stripe checkout triggers (opens modal)
    balanceEl?.addEventListener('click', () => openTopupModal());
    buyLink?.addEventListener('click', () => openTopupModal());

    // Modal close & click outside handlers
    topupModalClose?.addEventListener('click', () => closeTopupModal());
    topupModalOverlay?.addEventListener('click', (e) => {
        if (e.target === topupModalOverlay) closeTopupModal();
    });

    // Modal package card purchase handlers
    topupModalOverlay?.querySelectorAll('.sp-pkg-btn').forEach((btn) => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const pkg = btn.dataset.package;
            handlePackageCheckout(pkg, btn);
        });
    });

    topupModalOverlay?.querySelectorAll('.sp-pkg-card').forEach((card) => {
        card.addEventListener('click', (e) => {
            const pkg = card.dataset.package;
            const btn = card.querySelector('.sp-pkg-btn');
            handlePackageCheckout(pkg, btn);
        });
    });
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
            openTopupModal();
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
            <div class="sp-badges-left">
                <span class="sp-verdict PENDING">
                    <span class="sp-spinner"></span> ${modeBadgeLabel}…
                </span>
            </div>
            <div class="sp-badges-right">
                <span class="sp-card-time">${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
            </div>
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
    const headerLeft = card.querySelector('.sp-badges-left');
    const headerRight = card.querySelector('.sp-badges-right');
    const detailsContainer = card.querySelector('.sp-card-details');

    const safeClassToken = (val, fallback) => {
        const s = String(val || '').trim();
        return /^[A-Za-z0-9_-]+$/.test(s) ? s : fallback;
    };

    const leftBadgesNodes = [];
    const rightBadgesNodes = [];
    const detailsNodes = [];

    // 1. Claims Verdict Badge & Analysis Block (for 'both' or 'claims' mode)
    if (data.claims) {
        const rawVerdict = String(data.claims.verdict || 'NOT_VERIFIED');
        const verdictClass = safeClassToken(rawVerdict, 'NOT_VERIFIED');
        const verdictLabel = rawVerdict.replace(/_/g, ' ');

        const topBadge = document.createElement('span');
        topBadge.className = `sp-verdict ${verdictClass}`;
        topBadge.textContent = verdictLabel;
        leftBadgesNodes.push(topBadge);

        const claimBlock = document.createElement('div');
        claimBlock.className = 'sp-section-block sp-claim-block';

        const sectionHeader = document.createElement('div');
        sectionHeader.className = 'sp-section-header';
        const sectionTitle = document.createElement('span');
        sectionTitle.className = 'sp-section-title';
        sectionTitle.textContent = '🔍 Claim Check';
        const headerVerdict = document.createElement('span');
        headerVerdict.className = `sp-verdict ${verdictClass}`;
        headerVerdict.textContent = verdictLabel;
        sectionHeader.append(sectionTitle, headerVerdict);
        claimBlock.appendChild(sectionHeader);

        if (data.claims.explanation) {
            const expP = document.createElement('p');
            expP.className = 'sp-explanation';
            if (typeof DOMPurify !== 'undefined') {
                expP.appendChild(DOMPurify.sanitize(String(data.claims.explanation), { RETURN_DOM_FRAGMENT: true }));
            } else {
                expP.textContent = String(data.claims.explanation);
            }
            claimBlock.appendChild(expP);
        }

        if (data.claims.fallacy) {
            const fallacySpan = document.createElement('span');
            fallacySpan.className = 'sp-fallacy';
            fallacySpan.textContent = `⚠️ ${data.claims.fallacy}`;
            claimBlock.appendChild(fallacySpan);
        }

        if (data.claims.sources && Array.isArray(data.claims.sources) && data.claims.sources.length > 0) {
            const validSources = data.claims.sources.map(u => ({ raw: u, safe: sanitizeUrl(u) })).filter(s => s.safe);
            if (validSources.length > 0) {
                const sourcesDiv = document.createElement('div');
                sourcesDiv.className = 'sp-sources';
                const strong = document.createElement('strong');
                strong.textContent = 'Verified Sources:';
                sourcesDiv.appendChild(strong);
                for (const { raw, safe } of validSources) {
                    const a = document.createElement('a');
                    a.href = safe;
                    a.target = '_blank';
                    a.rel = 'noopener noreferrer';
                    a.title = String(raw);
                    a.textContent = `🔗 ${formatUrl(raw)}`;
                    sourcesDiv.appendChild(a);
                }
                claimBlock.appendChild(sourcesDiv);
            }
        }

        detailsNodes.push(claimBlock);
    }

    // 2. AI Probability Pill & Forensics Block (for 'both' or 'ai' mode)
    if (data.ai) {
        if (data.ai.skipped) {
            const shortPill = document.createElement('span');
            shortPill.className = 'sp-ai-pill ai-short';
            shortPill.title = String(data.ai.reason || '');
            shortPill.textContent = `🤖 Short (<${MIN_AI_WORDS}w)`;
            rightBadgesNodes.push(shortPill);
        } else {
            const rawProb = Number(data.ai.ai_probability ?? 50);
            const prob = Number.isFinite(rawProb) ? Math.max(0, Math.min(100, rawProb)) : 50;
            const conf = String(data.ai.confidence || 'MED');
            const pillClass = prob >= 70 ? 'ai-high' : prob >= 40 ? 'ai-med' : 'ai-low';
            const probLabel = prob >= 70 ? `${prob}% AI Likely` : prob <= 30 ? `${100 - prob}% Human` : `${prob}% AI Prob`;

            const aiPill = document.createElement('span');
            aiPill.className = `sp-ai-pill ${pillClass}`;
            aiPill.textContent = `🤖 ${probLabel}`;
            rightBadgesNodes.push(aiPill);

            // Forensics breakdown box
            const aiIndicators = (data.ai.ai_indicators || []).slice(0, 3);
            const humanIndicators = (data.ai.human_indicators || []).slice(0, 3);

            const aiBlock = document.createElement('div');
            aiBlock.className = 'sp-section-block sp-ai-block';

            const aiHeader = document.createElement('div');
            aiHeader.className = 'sp-section-header';
            const aiTitle = document.createElement('span');
            aiTitle.className = 'sp-section-title';
            aiTitle.textContent = `🤖 AI Forensics (${prob}%)`;
            const confTag = document.createElement('span');
            confTag.className = 'sp-ai-confidence-tag';
            confTag.textContent = `Confidence: ${conf}`;
            aiHeader.append(aiTitle, confTag);
            aiBlock.appendChild(aiHeader);

            const barTrack = document.createElement('div');
            barTrack.className = 'sp-ai-bar-track';
            const barFill = document.createElement('div');
            barFill.className = `sp-ai-bar-fill ${pillClass}`;
            barFill.style.width = `${prob}%`;
            barTrack.appendChild(barFill);
            aiBlock.appendChild(barTrack);

            if (data.ai.explanation) {
                const aiExp = document.createElement('p');
                aiExp.className = 'sp-explanation';
                aiExp.style.marginTop = '4px';
                aiExp.textContent = String(data.ai.explanation);
                aiBlock.appendChild(aiExp);
            }

            const indicatorsDiv = document.createElement('div');
            indicatorsDiv.className = 'sp-ai-indicators';
            for (const ind of aiIndicators) {
                const tag = document.createElement('span');
                tag.className = 'sp-indicator-tag';
                tag.textContent = `Marker: ${ind}`;
                indicatorsDiv.appendChild(tag);
            }
            for (const ind of humanIndicators) {
                const tag = document.createElement('span');
                tag.className = 'sp-indicator-tag sp-indicator-human';
                tag.textContent = `Human trait: ${ind}`;
                indicatorsDiv.appendChild(tag);
            }
            aiBlock.appendChild(indicatorsDiv);

            detailsNodes.push(aiBlock);
        }
    }

    const timeBadge = document.createElement('span');
    timeBadge.className = 'sp-card-time';
    timeBadge.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    rightBadgesNodes.push(timeBadge);

    if (headerLeft) headerLeft.replaceChildren(...leftBadgesNodes);
    if (headerRight) headerRight.replaceChildren(...rightBadgesNodes);
    detailsContainer.replaceChildren(...detailsNodes);

    // Card expand toggle
    card.classList.add('expanded'); // Auto-expand upon arrival
    card.addEventListener('click', () => card.classList.toggle('expanded'));
}

/** Show error state on card */
function updateCardError(cardId, message) {
    const card = document.getElementById(cardId);
    if (!card) return;

    card.classList.remove('pending');
    const headerLeft = card.querySelector('.sp-badges-left');
    const headerRight = card.querySelector('.sp-badges-right');
    const details = card.querySelector('.sp-card-details');

    if (headerLeft) {
        const errorBadge = document.createElement('span');
        errorBadge.className = 'sp-verdict FALSE error';
        errorBadge.textContent = 'ERROR';
        headerLeft.replaceChildren(errorBadge);
    }

    if (headerRight) {
        const timeBadge = document.createElement('span');
        timeBadge.className = 'sp-card-time';
        timeBadge.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        headerRight.replaceChildren(timeBadge);
    }

    if (details) {
        const errP = document.createElement('p');
        errP.className = 'sp-explanation sp-error-msg';
        errP.style.color = 'var(--color-error)';
        errP.textContent = String(message || 'Verification failed');
        details.replaceChildren(errP);
    }

    card.classList.add('expanded');
}

// --- Helpers ---

function clearEmptyState() {
    const empty = resultsFeed.querySelector('.sp-empty-state');
    if (empty) empty.remove();
}

function setButtonLoading(btn, label) {
    btn.disabled = true;
    const spinner = document.createElement('span');
    spinner.className = 'sp-spinner';
    const textSpan = document.createElement('span');
    textSpan.className = 'btn-text';
    textSpan.textContent = label;
    btn.replaceChildren(spinner, document.createTextNode(' '), textSpan);
}

function resetButton(btn, label) {
    const textSpan = document.createElement('span');
    textSpan.className = 'btn-text';
    textSpan.textContent = label;
    btn.replaceChildren(textSpan);
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
        const textSpan = document.createElement('span');
        textSpan.className = 'btn-text';
        textSpan.textContent = `Wait ${remaining}s`;
        btn.replaceChildren(textSpan);
        remaining--;
        setTimeout(tick, 1000);
    };
    tick();
}

function openTopupModal() {
    if (topupModalOverlay) {
        topupModalOverlay.classList.remove('hidden');
    }
}

function closeTopupModal() {
    if (topupModalOverlay) {
        topupModalOverlay.classList.add('hidden');
    }
}

async function handlePackageCheckout(packageType = 'battery_medium', btnEl = null) {
    const originalText = btnEl ? btnEl.textContent : '';
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '...';
    }

    try {
        const result = await createCheckout(packageType);
        if (result?.url) {
            chrome.tabs.create({ url: result.url });
            closeTopupModal();
        } else {
            chrome.tabs.create({ url: 'https://satoricheck-829698588154.europe-west6.run.app' });
        }
    } catch (err) {
        console.error('Checkout failed:', err);
        chrome.tabs.create({ url: 'https://satoricheck-829698588154.europe-west6.run.app' });
    } finally {
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = originalText;
        }
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
