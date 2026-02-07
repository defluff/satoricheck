/**
 * API Client Module
 * Centralized API communication with error handling
 */

const API_BASE = '/api';

class APIClient {
    async request(endpoint, options = {}, retries = 5, delay = 1500) {
        try {
            const response = await fetch(`${API_BASE}${endpoint}`, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers
                },
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

    // Auth endpoints
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

    // Token endpoints
    async getBalance() {
        return this.request('/tokens/balance');
    }

    async getTransactionHistory() {
        return this.request('/tokens/history');
    }

    // Billing endpoints
    async createCheckoutSession(packageType) {
        return this.request('/billing/create-checkout', {
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

    // Live Pro endpoints
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

    // Fact-check endpoints
    async analyzeText(text, context = null, smartAgent = false) {
        const payload = { text };

        // Add optional context for improved accuracy
        if (context) {
            payload.context = context;
        }

        // Add smart agent flag if enabled
        if (smartAgent) {
            payload.smart_agent = true;
        }

        return this.request('/factcheck/analyze', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
    }

    async analyzeBatch(claims, context = null) {
        // Progressive micro-batches: smaller chunks = faster user feedback
        // 3 claims per batch balances speed vs API efficiency
        const CHUNK_SIZE = 3;
        const allResults = [];
        let newBalance = null;

        // Process in chunks - user sees results progressively
        for (let i = 0; i < claims.length; i += CHUNK_SIZE) {
            const chunk = claims.slice(i, i + CHUNK_SIZE);
            const payload = { claims: chunk };
            if (context) payload.context = context;

            try {
                // Send chunk request
                const response = await this.request('/factcheck/analyze-batch', {
                    method: 'POST',
                    body: JSON.stringify(payload)
                });

                if (response.success && response.results) {
                    allResults.push(...response.results);

                    // Update balance from latest successful response
                    if (response.new_balance !== undefined) {
                        newBalance = response.new_balance;
                    }
                }
            } catch (error) {
                console.error(`Batch chunk ${i} failed:`, error);

                // If a chunk fails, we still want to return results for others if possible
                // Fill failed chunk with error placeholders
                for (let j = 0; j < chunk.length; j++) {
                    allResults.push({
                        verdict: 'ERROR',
                        explanation: `Batch processing failed for this item: ${error.message}`,
                        sources: [],
                        is_claim: true
                    });
                }

                // If it's a 403 (insufficient funds) or 401, we should probably stop
                if (error.status === 403 || error.status === 401) {
                    throw error;
                }
            }
        }

        return {
            success: true,
            results: allResults,
            new_balance: newBalance
        };
    }

    async identifyClaims(text) {
        return this.request('/factcheck/identify-claims', {
            method: 'POST',
            body: JSON.stringify({ text })
        });
    }

    async analyzeAI(text) {
        return this.request('/factcheck/analyze-ai', {
            method: 'POST',
            body: JSON.stringify({ text })
        });
    }

    async getFactCheckHistory(limit = 50) {
        return this.request(`/factcheck/history?limit=${limit}`);
    }

    // Export endpoint
    async exportFactChecks(format = 'csv') {
        // This returns a file, so handle differently
        const response = await fetch(`${API_BASE}/export/factchecks?format=${format}`, {
            credentials: 'include'
        });

        if (!response.ok) {
            throw new Error('Export failed');
        }

        const blob = await response.blob();
        return blob;
    }

    // Feature voting (user research)
    async recordFeatureVote(featureName) {
        return this.request('/feedback/feature-vote', {
            method: 'POST',
            body: JSON.stringify({ feature: featureName })
        });
    }
}

export default new APIClient();
