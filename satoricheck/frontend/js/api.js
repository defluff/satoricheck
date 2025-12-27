/**
 * API Client Module
 * Centralized API communication with error handling
 */

const API_BASE = '/api';

class APIClient {
    async request(endpoint, options = {}) {
        try {
            const response = await fetch(`${API_BASE}${endpoint}`, {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    ...options.headers
                },
                credentials: 'include' // Include cookies for session
            });

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.error || 'Request failed');
            }

            return data;
        } catch (error) {
            console.error(`API Error (${endpoint}):`, error);
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

    async deductLiveProTime(seconds) {
        return this.request('/live-pro/deduct', {
            method: 'POST',
            body: JSON.stringify({ seconds })
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
}

export default new APIClient();
