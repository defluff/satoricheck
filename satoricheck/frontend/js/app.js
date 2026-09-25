/**
 * Main Application Module
 * Orchestrates all managers and handles global setup
 */

import ui from './ui.js';
import auth from './auth.js';
import audio from './audio.js';
import factcheck from './factcheck.js';
import selection from './selection.js';
import api from './api.js';
import pitchdeck from './pitchdeck.js';
import media from './media.js';

class App {
    constructor() {
        this.analysisMode = localStorage.getItem('analysisMode') || 'factcheck'; // 'factcheck' or 'aidetect'
    }

    async init() {

        // Initialize auth first
        await auth.init();

        // Initialize Theme
        const savedTheme = localStorage.getItem('theme') || 'light';
        document.documentElement.setAttribute('data-theme', savedTheme);
        const darkModeToggle = document.getElementById('dark-mode-toggle');
        if (darkModeToggle) {
            darkModeToggle.checked = (savedTheme === 'dark');
        }

        // Set up all event listeners
        this.setupEventListeners();

        // Initialize selection handler
        selection.init();

        // Initialize Pitch Deck module
        pitchdeck.init();

        // Initialize Media Authenticity & Stream module
        media.init();

        // Set up audio result handler for auto-check (Standard mode)
        audio.onResult((transcript) => {
            factcheck.handleAutoCheck(transcript);
        });


        // Initialize shop packages from backend
        try {
            const packageData = await api.getPackages();
            if (packageData.success) {
                ui.renderPackages(packageData.packages);
            }
        } catch (error) {
            console.error('Failed to load shop packages:', error);
        }


    }

    setupEventListeners() {
        // Auth event listeners
        auth.setupEventListeners();

        // Factcheck event listeners
        factcheck.setupEventListeners();

        // Analysis Mode Toggle (Fact Check vs AI Detect)
        const analysisModeToggle = document.getElementById('analysis-mode-toggle');

        if (analysisModeToggle) {
            const modeButtons = analysisModeToggle.querySelectorAll('.analysis-mode-btn');

            // Restore saved mode
            modeButtons.forEach(btn => {
                btn.classList.toggle('active', btn.dataset.mode === this.analysisMode);
            });

            modeButtons.forEach(btn => {
                btn.addEventListener('click', () => {
                    this.analysisMode = btn.dataset.mode;
                    localStorage.setItem('analysisMode', this.analysisMode);

                    // Update UI
                    modeButtons.forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');

                    ui.showToast(
                        this.analysisMode === 'aidetect' ? '🤖 AI Detection Mode' : '✓ Fact Check Mode',
                        'info'
                    );
                });
            });
        }


        // Microphone button — starts/stops standard Web Speech Recognition
        ui.elements.micBtn.addEventListener('click', async () => {
            if (!audio.recognition) {
                const initialized = audio.init();
                if (!initialized) {
                    return;
                }
            }
            audio.start();
        });

        // Settings button
        ui.elements.settingsBtn.addEventListener('click', () => {
            ui.showModal('settings-modal');

            // Sync toggle state just in case
            const darkModeToggle = document.getElementById('dark-mode-toggle');
            if (darkModeToggle) {
                darkModeToggle.checked = document.documentElement.getAttribute('data-theme') === 'dark';
            }
        });

        // Dark Mode Toggle
        const darkModeToggle = document.getElementById('dark-mode-toggle');
        if (darkModeToggle) {
            darkModeToggle.addEventListener('change', () => {
                const theme = darkModeToggle.checked ? 'dark' : 'light';
                document.documentElement.setAttribute('data-theme', theme);
                localStorage.setItem('theme', theme);
            });
        }

        // Close settings modal
        ui.elements.closeSettingsModal.addEventListener('click', () => {
            ui.hideModal('settings-modal');
        });


        // Token balance click - show buy modal
        ui.elements.tokenCount.parentElement.addEventListener('click', () => {
            ui.showModal('buy-tokens-modal');
        });

        // Close buy tokens modal
        ui.elements.closeBuyModal.addEventListener('click', () => {
            ui.hideModal('buy-tokens-modal');
        });

        // Streak click - show streak modal
        ui.elements.streakDisplay.addEventListener('click', () => {
            ui.showModal('streak-modal');
        });

        // Close streak modal
        const closeStreakModal = document.getElementById('close-streak-modal');
        if (closeStreakModal) {
            closeStreakModal.addEventListener('click', () => {
                ui.hideModal('streak-modal');
            });
        }

        // Help button
        const helpBtn = document.getElementById('help-btn');
        if (helpBtn) {
            helpBtn.addEventListener('click', () => {
                ui.showModal('help-modal');
            });
        }

        // Close help modal
        const closeHelpModal = document.getElementById('close-help-modal');
        if (closeHelpModal) {
            closeHelpModal.addEventListener('click', () => {
                ui.hideModal('help-modal');
            });
        }

        // Show introduction button (in help modal)
        const showIntroBtn = document.getElementById('show-intro-btn');
        if (showIntroBtn) {
            showIntroBtn.addEventListener('click', () => {
                ui.hideModal('help-modal');
                ui.showModal('intro-modal');
            });
        }

        // Close intro modal
        const introCloseBtn = document.getElementById('intro-close-btn');
        if (introCloseBtn) {
            introCloseBtn.addEventListener('click', () => {
                ui.hideModal('intro-modal');
            });
        }

        // 🎬 Media button — switches to #media-view
        const navMediaBtn = document.getElementById('nav-media-btn');
        if (navMediaBtn) {
            navMediaBtn.addEventListener('click', () => {
                this.switchView('media');
            });
        }

        // 🧠 Fact Check button
        const navFactcheckBtn = document.getElementById('nav-factcheck-btn');
        if (navFactcheckBtn) {
            navFactcheckBtn.addEventListener('click', () => {
                this.switchView('factcheck');
            });
        }

        // 📊 Pitch Deck button
        const navPitchdeckBtn = document.getElementById('nav-pitchdeck-btn');
        if (navPitchdeckBtn) {
            navPitchdeckBtn.addEventListener('click', () => {
                this.switchView('pitchdeck');
            });
        }

        // Handle token package purchases (Event Delegation)
        document.body.addEventListener('click', async (e) => {
            const purchaseBtn = e.target.closest('.package-card button');
            if (purchaseBtn) {
                const card = purchaseBtn.closest('.package-card');
                const packageType = card.dataset.package;
                await this.handlePurchase(packageType);
            }
        });

        // Export button
        ui.elements.exportBtn.addEventListener('click', () => {
            ui.handleExport();
        });

        // Manage billing button - opens Billing Account modal
        ui.elements.manageBillingBtn.addEventListener('click', async () => {
            await this.handleManageBilling();
        });

        // Close billing account modal
        const closeBillingModal = document.getElementById('close-billing-modal');
        if (closeBillingModal) {
            closeBillingModal.addEventListener('click', () => {
                ui.hideModal('billing-account-modal');
            });
        }

        // Chrome Extension: Settings button
        const chromeExtBtn = document.getElementById('chrome-extension-btn');
        if (chromeExtBtn) {
            chromeExtBtn.addEventListener('click', () => {
                window.open('https://chromewebstore.google.com/detail/authenix-%E2%80%93-live-fact-ai-c/egamiabbbonhkjlhjbniocfpihpdcehl?authuser=0&hl=en-GB', '_blank', 'noopener,noreferrer');
            });
        }

        // Close modals on overlay click
        document.querySelectorAll('.modal-overlay').forEach(overlay => {
            overlay.addEventListener('click', () => {
                // Don't close auth modal on overlay click (must login)
                if (overlay.parentElement.id === 'auth-modal') {
                    return;
                }
                overlay.parentElement.classList.add('hidden');
            });
        });

        // Check for new user param (from backend) or storage flag
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('new_user') === 'true' || sessionStorage.getItem('showIntroAfterAuth') === 'true') {
            sessionStorage.removeItem('showIntroAfterAuth');

            // Clean URL without refresh
            if (urlParams.get('new_user') === 'true') {
                const newUrl = window.location.pathname;
                window.history.replaceState({}, '', newUrl);
            }

            // Small delay to ensure page is fully loaded
            setTimeout(() => {
                ui.showModal('intro-modal');
            }, 1000);
        }

        // Check for extension redirect (?ext=1)
        if (urlParams.get('ext') === '1') {
            window.history.replaceState({}, '', window.location.pathname);
            setTimeout(() => {
                ui.showToast('Logged in! You can now use the Authenix Chrome Extension.', 'success');
            }, 1000);
        }

        // Check for payment success in URL
        this.checkPaymentStatus();
    }


    /**
     * Switch between the three main views: factcheck, pitchdeck, media.
     * Delegates to pitchdeck's own show()/hide() to respect its isActive flag;
     * otherwise uses direct DOM toggling for factcheck ↔ media.
     * @param {'factcheck'|'pitchdeck'|'media'} view
     */
    switchView(view) {
        // Delegate to modules for specialized show/hide logic
        if (view === 'pitchdeck') {
            pitchdeck.show(); 
        } else {
            pitchdeck.hide();
        }

        if (view === 'media') {
            media.show();
        } else {
            media.hide();
        }

        const views = {
            factcheck: document.getElementById('factcheck-view'),
            pitchdeck: document.getElementById('pitchdeck-view'),
            media:     document.getElementById('media-view'),
        };
        const navBtns = {
            factcheck: document.getElementById('nav-factcheck-btn'),
            pitchdeck: document.getElementById('nav-pitchdeck-btn'),
            media:     document.getElementById('nav-media-btn'),
        };

        Object.keys(views).forEach(key => {
            const el = views[key];
            const btn = navBtns[key];
            
            if (el) el.classList.toggle('hidden', key !== view);
            if (btn) btn.classList.toggle('active', key === view);
        });

        console.log(`[App] Switched to ${view} view`);
    }

    setupMediaView() {
        // Delegated to media module (media.js)
    }

    async handlePurchase(packageType) {
        try {
            ui.showToast('Redirecting to checkout...', 'info');

            const response = await api.createCheckoutSession(packageType);

            // Redirect to Stripe checkout
            window.location.href = response.url;

        } catch (error) {
            ui.showToast('Purchase failed: ' + error.message, 'error');
        }
    }

    async handleManageBilling() {
        // Open the billing account modal
        ui.showModal('billing-account-modal');

        // Fetch and render transaction history
        const transactionList = document.getElementById('transaction-list');
        if (!transactionList) return;

        transactionList.innerHTML = '<div class="loading-transactions">Loading transactions...</div>';

        try {
            const response = await api.getTransactionHistory();

            if (response.success && response.transactions.length > 0) {
                // Filter: Only show Purchases (positive) or Bonuses. Hide internal usage costs (negative).
                const visibleTransactions = response.transactions.filter(t =>
                    t.type === 'purchase' || t.type === 'bonus' || t.amount > 0
                );

                if (visibleTransactions.length > 0) {
                    transactionList.innerHTML = visibleTransactions.map(t => {
                        const isPositive = t.amount > 0;
                        const date = new Date(t.timestamp).toLocaleDateString('en-US', {
                            month: 'short',
                            day: 'numeric',
                            year: 'numeric'
                        });
                        // Use textContent-safe values to prevent XSS
                        const safeDesc = t.description || t.type;
                        const amountPrefix = isPositive ? '+' : '';

                        return `
                            <div class="transaction-item">
                                <div class="transaction-info">
                                    <span class="transaction-desc">${this.escapeHtml(safeDesc)}</span>
                                    <span class="transaction-date">${date}</span>
                                </div>
                                <span class="transaction-amount ${isPositive ? 'positive' : 'negative'}">
                                    ${amountPrefix}${t.amount} CP
                                </span>
                            </div>
                        `;
                    }).join('');
                } else {
                    // Filtered list is empty (or original was empty)
                    this.renderEmptyTransactions(transactionList);
                }
            } else {
                this.renderEmptyTransactions(transactionList);
            }
        } catch (error) {
            console.error('Failed to load transactions:', error);
            // Show friendly message for auth errors (user not logged in)
            const isAuthError = error.message?.includes('Authentication') || error.message?.includes('401');
            transactionList.innerHTML = `
                <div class="transaction-empty">
                    <div class="transaction-empty-icon">${isAuthError ? '🔒' : '⚠️'}</div>
                    <p>${isAuthError ? 'Please sign in to view transactions' : 'Failed to load transactions'}</p>
                </div>
            `;
        }
    }

    /**
     * Escape HTML to prevent XSS
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    renderEmptyTransactions(container) {
        container.innerHTML = `
            <div class="transaction-empty">
                <div class="transaction-empty-icon">📋</div>
                <p>No transactions yet</p>
                <p style="font-size: 0.8rem; margin-top: 4px;">Purchase tokens to get started!</p>
            </div>
        `;
    }

    checkPaymentStatus() {
        const params = new URLSearchParams(window.location.search);

        if (params.get('payment') === 'success') {
            ui.showToast('Payment successful! Tokens added to your account', 'success');

            // Update balance
            auth.updateBalance();

            // Clean URL
            window.history.replaceState({}, document.title, window.location.pathname);
        } else if (params.get('payment') === 'cancelled') {
            ui.showToast('Payment cancelled', 'warning');
            window.history.replaceState({}, document.title, window.location.pathname);
        }
    }


}

// Initialize app when DOM is ready
const app = new App();

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => app.init());
} else {
    app.init();
}
