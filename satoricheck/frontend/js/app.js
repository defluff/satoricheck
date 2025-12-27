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
import livepro from './livepro.js';

class App {
    constructor() {
        this.liveProMode = false; // Standard mode by default
        this.analysisMode = localStorage.getItem('analysisMode') || 'factcheck'; // 'factcheck' or 'aidetect'
    }

    async init() {
        console.log('🚀 SatoriCheck initializing...');

        // Initialize auth first
        await auth.init();

        // Set up all event listeners
        this.setupEventListeners();

        // Initialize selection handler
        selection.init();

        // Set up audio result handler for auto-check (Standard mode)
        audio.onResult((transcript) => {
            factcheck.handleAutoCheck(transcript);
        });

        // Initialize Live Pro
        const liveProAvailable = await livepro.init();
        if (liveProAvailable) {
            console.log('⚡ Live Pro available');
            // Set up Live Pro transcript handler
            livepro.onTranscript((transcript, isFinal) => {
                ui.appendTranscript(transcript, isFinal);
                if (isFinal) {
                    factcheck.handleAutoCheck(transcript);
                }
            });
        } else {
            // Hide Live Pro button if not available
            const liveProBtn = document.getElementById('mode-live-pro');
            if (liveProBtn) {
                liveProBtn.style.display = 'none';
            }
        }

        // Initialize shop packages from backend
        try {
            const packageData = await api.getPackages();
            if (packageData.success) {
                ui.renderPackages(packageData.packages);
            }
        } catch (error) {
            console.error('Failed to load shop packages:', error);
        }


        console.log('✅ SatoriCheck ready!');
    }

    setupEventListeners() {
        // Auth event listeners
        auth.setupEventListeners();

        // Factcheck event listeners
        factcheck.setupEventListeners();

        // Transcription mode selector
        const modeStandard = document.getElementById('mode-standard');
        const modeLivePro = document.getElementById('mode-live-pro');
        const liveProIndicator = document.getElementById('live-pro-indicator');

        if (modeStandard && modeLivePro) {
            modeStandard.addEventListener('click', () => {
                this.liveProMode = false;
                modeStandard.classList.add('active');
                modeLivePro.classList.remove('active');
                liveProIndicator?.classList.add('hidden');
                ui.elements.micBtn.classList.remove('live-pro-active');
            });

            modeLivePro.addEventListener('click', () => {
                // Check if we should skip confirmation modal
                const hideModal = localStorage.getItem('hideLiveProModal') === 'true';

                if (hideModal) {
                    // Activate directly
                    this.activateLiveProMode();
                } else {
                    // Show confirmation modal
                    ui.showModal('live-pro-modal');
                }
            });
        }

        // Analysis Mode Toggle (Fact Check vs AI Detect)
        const analysisModeToggle = document.getElementById('analysis-mode-toggle');
        const smartAgentToggle = document.getElementById('smart-agent-toggle');

        if (analysisModeToggle) {
            const modeButtons = analysisModeToggle.querySelectorAll('.analysis-mode-btn');

            // Restore saved mode
            modeButtons.forEach(btn => {
                btn.classList.toggle('active', btn.dataset.mode === this.analysisMode);
            });

            // Disable Smart Agent if in AI detect mode
            if (smartAgentToggle && this.analysisMode === 'aidetect') {
                smartAgentToggle.disabled = true;
                smartAgentToggle.parentElement.style.opacity = '0.5';
            }

            modeButtons.forEach(btn => {
                btn.addEventListener('click', () => {
                    this.analysisMode = btn.dataset.mode;
                    localStorage.setItem('analysisMode', this.analysisMode);

                    // Update UI
                    modeButtons.forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');

                    // Enable/disable Smart Agent based on mode
                    if (smartAgentToggle) {
                        if (this.analysisMode === 'aidetect') {
                            smartAgentToggle.disabled = true;
                            smartAgentToggle.checked = false;
                            smartAgentToggle.parentElement.style.opacity = '0.5';
                            factcheck.smartAgent = false;
                        } else {
                            smartAgentToggle.disabled = false;
                            smartAgentToggle.parentElement.style.opacity = '1';
                        }
                    }

                    ui.showToast(
                        this.analysisMode === 'aidetect' ? '🤖 AI Detection Mode' : '✓ Fact Check Mode',
                        'info'
                    );
                });
            });
        }

        // Live Pro confirmation modal handlers
        const activateLiveProBtn = document.getElementById('activate-live-pro');
        const cancelLiveProBtn = document.getElementById('cancel-live-pro');
        const closeLiveProModal = document.getElementById('close-live-pro-modal');
        const hideModalCheckbox = document.getElementById('hide-live-pro-modal-checkbox');

        if (activateLiveProBtn) {
            activateLiveProBtn.addEventListener('click', () => {
                // Save preference if checkbox is checked
                if (hideModalCheckbox?.checked) {
                    localStorage.setItem('hideLiveProModal', 'true');
                }
                ui.hideModal('live-pro-modal');
                this.activateLiveProMode();
            });
        }

        if (cancelLiveProBtn) {
            cancelLiveProBtn.addEventListener('click', () => {
                ui.hideModal('live-pro-modal');
            });
        }

        if (closeLiveProModal) {
            closeLiveProModal.addEventListener('click', () => {
                ui.hideModal('live-pro-modal');
            });
        }

        // Microphone button - handles both Standard and Live Pro modes
        ui.elements.micBtn.addEventListener('click', async () => {
            if (this.liveProMode) {
                // Live Pro mode
                if (livepro.isActive) {
                    await livepro.stop();
                    ui.elements.micBtn.classList.remove('active', 'live-pro-active');
                    liveProIndicator?.classList.add('hidden');
                } else {
                    const deviceId = ui.selectedMicId || null;
                    const started = await livepro.start(deviceId);
                    if (started) {
                        ui.elements.micBtn.classList.add('active', 'live-pro-active');
                        liveProIndicator?.classList.remove('hidden');
                    }
                }
            } else {
                // Standard mode (browser SpeechRecognition)
                if (!audio.recognition) {
                    const initialized = audio.init();
                    if (!initialized) {
                        return;
                    }
                }
                audio.start();
            }
        });

        // Settings button
        ui.elements.settingsBtn.addEventListener('click', async () => {
            ui.showModal('settings-modal');
            await ui.updateAudioDevices();
        });

        // Close settings modal
        ui.elements.closeSettingsModal.addEventListener('click', () => {
            ui.hideModal('settings-modal');
        });

        // Mic selection change
        if (ui.elements.micSelect) {
            ui.elements.micSelect.addEventListener('change', async (e) => {
                const deviceId = e.target.value;
                localStorage.setItem('selectedMicId', deviceId);
                ui.selectedMicId = deviceId;

                // Ping the device to ensure browser has permission and 'focuses' it
                if (deviceId) {
                    try {
                        const stream = await navigator.mediaDevices.getUserMedia({
                            audio: { deviceId: { exact: deviceId } }
                        });
                        // Stop tracks immediately, we just wanted to 'activate' the device choice in browser
                        stream.getTracks().forEach(track => track.stop());
                        ui.showToast('Microphone updated', 'success');
                    } catch (error) {
                        console.error('Mic selection error:', error);
                        ui.showToast('Could not switch to that microphone', 'warning');
                    }
                }
            });
        }

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

        // Manage billing button
        ui.elements.manageBillingBtn.addEventListener('click', async () => {
            await this.handleManageBilling();
        });

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

        // Check for payment success in URL
        this.checkPaymentStatus();
    }

    /**
     * Activate Live Pro mode - updates UI and sets mode flag
     */
    activateLiveProMode() {
        const modeStandard = document.getElementById('mode-standard');
        const modeLivePro = document.getElementById('mode-live-pro');

        this.liveProMode = true;
        modeLivePro?.classList.add('active');
        modeStandard?.classList.remove('active');
        ui.showToast('⚡ Live Pro mode activated (1 CP/min)', 'success');
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
        try {
            const response = await api.createPortalSession();
            window.open(response.url, '_blank');
        } catch (error) {
            ui.showToast('Failed to open billing portal: ' + error.message, 'error');
        }
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
