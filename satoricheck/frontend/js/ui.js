/**
 * UI Manager Module
 * Handles all UI updates, modals, toasts, and display logic
 */

import api from './api.js';

class UIManager {
    constructor() {
        this.elements = {
            // Header
            tokenCount: document.getElementById('token-count'),
            batteryLevel: document.getElementById('battery-level'),
            streakCount: document.getElementById('streak-count'),
            streakDisplay: document.getElementById('streak-display'),
            testModeBanner: document.getElementById('test-mode-banner'),

            // Transcript
            transcriptContainer: document.getElementById('transcript-container'),
            micBtn: document.getElementById('mic-btn'),
            checkNowBtn: document.getElementById('check-now-btn'),
            autoCheckToggle: document.getElementById('auto-check-toggle'),

            // Feed
            feedContainer: document.getElementById('feed-container'),
            exportBtn: document.getElementById('export-btn'),

            // Modals
            authModal: document.getElementById('auth-modal'),
            authForm: document.getElementById('auth-form'),
            authTitle: document.getElementById('auth-title'),
            authBtnText: document.getElementById('auth-btn-text'),
            authToggle: document.getElementById('auth-toggle'),
            authToggleText: document.getElementById('auth-toggle-text'),

            buyTokensModal: document.getElementById('buy-tokens-modal'),
            closeBuyModal: document.getElementById('close-buy-modal'),

            settingsModal: document.getElementById('settings-modal'),
            closeSettingsModal: document.getElementById('close-settings-modal'),
            settingsBtn: document.getElementById('settings-btn'),
            logoutBtn: document.getElementById('logout-btn'),
            passwordForm: document.getElementById('password-form'),
            userEmail: document.getElementById('user-email'),
            manageBillingBtn: document.getElementById('manage-billing-btn'),
            streakRoadmap: document.getElementById('streak-roadmap'),

            // Selection tooltip
            selectionTooltip: document.getElementById('selection-tooltip'),

            // Toast container
            toastContainer: document.getElementById('toast-container')
        };

        this.transcriptContent = '';
        this.cardCounter = 0;

        // Strip formatting on paste - always use clean plain text
        if (this.elements.transcriptContainer) {
            this.elements.transcriptContainer.addEventListener('paste', (e) => {
                e.preventDefault();
                const text = (e.clipboardData || window.clipboardData).getData('text/plain');
                document.execCommand('insertText', false, text);
            });
        }
    }

    // Modal controls
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

    // Auth modal toggle between login/signup
    setAuthMode(isLogin) {
        if (isLogin) {
            this.elements.authTitle.textContent = 'Sign In';
            this.elements.authBtnText.textContent = 'Sign In';
            this.elements.authToggleText.textContent = 'Don\'t have an account? Sign up';
        } else {
            this.elements.authTitle.textContent = 'Sign Up';
            this.elements.authBtnText.textContent = 'Create Account';
            this.elements.authToggleText.textContent = 'Already have an account? Sign in';
        }
    }

    // Update token balance display
    updateBalance(balance) {
        this.elements.tokenCount.textContent = balance;

        // Update battery level (max 5000 for visual)
        const percentage = Math.min((balance / 5000) * 100, 100);
        this.elements.batteryLevel.style.width = `${percentage}%`;

        // Show warning if low
        if (balance < 10 && balance > 0) {
            this.showToast('Low balance! Recharge your batteries', 'warning');
        } else if (balance === 0) {
            this.showToast('No tokens remaining! Purchase more to continue', 'error');
        }
    }

    // Update streak display
    updateStreak(streakData) {
        if (streakData.current_streak !== undefined) {
            this.elements.streakCount.textContent = streakData.current_streak;
        }

        // Update roadmap in settings
        if (streakData.current_milestone && this.elements.streakRoadmap) {
            this.renderStreakRoadmap(streakData);
        }
    }

    renderStreakRoadmap(streakData) {
        const currentStreak = streakData.current_streak || 0;

        // Calculate Cycle (Prestige Level)
        // Cycle 0: Days 1-30
        // Cycle 1: Days 31-60 (Legendary Blue)
        const cycle = Math.floor((currentStreak - 1) / 30);
        const displayStreak = (currentStreak - 1) % 30 + 1; // 1 to 30

        const isLegendary = cycle > 0;
        const themeClass = isLegendary ? 'legendary' : '';
        const badgeHtml = isLegendary ? '<span class="legendary-badge">👑</span>' : '';

        // Reward schedule (Day within cycle)
        const rewards = {
            6: { icon: '🔋', amount: 100 },
            14: { icon: '🔋', amount: 200 },
            21: { icon: '⚡', amount: 400 },
            30: { icon: '💎', amount: 1000 }
        };

        // 1. Header with large count
        let html = `
            <div class="streak-info-container ${themeClass}">
                <span class="streak-count-large ${themeClass}">
                    ${currentStreak} Days ${badgeHtml}
                </span>
                <p class="modal-subtitle">
                    ${isLegendary ? 'Legendary Streak Active! Blue Flame Ignited.' : 'Keep the energy flowing!'}
                </p>
            </div>
            <div class="streak-grid ${themeClass}">
        `;

        // 2. Grid items (30 Days)
        for (let i = 1; i <= 30; i++) {
            let classes = 'streak-day';
            let iconHtml = '';

            // Calculate status based on current cycle
            // If i <= displayStreak, it is completed in CURRENT cycle (or previous cycles if we just show current)
            // Logic: Shows progress for CURRENT 30-day chunk.

            if (i <= displayStreak) {
                classes += ' completed';
                if (i === displayStreak) classes += ' today';
            }

            if (isLegendary) {
                classes += ' legendary';
            }

            // Rewards (Add tooltip data)
            let tooltipAttr = '';
            if (rewards[i]) {
                const isBig = true;
                classes += ' reward-day';
                iconHtml = `<span class="streak-reward-icon ${isBig ? 'large' : ''}">${rewards[i].icon}</span>`;
                tooltipAttr = `data-tooltip="Reward: ${rewards[i].amount} CP"`;
            }

            html += `
                <div class="${classes}" ${tooltipAttr}>
                    <span class="day-number">${i}</span>
                    ${iconHtml}
                </div>
            `;
        }

        html += '</div>';
        this.elements.streakRoadmap.innerHTML = html;

        // Update header flame color if legendary
        if (isLegendary) {
            const streakIcon = document.querySelector('.streak-icon');
            if (streakIcon) streakIcon.textContent = '🔵'; // Blue circle or flame
        }
    }

    // Show test mode banner
    showTestModeBanner() {
        this.elements.testModeBanner.classList.remove('hidden');
    }

    // Transcript management
    appendTranscript(text, isFinal = false) {
        const existingPlaceholder = this.elements.transcriptContainer.querySelector('.transcript-placeholder');
        if (existingPlaceholder) {
            existingPlaceholder.remove();
        }

        const transcriptText = this.elements.transcriptContainer.querySelector('.transcript-text')
            || this.createTranscriptElement();

        if (isFinal) {
            // Remove all interim spans before adding final text
            const interimSpans = transcriptText.querySelectorAll('.interim');
            interimSpans.forEach(span => span.remove());

            transcriptText.textContent += text + ' ';
            this.transcriptContent = transcriptText.textContent;
        } else {
            // Remove previous interim results before adding new one
            const interimSpans = transcriptText.querySelectorAll('.interim');
            interimSpans.forEach(span => span.remove());

            const tempSpan = document.createElement('span');
            tempSpan.className = 'interim';
            tempSpan.textContent = text;
            transcriptText.appendChild(tempSpan);
        }

        // Auto-scroll
        this.elements.transcriptContainer.scrollTop = this.elements.transcriptContainer.scrollHeight;
    }

    createTranscriptElement() {
        const div = document.createElement('div');
        div.className = 'transcript-text';
        this.elements.transcriptContainer.appendChild(div);
        return div;
    }

    // Set listening state for mic button
    setListeningState(isListening) {
        if (isListening) {
            this.elements.micBtn.classList.add('active');
        } else {
            this.elements.micBtn.classList.remove('active');
        }
    }

    // Create fact-check card
    createCard(claim, isPending = false) {
        const cardId = `card-${++this.cardCounter}`;

        // Remove feed placeholder
        const placeholder = this.elements.feedContainer.querySelector('.feed-placeholder');
        if (placeholder) {
            placeholder.remove();
        }

        const card = document.createElement('div');
        card.className = `fact-check-card ${isPending ? 'pending' : ''}`;
        card.id = cardId;

        card.innerHTML = `
            <div class="card-header">
                <span class="verdict-badge PENDING">
                    ${isPending ? '<span class="loading-spinner"></span> Checking...' : 'Pending'}
                </span>
                <span class="card-meta">${new Date().toLocaleTimeString()}</span>
            </div>
            <p class="claim-text">"${this.escapeHtml(claim)}"</p>
            <div class="card-details"></div>
        `;

        // Add click handler for expanding (only when not pending)
        if (!isPending) {
            card.addEventListener('click', () => {
                if (!card.classList.contains('pending')) {
                    card.classList.toggle('expanded');
                }
            });
        }

        // Add to top of feed
        this.elements.feedContainer.insertBefore(card, this.elements.feedContainer.firstChild);

        return cardId;
    }

    // Update fact-check card with results
    updateCard(cardId, result) {
        const card = document.getElementById(cardId);
        if (!card) return;

        card.classList.remove('pending');

        const verdictBadge = card.querySelector('.verdict-badge');
        verdictBadge.className = `verdict-badge ${result.verdict}`;
        verdictBadge.textContent = result.verdict.replace(/_/g, ' ');

        // For NOT_A_CLAIM, don't add details (save space and tokens)
        if (result.verdict === 'NOT_A_CLAIM') {
            card.style.cursor = 'default';
            return;
        }

        // Create details container
        let detailsContainer = card.querySelector('.card-details');
        if (!detailsContainer) {
            detailsContainer = document.createElement('div');
            detailsContainer.className = 'card-details';
            card.appendChild(detailsContainer);
        }

        // Add explanation
        if (result.explanation) {
            const explanation = document.createElement('p');
            explanation.className = 'explanation-text';
            explanation.textContent = result.explanation;
            detailsContainer.appendChild(explanation);
        }

        // Add fallacy if detected
        if (result.fallacy) {
            const fallacy = document.createElement('div');
            fallacy.className = 'fallacy-tag';
            fallacy.textContent = `⚠️ ${result.fallacy}`;
            detailsContainer.appendChild(fallacy);
        }

        // Add sources
        if (result.sources && result.sources.length > 0) {
            const sourcesDiv = document.createElement('div');
            sourcesDiv.innerHTML = `
                <strong style="color: var(--color-text-secondary); font-size: 0.875rem;">Sources:</strong>
                <ul class="sources-list">
                    ${result.sources.map(url => `
                        <li><a href="${url}" target="_blank" rel="noopener">${url}</a></li>
                    `).join('')}
                </ul>
            `;
            detailsContainer.appendChild(sourcesDiv);
        }

        // Add click handler to toggle expansion
        card.style.cursor = 'pointer';
        card.addEventListener('click', () => {
            card.classList.toggle('expanded');
        });
    }

    // Show selection tooltip
    showSelectionTooltip(x, y) {
        this.elements.selectionTooltip.classList.remove('hidden');
        this.elements.selectionTooltip.style.left = `${x}px`;
        this.elements.selectionTooltip.style.top = `${y}px`;
    }

    hideSelectionTooltip() {
        this.elements.selectionTooltip.classList.add('hidden');
    }

    // Toast notifications
    showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;

        this.elements.toastContainer.appendChild(toast);

        // Auto-remove after 5 seconds
        setTimeout(() => {
            toast.style.animation = 'fadeOut 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }, 5000);
    }

    // Utility: escape HTML
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Export functionality
    async handleExport() {
        try {
            const blob = await api.exportFactChecks('csv');

            // Create download link
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `satoricheck_export_${new Date().toISOString().split('T')[0]}.csv`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);

            this.showToast('Export successful!', 'success');
        } catch (error) {
            this.showToast('Export failed: ' + error.message, 'error');
        }
    }
}

export default new UIManager();
