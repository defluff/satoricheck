/**
 * Authentication Manager Module
 * Handles user authentication and session management
 */

import api from './api.js';
import ui from './ui.js';

class AuthManager {
    constructor() {
        this.currentUser = null;
        this.isLogin = true;
    }

    async init() {
        // Check if user is already logged in
        try {
            const response = await api.getCurrentUser();
            this.currentUser = response.user;

            // Show test mode banner if applicable
            if (response.user.is_test_mode) {
                ui.showTestModeBanner();
            }

            this.onAuthSuccess();
        } catch (error) {
            // Not logged in, show auth modal
            ui.showModal('auth-modal');
        }
    }

    setupEventListeners() {
        // Google Sign In (Returning User)
        const googleLoginBtn = document.getElementById('google-signin-btn');
        if (googleLoginBtn) {
            googleLoginBtn.addEventListener('click', () => {
                window.location.href = '/api/auth/google?action=login';
            });
        }

        // Google Register (New User - will show intro popup after)
        const googleRegisterBtn = document.getElementById('google-register-btn');
        if (googleRegisterBtn) {
            googleRegisterBtn.addEventListener('click', () => {
                // Store flag to show intro after registration
                sessionStorage.setItem('showIntroAfterAuth', 'true');
                window.location.href = '/api/auth/google?action=register';
            });
        }

        // Logout
        if (ui.elements.logoutBtn) {
            ui.elements.logoutBtn.addEventListener('click', async () => {
                await this.handleLogout();
            });
        }
    }

    async handleAuth() {
        const email = document.getElementById('auth-email').value;
        const password = document.getElementById('auth-password').value;

        try {
            let response;
            if (this.isLogin) {
                response = await api.login(email, password);
            } else {
                response = await api.signup(email, password);
            }

            this.currentUser = response.user;
            ui.hideModal('auth-modal');
            ui.showToast(response.message, 'success');

            this.onAuthSuccess();
        } catch (error) {
            ui.showToast(error.message, 'error');
        }
    }

    async handleLogout() {
        try {
            await api.logout();
            window.location.reload();
        } catch (error) {
            ui.showToast('Logout failed: ' + error.message, 'error');
        }
    }

    async onAuthSuccess() {
        // Update UI with user info
        if (ui.elements.userEmail) {
            ui.elements.userEmail.textContent = this.currentUser.email;
        }

        // Fetch and update balance
        await this.updateBalance();
    }

    async updateBalance() {
        try {
            const response = await api.getBalance();
            ui.updateBalance(response.balance);
            ui.updateStreak(response.streak);
        } catch (error) {
            console.error('Failed to fetch balance:', error);
        }
    }
}

export default new AuthManager();
