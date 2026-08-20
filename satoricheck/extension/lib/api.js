/**
 * API client for the Authenix extension.
 *
 * Reads the Bearer token from encrypted storage or auto-syncs from
 * active browser session cookies (Google OAuth).
 *
 * @module api
 */

import { getToken, saveToken, saveUserEmail, clearAuth } from './storage.js';
import { fetchWithRetry } from './retry.js';

/** Candidate backend origins (production, Cloud Run). */
export const CANDIDATE_ORIGINS = [
    'https://satoricheck-829698588154.europe-west6.run.app',
    'https://authenix.ai',
];

/** Active default API base. */
export const API_BASE = 'https://satoricheck-829698588154.europe-west6.run.app/api';

/** Default timeout per request attempt (ms). Used for fast endpoints (auth, balance). */
const DEFAULT_TIMEOUT_MS = 20000;
/** Extended timeout for AI analysis endpoints — covers model cold-starts. */
const ANALYSIS_TIMEOUT_MS = 45000;

/** Minimum word count required by the AI detection endpoint. */
export const MIN_AI_WORDS = 20;

/**
 * Make an authenticated API request with timeout + retry.
 *
 * @param {string} endpoint - Path relative to API_BASE (e.g. '/auth/me')
 * @param {RequestInit & { timeoutMs?: number, customToken?: string }} [options={}] - Fetch options
 * @returns {Promise<Object>} Parsed JSON response
 * @throws {Error} On network/auth/server/rate-limit errors
 */
export async function request(endpoint, options = {}) {
    const token = options.customToken || (await getToken());
    if (!token) {
        throw new Error('Not authenticated — please connect your account.');
    }

    const headers = {
        'Authorization': `Bearer ${token}`,
        ...options.headers,
    };

    // Default to JSON content type unless FormData
    if (!(options.body instanceof FormData) && !headers['Content-Type']) {
        headers['Content-Type'] = 'application/json';
    }

    const timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT_MS;

    let response;
    try {
        response = await fetchWithRetry(() => {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
            return fetch(`${API_BASE}${endpoint}`, {
                ...options,
                headers,
                signal: controller.signal,
            }).finally(() => clearTimeout(timeoutId));
        });
    } catch (error) {
        if (error.name === 'AbortError') {
            throw new Error('Request timed out. Please try again.');
        }
        throw new Error('Network error. Check your connection.');
    }

    // --- Rate limit: surface immediately with Retry-After ---
    if (response.status === 429) {
        const retryAfter = response.headers.get('Retry-After') || '60';
        const seconds = parseInt(retryAfter, 10) || 60;
        const error = new Error(`Rate limited. Try again in ${seconds}s.`);
        error.status = 429;
        error.retryAfterSec = seconds;
        throw error;
    }

    // --- Auth and other errors: parse body safely ---
    if (!response.ok) {
        let message = `Request failed (${response.status})`;
        const contentType = response.headers.get('content-type') || '';
        if (contentType.includes('application/json')) {
            try {
                const data = await response.json();
                message = data.error || data.message || message;
            } catch { /* use default message */ }
        }

        if (response.status === 401) {
            message = 'Session expired. Please sign in again.';
        } else if (response.status === 402 || response.status === 403) {
            message = 'Insufficient Check Points. Please top up.';
        }

        const error = new Error(message);
        error.status = response.status;
        throw error;
    }

    return response.json();
}

/**
 * Validate the stored token and get user info.
 * @param {string} [customToken]
 * @returns {Promise<Object>} { success, user: { email, balance, streak } }
 */
export async function getCurrentUser(customToken) {
    return request('/auth/me', { customToken });
}

/**
 * Get token balance and streak.
 * @returns {Promise<Object>} { balance, streak }
 */
export async function getBalance() {
    return request('/tokens/balance');
}

/**
 * Attempt to auto-sync authentication from browser session cookies (Google OAuth).
 * Checks authenix_token cookie on candidate origins without user needing to copy an API key.
 *
 * @returns {Promise<Object|null>} User object if authenticated, or null
 */
export async function syncWebAuthSession() {
    if (!chrome.cookies || !chrome.cookies.get) {
        return null;
    }

    for (const origin of CANDIDATE_ORIGINS) {
        try {
            const cookie = await chrome.cookies.get({ url: origin, name: 'authenix_token' });
            if (cookie && cookie.value) {
                // Test token against /auth/me
                const res = await getCurrentUser(cookie.value);
                if (res?.success && res.user) {
                    await saveToken(cookie.value);
                    await saveUserEmail(res.user.email);
                    return res.user;
                }
            }
        } catch {
            // Ignore origin error, check next candidate
        }
    }

    return null;
}

/**
 * Open the Google OAuth login window in a tab, and auto-detect when login completes.
 * @returns {Promise<void>}
 */
export async function initiateGoogleLogin() {
    const authUrl = `${CANDIDATE_ORIGINS[0]}/api/auth/google?ext=1`;
    const tab = await chrome.tabs.create({ url: authUrl });

    // Poll briefly for the cookie to be set once the user finishes OAuth
    return new Promise((resolve) => {
        let attempts = 0;
        const maxAttempts = 60; // 60 * 1500ms = 90s
        const intervalId = setInterval(async () => {
            attempts++;
            const user = await syncWebAuthSession();
            if (user) {
                clearInterval(intervalId);
                // Close the login tab if it's still open on the callback page
                try {
                    const tabInfo = await chrome.tabs.get(tab.id);
                    if (tabInfo && tabInfo.url && tabInfo.url.includes('ext=1')) {
                        await chrome.tabs.remove(tab.id);
                    }
                } catch { /* Tab may already be closed by user */ }
                resolve(user);
            } else if (attempts >= maxAttempts) {
                clearInterval(intervalId);
                resolve(null);
            }
        }, 1500);
    });
}

/**
 * Submit a claim for factual analysis.
 * @param {string} text - The claim text
 * @returns {Promise<Object>} Fact-check result
 */
export async function analyzeText(text) {
    return request('/factcheck/analyze', {
        method: 'POST',
        body: JSON.stringify({ text }),
        timeoutMs: ANALYSIS_TIMEOUT_MS,
    });
}

/**
 * Submit text for AI-generation detection.
 * @param {string} text - Text to analyze (min 20 words)
 * @returns {Promise<Object>} { ai_probability, confidence, ai_indicators, human_indicators, explanation }
 */
export async function analyzeAI(text) {
    return request('/factcheck/analyze-ai', {
        method: 'POST',
        body: JSON.stringify({ text }),
        timeoutMs: ANALYSIS_TIMEOUT_MS,
    });
}

/**
 * Submit a media URL for authenticity analysis.
 * @param {string} url - Public media URL
 * @returns {Promise<Object>} Media analysis result
 */
export async function analyzeMediaUrl(url) {
    return request('/media/analyze-url', {
        method: 'POST',
        body: JSON.stringify({ url }),
        timeoutMs: ANALYSIS_TIMEOUT_MS,
    });
}

/**
 * Unified Modular Analyzer.
 * Executes analysis according to mode:
 *   - 'both': Runs Claims Verification & AI Text Scanner in parallel (Default)
 *   - 'claims': Runs Claims Verification only
 *   - 'ai': Runs AI Text Scanner only
 *
 * @param {string} text - Text content to verify
 * @param {'both'|'claims'|'ai'} [mode='both'] - Verification mode
 * @returns {Promise<Object>} Modular result payload
 */
export async function analyzeModular(text, mode = 'both') {
    const cleanText = (text || '').trim();
    if (!cleanText) {
        throw new Error('Please provide text to verify.');
    }

    const words = cleanText.split(/\s+/).filter(Boolean);
    const wordCount = words.length;

    if (mode === 'claims') {
        const result = await analyzeText(cleanText);
        return {
            mode: 'claims',
            text: cleanText,
            wordCount,
            claims: result.result || result,
            new_balance: result.new_balance,
        };
    }

    if (mode === 'ai') {
        if (wordCount < MIN_AI_WORDS) {
            throw new Error(`AI Spotter requires at least ${MIN_AI_WORDS} words (provided ${wordCount}).`);
        }
        const result = await analyzeAI(cleanText);
        return {
            mode: 'ai',
            text: cleanText,
            wordCount,
            ai: result,
            new_balance: result.new_balance,
        };
    }

    // Default: 'both' mode (parallel execution with graceful AI fallback for short snippets)
    const canRunAI = wordCount >= MIN_AI_WORDS;

    const claimsPromise = analyzeText(cleanText);
    const aiPromise = canRunAI
        ? analyzeAI(cleanText)
        : Promise.resolve({
            skipped: true,
            reason: `Snippet too short for forensic AI analysis (${wordCount}/${MIN_AI_WORDS} words).`,
            ai_probability: null,
            confidence: 'N/A',
        });

    const [claimsOutcome, aiOutcome] = await Promise.allSettled([claimsPromise, aiPromise]);

    if (claimsOutcome.status === 'rejected') {
        throw claimsOutcome.reason;
    }

    const claimData = claimsOutcome.value;
    const aiData = aiOutcome.status === 'fulfilled' ? aiOutcome.value : null;

    return {
        mode: 'both',
        text: cleanText,
        wordCount,
        claims: claimData.result || claimData,
        ai: aiData,
        new_balance: claimData.new_balance ?? (aiData?.new_balance),
    };
}

/**
 * Create a Stripe checkout session for CP purchase.
 * @param {string} [packageType='battery_medium'] - Package key from TOKEN_PACKAGES
 * @returns {Promise<Object>} { success, session_id, url }
 */
export async function createCheckout(packageType = 'battery_medium') {
    return request('/billing/checkout', {
        method: 'POST',
        body: JSON.stringify({ package_type: packageType }),
    });
}
