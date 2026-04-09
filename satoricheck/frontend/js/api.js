/**
 * API Client Module
 * Centralized API communication with error handling
 */

const API_BASE = '/api';

class APIClient {
    async request(endpoint, options = {}, retries = 5, delay = 1500) {
        try {
            const headers = { ...options.headers };

            // Only set Content-Type to JSON if not already set and not FormData
            if (!headers['Content-Type'] && !(options.body instanceof FormData)) {
                headers['Content-Type'] = 'application/json';
            }

            const response = await fetch(`${API_BASE}${endpoint}`, {
                ...options,
                headers,
                credentials: 'include' // Include cookies for session
            });

            // Parse JSON response if possible
            let data;
            const contentType = response.headers.get("content-type");
            if (contentType && contentType.indexOf("application/json") !== -1) {
                data = await response.json();
            } else {
                // Handle non-JSON response (e.g. blobs)
                if (response.ok) return response;
                throw new Error(response.statusText || 'Request failed');
            }

            if (!response.ok) {
                // Attach status code to error object for retry logic
                const error = new Error(data.error || 'Request failed');
                error.status = response.status;
                throw error;
            }

            return data;
        } catch (error) {
            console.error(`API Error (${endpoint}):`, error);

            // Retry on 503 (Service Unavailable) or 429 (Rate Limit)
            // Also retry if message explicitly says "temporarily unavailable" (legacy support)
            const isTransient = error.status === 503 ||
                error.status === 429 ||
                (error.message && error.message.includes('temporarily unavailable'));

            if (isTransient && retries > 0) {
                console.warn(`Retrying API call to ${endpoint} in ${delay / 1000} seconds... (Status: ${error.status})`);
                await new Promise(res => setTimeout(res, delay));
                // Recursively call request with decremented retries and increased delay (exponential backoff)
                return this.request(endpoint, options, retries - 1, delay * 2);
            }

            throw error;
        }
    }

    // --- AUTH ENDPOINTS ---

    async signup(email, password) {
        return this.request('/auth/signup', {
            method: 'POST',
            body: JSON.stringify({ email, password })
        });
    }

    async login(email, password) {
        return this.request('/auth/login', {
            method: 'POST',
            body: JSON.stringify({ email, password })
        });
    }

    async logout() {
        return this.request('/auth/logout', {
            method: 'POST'
        });
    }

    async getCurrentUser() {
        return this.request('/auth/me');
    }

    async changePassword(currentPassword, newPassword) {
        return this.request('/auth/change-password', {
            method: 'POST',
            body: JSON.stringify({
                current_password: currentPassword,
                new_password: newPassword
            })
        });
    }

    async deleteAccount() {
        return this.request('/auth/delete-account', {
            method: 'POST'
        });
    }

    async getExtensionToken() {
        return this.request('/auth/extension-token');
    }

    // --- TOKEN & BILLING ENDPOINTS ---

    async getBalance() {
        return this.request('/tokens/balance');
    }

    async getTransactionHistory() {
        return this.request('/tokens/history');
    }

    // Billing endpoints
    async createCheckoutSession(packageType) {
        return this.request('/billing/checkout', {
            method: 'POST',
            body: JSON.stringify({ package_type: packageType })
        });
    }

    async createPortalSession() {
        return this.request('/billing/portal', {
            method: 'POST'
        });
    }

    async getPackages() {
        return this.request('/billing/packages');
    }

    // --- LIVE PRO ENDPOINTS ---

    async getLiveProConfig() {
        return this.request('/live-pro/config');
    }

    async startLiveProSession(language = 'en', deviceId = null) {
        return this.request('/live-pro/start', {
            method: 'POST',
            body: JSON.stringify({ language, device_id: deviceId })
        });
    }

    async liveProHeartbeat(sessionId) {
        return this.request('/live-pro/heartbeat', {
            method: 'POST',
            body: JSON.stringify({ session_id: sessionId })
        });
    }

    async endLiveProSession(sessionId) {
        return this.request('/live-pro/end', {
            method: 'POST',
            body: JSON.stringify({ session_id: sessionId })
        });
    }

    // --- FACT-CHECK ENDPOINTS ---

    async analyzeText(text, context = null) {
        const payload = { text };
        if (context) payload.context = context;

        return this.request('/factcheck/analyze', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    }

    async analyzeBatch(claims, context = null, cache_name = null) {
        // Micro-batching handled here for standard fact-check mode
        const CHUNK_SIZE = 3;
        const allResults = [];
        let newBalance = null;

        for (let i = 0; i < claims.length; i += CHUNK_SIZE) {
            const chunk = claims.slice(i, i + CHUNK_SIZE);
            const payload = { claims: chunk };
            if (context) payload.context = context;
            if (cache_name) payload.cache_name = cache_name;

            try {
                const response = await this.request('/factcheck/analyze-batch', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });

                if (response.success && response.results) {
                    allResults.push(...response.results);
                    if (response.new_balance !== undefined) newBalance = response.new_balance;
                }
            } catch (error) {
                console.error(`Batch chunk ${i} failed:`, error);
                chunk.forEach(() => {
                    allResults.push({
                        verdict: 'ERROR',
                        explanation: `Batch processing failed: ${error.message}`,
                        sources: [],
                        is_claim: true
                    });
                });
                if (error.status === 403 || error.status === 401) throw error;
            }
        }

        return { success: true, results: allResults, new_balance: newBalance };
    }

    async identifyClaims(text) {
        return this.request('/factcheck/identify-claims', {
            method: 'POST',
            body: JSON.stringify({ text })
        });
    }

    async getFactCheckHistory(limit = 50) {
        return this.request(`/factcheck/history?limit=${limit}`);
    }

    // --- AI DETECTION ENDPOINTS ---

    async analyzeAI(text) {
        return this.request('/factcheck/analyze-ai', {
            method: 'POST',
            body: JSON.stringify({ text })
        });
    }

    // --- MEDIA AUTHENTICITY ENDPOINTS ---

    async analyzeMedia(file) {
        const formData = new FormData();
        formData.append('file', file);

        return this.request('/media/analyze-upload', {
            method: 'POST',
            body: formData
        });
    }

    async analyzeMediaUrl(url) {
        return this.request('/media/analyze-url', {
            method: 'POST',
            body: JSON.stringify({ url })
        });
    }

    // --- PITCHDECK ENDPOINTS ---

    async analyzePitchDeck(pdfData, signal) {
        // pdfData is base64
        return this.request('/pitchdeck/analyze', {
            method: 'POST',
            body: JSON.stringify({ pdf_data: pdfData }),
            signal: signal
        });
    }

    async verifyMarketClaims(payload) {
        return this.request('/pitchdeck/verify-market', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    }

    // --- UTILITY ENDPOINTS ---

    async exportFactChecks(format = 'csv') {
        const response = await fetch(`${API_BASE}/export/factchecks?format=${format}`, {
            credentials: 'include'
        });

        if (!response.ok) throw new Error('Export failed');
        return await response.blob();
    }

}

export default new APIClient();
