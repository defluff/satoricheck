/**
 * Modal UI Module
 * Handles dialogs and overlays
 */

class ModalUI {
    constructor() {
        this.authTitle = document.getElementById('auth-title');
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

    showTestModeBanner() {
        if (this.testModeBanner) {
            this.testModeBanner.classList.remove('hidden');
        }
    }
}

export default new ModalUI();
