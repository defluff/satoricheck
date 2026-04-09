/**
 * API client for the SatoriCheck extension.
 *
 * Reads the Bearer token from storage and attaches it to every
 * request. Configurable base URL to switch between localhost
 * and production.
 *
 * @module api
 */

import { getToken } from './storage.js';
import { fetchWithRetry } from './retry.js';

// Toggle for development — change to localhost when testing locally
const API_BASE = 'https://satoricheck.com/api';
// const API_BASE = 'http://localhost:8000/api';

/** Default timeout per request attempt (ms). Used for fast endpoints (auth, balance). */
const DEFAULT_TIMEOUT_MS = 20000;
/** Extended timeout for AI analysis endpoints — covers Gemini Pro model cold-starts. */
const ANALYSIS_TIMEOUT_MS = 45000;

/**
 * Make an authenticated API request with timeout + retry.
 *
 * Each attempt gets a fresh AbortController with DEFAULT_TIMEOUT_MS.
 * Transient server errors (500-504) are retried up to 2× with exponential
 * backoff. Rate-limit (429) and auth errors (401/403) surface immediately.
 *
 * @param {string} endpoint - Path relative to API_BASE (e.g. '/auth/me')
 * @param {RequestInit & { timeoutMs?: number }} [options={}] - Fetch options
 * @returns {Promise<Object>} Parsed JSON response
 * @throws {Error} On network/auth/server/rate-limit errors
 */
export async function request(endpoint, options = {}) {
    const token = await getToken();
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

    // Each retry creates a fresh AbortController so the timeout resets per attempt
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

    // --- Auth and other errors: parse body safely (B5 — no crash on HTML) ---
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
            message = 'Session expired. Please reconnect your account.';
        } else if (response.status === 403) {
            message = 'Insufficient Check Points. Please top up.';
        }

        const error = new Error(message);
        error.status = response.status;
        throw error;
    }

    return response.json();
}

// --- Convenience methods ---

/**
 * Validate the stored token and get user info.
 * @returns {Promise<Object>} { success, user: { email, balance, streak } }
 */
export async function getCurrentUser() {
    return request('/auth/me');
}

/**
 * Get token balance and streak.
 * @returns {Promise<Object>} { balance, streak }
 */
export async function getBalance() {
    return request('/tokens/balance');
}

/**
 * Submit a claim for fact-checking.
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
 * Create a Stripe checkout session for CP purchase.
 * Returns a URL to redirect the user to Stripe.
 * @param {string} [packageType='battery_medium'] - Package key from TOKEN_PACKAGES
 * @returns {Promise<Object>} { success, session_id, url }
 */
export async function createCheckout(packageType = 'battery_medium') {
    return request('/billing/checkout', {
        method: 'POST',
        body: JSON.stringify({ package_type: packageType }),
    });
}
