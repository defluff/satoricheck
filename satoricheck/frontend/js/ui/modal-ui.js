/**
 * Modal UI Module
 * Handles dialogs and overlays
 */

class ModalUI {
    constructor() {
        this.authTitle = document.getElementById('auth-title');
        this.authBtnText = document.getElementById('auth-btn-text');
        this.authToggleText = document.getElementById('auth-toggle-text');
        this.testModeBanner = document.getElementById('test-mode-banner');
    }

    showModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.remove('hidden');
        }
    }

    hideModal(modalId) {
        const modal = document.getElementById(modalId);
        if (modal) {
            modal.classList.add('hidden');
        }
    }

    setAuthMode(isLogin) {
        if (isLogin) {
            this.authTitle.textContent = 'Sign In';
            this.authBtnText.textContent = 'Sign In';
            this.authToggleText.textContent = "Don't have an account? Sign up";
        } else {
            this.authTitle.textContent = 'Sign Up';
            this.authBtnText.textContent = 'Create Account';
            this.authToggleText.textContent = 'Already have an account? Sign in';
        }
    }

    showTestModeBanner() {
        if (this.testModeBanner) {
            this.testModeBanner.classList.remove('hidden');
        }
    }
}

export default new ModalUI();
