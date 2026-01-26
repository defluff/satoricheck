/**
 * UI Manager Module (Facade)
 * Coordinates all specialized UI modules
 */

import toastUI from './ui/toast-ui.js';
import modalUI from './ui/modal-ui.js';
import exportUI from './ui/export-ui.js';
import audioUI from './ui/audio-ui.js';
import transcriptUI from './ui/transcript-ui.js';
import billingUI from './ui/billing-ui.js';
import streakUI from './ui/streak-ui.js';
import cardUI from './ui/card-ui.js';

class UIManager {
    constructor() {
        // Initialize sub-modules
        this.toast = toastUI;
        this.modal = modalUI;
        this.export = exportUI;
        this.audio = audioUI;
        this.transcript = transcriptUI;
        this.billing = billingUI;
        this.streak = streakUI;
        this.card = cardUI;

        // DOM Elements - Required by app.js, selection.js, and other modules
        this.elements = {
            // Editor & Transcript
            transcriptContainer: document.getElementById('transcript-container'),
            checkNowBtn: document.getElementById('check-now-btn'),
            autoCheckToggle: document.getElementById('auto-check-toggle'),
            selectionTooltip: document.getElementById('selection-tooltip'),

            // Header Actions
            micBtn: document.getElementById('mic-btn'),
            settingsBtn: document.getElementById('settings-btn'),
            exportBtn: document.getElementById('export-btn'),
            logoutBtn: document.getElementById('logout-btn'),

            // Token & Streak
            tokenCount: document.getElementById('token-count'),
            streakDisplay: document.getElementById('streak-display'),

            // Modals & Settings
            closeBuyModal: document.getElementById('close-buy-modal'),
            closeSettingsModal: document.getElementById('close-settings-modal'),
            micSelect: document.getElementById('mic-select'),
            manageBillingBtn: document.getElementById('manage-billing-btn'),
            userEmail: document.getElementById('user-email')
        };

        // Store selected mic ID (legacy compatibility)
        this.selectedMicId = localStorage.getItem('selectedMicId') || null;

        // Strip formatting on paste
        if (this.elements.transcriptContainer) {
            this.elements.transcriptContainer.addEventListener('paste', (e) => {
                e.preventDefault();
                const text = (e.clipboardData || window.clipboardData).getData('text/plain');
                document.execCommand('insertText', false, text);
            });
        }
    }


    // ===== Toast Delegation =====
    showToast(message, type = 'info') {
        this.toast.show(message, type);
    }

    // ===== Modal Delegation =====
    showModal(modalId) {
        this.modal.showModal(modalId);
    }

    hideModal(modalId) {
        this.modal.hideModal(modalId);
    }

    setAuthMode(isLogin) {
        this.modal.setAuthMode(isLogin);
    }

    showTestModeBanner() {
        this.modal.showTestModeBanner();
    }

    // ===== Billing Delegation =====
    updateBalance(balance) {
        this.billing.updateBalance(balance, (msg, type) => this.showToast(msg, type));
    }

    renderPackages(packages) {
        this.billing.renderPackages(packages);
    }

    // ===== Streak Delegation =====
    updateStreak(streakData) {
        this.streak.updateStreak(streakData);
    }

    renderStreakRoadmap(streakData) {
        this.streak.renderStreakRoadmap(streakData);
    }

    // ===== Transcript Delegation =====
    appendTranscript(text, isFinal = false) {
        this.transcript.appendTranscript(text, isFinal);
    }

    createTranscriptElement() {
        return this.transcript.createTranscriptElement();
    }

    setListeningState(isListening) {
        this.transcript.setListeningState(isListening);
    }

    // ===== Audio Delegation =====
    updateAudioDevices() {
        return this.audio.updateAudioDevices();
    }

    requestMicPermission() {
        return this.audio.requestMicPermission();
    }

    // ===== Card Delegation =====
    createCard(claim, isPending = false) {
        return this.card.createCard(claim, isPending);
    }

    updateCard(cardId, result) {
        this.card.updateCard(cardId, result);
    }

    showSelectionTooltip(x, y) {
        this.card.showSelectionTooltip(x, y);
    }

    hideSelectionTooltip() {
        this.card.hideSelectionTooltip();
    }

    removeCard(cardId) {
        this.card.removeCard(cardId);
    }

    escapeHtml(text) {
        return this.card.escapeHtml(text);
    }

    // ===== Stop Button Helper =====
    setCheckButtonMode(mode) {
        const btn = this.elements.checkNowBtn;
        if (!btn) return;

        if (mode === 'stop') {
            btn.textContent = '⏹ Stop';
            btn.classList.add('btn-danger');
            btn.title = "Stop processing after current check completes";
        } else {
            btn.textContent = 'Check';
            btn.classList.remove('btn-danger');
            btn.title = "";
        }
    }

    // ===== Export Delegation =====
    async handleExport() {
        const result = await this.export.handleExport();
        if (result.success) {
            this.showToast('Export successful!', 'success');
        } else {
            this.showToast('Export failed: ' + result.error, 'error');
        }
    }
}

export default new UIManager();
