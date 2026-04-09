/**
 * Fetch with exponential backoff for transient server errors.
 *
 * Retries on HTTP 500, 502, 503, 504 and network errors.
 * Does NOT retry on client errors (4xx) or timeouts (AbortError).
 *
 * @module retry
 */

/** HTTP status codes that indicate transient server issues. */
const RETRYABLE_STATUSES = new Set([500, 502, 503, 504]);

/**
 * Execute a fetch factory function with retry logic.
 *
 * The factory is called on each attempt so that callers can create
 * fresh AbortControllers per attempt.
 *
 * @param {() => Promise<Response>} fetchFn - Factory that returns a fetch promise
 * @param {Object} [options]
 * @param {number} [options.maxRetries=2] - Max retry attempts (total = maxRetries + 1)
 * @param {number} [options.baseDelayMs=1000] - Base delay before first retry
 * @returns {Promise<Response>} The final Response (may still be non-ok after exhaustion)
 * @throws {Error} Network/abort errors after all retries are exhausted
 */
export async function fetchWithRetry(fetchFn, { maxRetries = 2, baseDelayMs = 1000 } = {}) {
    let lastError;

    for (let attempt = 0; attempt <= maxRetries; attempt++) {
        try {
            const response = await fetchFn();

            // Return immediately for non-retryable statuses or final attempt
            if (!RETRYABLE_STATUSES.has(response.status) || attempt >= maxRetries) {
                return response;
            }

            // Server error — wait and retry
            const delay = baseDelayMs * Math.pow(2, attempt);
            await new Promise(resolve => setTimeout(resolve, delay));
        } catch (error) {
            lastError = error;

            // AbortError = timeout — don't retry, let caller handle
            if (error.name === 'AbortError' || attempt >= maxRetries) {
                throw error;
            }

            // Network error — wait and retry
            const delay = baseDelayMs * Math.pow(2, attempt);
            await new Promise(resolve => setTimeout(resolve, delay));
        }
    }

    throw lastError || new Error('Request failed after retries');
}
