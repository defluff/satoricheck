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
import pitchdeck from './pitchdeck.js';

class App {
    constructor() {
        this.liveProMode = false; // Standard mode by default
        this.analysisMode = localStorage.getItem('analysisMode') || 'factcheck'; // 'factcheck' or 'aidetect'
    }

    async init() {

        // Initialize auth first
        await auth.init();

        // Set up all event listeners
        this.setupEventListeners();

        // Initialize selection handler
        selection.init();

        // Initialize Pitch Deck module
        pitchdeck.init();

        // Set up audio result handler for auto-check (Standard mode)
        audio.onResult((transcript) => {
            factcheck.handleAutoCheck(transcript);
        });

        // Initialize Live Pro
        const liveProAvailable = await livepro.init();
        if (liveProAvailable) {
            // Set up Live Pro transcript handler
            livepro.onTranscript((transcript, isFinal) => {
                ui.appendTranscript(transcript, isFinal);
                if (isFinal) {
                    factcheck.handleAutoCheck(transcript);
                }
            });

            // Handle automatic stops (connection loss, etc)
            livepro.onStop(() => {
                ui.elements.micBtn.classList.remove('active', 'live-pro-active');
                const liveProIndicator = document.getElementById('live-pro-indicator');
                if (liveProIndicator) liveProIndicator.classList.add('hidden');
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

        // Request microphone access button
        const requestMicAccessBtn = document.getElementById('request-mic-access-btn');
        if (requestMicAccessBtn) {
            requestMicAccessBtn.addEventListener('click', async () => {
                requestMicAccessBtn.disabled = true;
                requestMicAccessBtn.textContent = 'Requesting...';

                const granted = await ui.requestMicPermission();

                if (granted) {
                    ui.showToast('Microphone access granted! Device list updated.', 'success');
                    requestMicAccessBtn.textContent = '✓ Access Granted';
                } else {
                    ui.showToast('Microphone permission denied. Enable in browser settings.', 'error');
                    requestMicAccessBtn.disabled = false;
                    requestMicAccessBtn.textContent = 'Request Access';
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

        // Wire up internal media view interactions
        this.setupMediaView();

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

        // Extension token: Settings button
        const showExtTokenBtn = document.getElementById('show-extension-token-btn');
        if (showExtTokenBtn) {
            showExtTokenBtn.addEventListener('click', () => {
                this.showExtensionTokenModal();
            });
        }

        // Extension token: close modal
        const closeExtTokenModal = document.getElementById('close-extension-token-modal');
        if (closeExtTokenModal) {
            closeExtTokenModal.addEventListener('click', () => {
                ui.hideModal('extension-token-modal');
            });
        }

        // Extension token: copy button
        const copyExtTokenBtn = document.getElementById('copy-extension-token-btn');
        if (copyExtTokenBtn) {
            copyExtTokenBtn.addEventListener('click', () => {
                this.copyExtensionToken();
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

        // Check for extension redirect (?ext=1) — show token modal after login
        if (urlParams.get('ext') === '1') {
            window.history.replaceState({}, '', window.location.pathname);
            setTimeout(() => {
                this.showExtensionTokenModal();
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
        // Toast removed to avoid confusion (Live Pro isn't 'Active' until Mic is clicked)
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

    /**
     * Initialise all interactions within the #media-view shell:
     * tab switching (upload ↔ URL), drag-and-drop, file input,
     * URL input, enable/disable of the Analyse button, and
     * the TEST_MODE demo result render.
     */
    setupMediaView() {
        const tabUpload         = document.getElementById('ma-tab-upload');
        const tabUrl            = document.getElementById('ma-tab-url');
        const uploadPanel       = document.getElementById('ma-upload-panel');
        const urlPanel          = document.getElementById('ma-url-panel');
        const dropZone          = document.getElementById('ma-drop-zone');
        const fileInput         = document.getElementById('ma-file-input');
        const urlInput          = document.getElementById('ma-url-input');
        const analyseBtn        = document.getElementById('ma-analyse-btn');
        const resultSection     = document.getElementById('ma-result-section');
        const resultPlaceholder = document.getElementById('ma-result-placeholder');

        if (!tabUpload) return; // Guard: media-view not in DOM

        // --- Tab switcher ---
        tabUpload.addEventListener('click', () => {
            tabUpload.classList.add('active');
            tabUrl.classList.remove('active');
            uploadPanel.classList.remove('hidden');
            urlPanel.classList.add('hidden');
            this._updateAnalyseBtn(analyseBtn, dropZone, urlInput, 'upload');
        });

        tabUrl.addEventListener('click', () => {
            tabUrl.classList.add('active');
            tabUpload.classList.remove('active');
            urlPanel.classList.remove('hidden');
            uploadPanel.classList.add('hidden');
            this._updateAnalyseBtn(analyseBtn, dropZone, urlInput, 'url');
        });

        // --- Drop zone: click to open file picker ---
        dropZone.addEventListener('click', () => fileInput.click());
        dropZone.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') fileInput.click();
        });

        // --- Drag-and-drop visual feedback ---
        dropZone.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('drag-over');
        });
        dropZone.addEventListener('dragleave', () => {
            dropZone.classList.remove('drag-over');
        });
        dropZone.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('drag-over');
            const file = e.dataTransfer.files[0];
            if (file) this._handleFileSelected(file, dropZone, analyseBtn);
        });

        // --- File input change ---
        fileInput.addEventListener('change', () => {
            const file = fileInput.files[0];
            if (file) this._handleFileSelected(file, dropZone, analyseBtn);
        });

        // "Change file" text resets the drop zone
        dropZone.querySelector('.ma-file-selected-change')?.addEventListener('click', (e) => {
            e.stopPropagation();
            fileInput.value = '';
            this._mediaFile = null;
            dropZone.classList.remove('has-file');
            document.getElementById('ma-selected-filename').textContent = '';
            analyseBtn.disabled = true;
        });

        // --- URL input ---
        urlInput.addEventListener('input', () => {
            analyseBtn.disabled = urlInput.value.trim() === '';
        });

        // --- Analyse button ---
        analyseBtn.addEventListener('click', async () => {
            const activeTab = tabUpload.classList.contains('active') ? 'upload' : 'url';
            

            // Set loading state
            const originalBtnText = analyseBtn.innerHTML;
            analyseBtn.disabled = true;
            analyseBtn.innerHTML = '<span class="spinner"></span> Analysing...';
            
            try {
                let response;
                if (activeTab === 'upload') {
                    const file = this._mediaFile || fileInput.files[0];
                    if (!file) throw new Error('Please select a file first.');
                    response = await api.analyzeMedia(file);
                } else {
                    const url = urlInput.value.trim();
                    if (!url) throw new Error('Please enter a media URL.');
                    response = await api.analyzeMediaUrl(url);
                }

                if (response.success) {
                    this._renderMediaResult(response.result, resultSection, resultPlaceholder);
                    ui.showToast('Analysis complete', 'success');
                    
                    // Update global balance if returned
                    if (response.new_balance !== undefined) {
                        const balanceEl = document.getElementById('user-balance');
                        if (balanceEl) balanceEl.textContent = response.new_balance;
                    }
                } else {
                    throw new Error(response.error || 'Analysis failed');
                }
            } catch (error) {
                ui.showToast(error.message, 'error');
            } finally {
                analyseBtn.disabled = false;
                analyseBtn.innerHTML = originalBtnText;
            }
        });
    }

    /**
     * Render media analysis result into the result panel.
     * @param {Object} result The analysis result object
     * @param {HTMLElement} resultSection
     * @param {HTMLElement} resultPlaceholder
     */
    _renderMediaResult(result, resultSection, resultPlaceholder) {
        if (!result) return;

        resultPlaceholder.classList.add('hidden');
        resultSection.classList.remove('hidden');

        // Update Verdict Card
        const verdictCard = document.getElementById('ma-verdict-card');
        const verdictLabel = document.getElementById('ma-verdict-label');
        const verdictEmoji = document.getElementById('ma-verdict-emoji');
        const reasoningText = document.getElementById('ma-reasoning-text');

        const verdictMap = {
            'AI Generated':      { type: 'ai',          emoji: '🤖' },
            'Likely Manipulated': { type: 'manipulated', emoji: '⚠️' },
            'Appears Authentic':  { type: 'authentic',   emoji: '✅' }
        };

        const verdictInfo = verdictMap[result.verdict] || { type: 'manipulated', emoji: '❓' };
        
        verdictCard.setAttribute('data-verdict', verdictInfo.type);
        verdictLabel.textContent = result.verdict;
        verdictEmoji.textContent = verdictInfo.emoji;
        
        // Security: Sanitize AI-generated explanation
        if (window.DOMPurify) {
            reasoningText.innerHTML = DOMPurify.sanitize(result.explanation || '');
        } else {
            reasoningText.textContent = result.explanation || '';
        }

        // Update Confidence Arc
        const confidenceValue = document.getElementById('ma-confidence-value');
        const confidenceArc = document.getElementById('ma-confidence-arc');
        const conf = result.confidence || 0;
        
        confidenceValue.textContent = `${conf}%`;
        confidenceArc.style.setProperty('--arc-fill', `${conf}%`);
        
        // Arc color based on verdict and confidence
        let arcColor = '#eab308'; // Yellow (Uncertain)
        if (verdictInfo.type === 'ai') arcColor = '#ef4444'; // Red
        if (verdictInfo.type === 'authentic' && conf > 70) arcColor = '#22c55e'; // Green
        confidenceArc.style.setProperty('--arc-color', arcColor);

        // Update Criteria Strips
        const criteria = result.criteria || {};
        const updateCriterion = (id, data) => {
            const signalTag = document.getElementById(`ma-signal-${id}`);
            const bar = document.getElementById(`ma-bar-${id}`);
            const desc = document.getElementById(`ma-desc-${id}`);

            if (!signalTag || !data) return;

            // Map friendly tags to CSS signal states
            const tagLower = (data.tag || '').toLowerCase();
            let signal = 'uncertain';
            if (tagLower.includes('high') || tagLower.includes('suspicious')) signal = 'suspicious';
            else if (tagLower.includes('low') || tagLower.includes('clean') || tagLower.includes('clear')) signal = 'clear';
            else if (tagLower.includes('med') || tagLower.includes('uncertain')) signal = 'uncertain';

            const score = data.score || 0;

            signalTag.setAttribute('data-signal', signal);
            signalTag.textContent = `${this._getSignalEmoji(signal)} ${data.tag}`;
            
            bar.setAttribute('data-signal', signal);
            bar.style.width = `${score}%`;
            bar.style.setProperty('--fill', `${score}%`);
            
            // Security: Sanitize AI-generated detail
            if (window.DOMPurify) {
                desc.innerHTML = DOMPurify.sanitize(data.detail || '');
            } else {
                desc.textContent = data.detail || '';
            }
        };

        updateCriterion('physics',   criteria.physics);
        updateCriterion('bio',       criteria.bio);
        updateCriterion('context',   criteria.context);
        updateCriterion('compression', criteria.compression);
        updateCriterion('metadata',  criteria.metadata);
        
        // Smooth scroll to results on mobile
        if (window.innerWidth < 1024) {
            resultSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    }

    _getSignalEmoji(signal) {
        // Signal state is conveyed via CSS [data-signal] styling — no emoji needed
        return '';
    }

    /**
     * Handle a file being selected (via drop or file picker).
     * Transitions the drop zone to its "file selected" state.
     * @param {File} file
     * @param {HTMLElement} dropZone
     * @param {HTMLButtonElement} analyseBtn
     */
    _handleFileSelected(file, dropZone, analyseBtn) {
        this._mediaFile = file;
        dropZone.classList.add('has-file');
        document.getElementById('ma-selected-filename').textContent = file.name;
        analyseBtn.disabled = false;
    }

    /**
     * Enable/disable the Analyse button depending on which tab is active
     * and whether that tab has valid input.
     * @param {HTMLButtonElement} btn
     * @param {HTMLElement} dropZone
     * @param {HTMLInputElement} urlInput
     * @param {'upload'|'url'} activeTab
     */
    _updateAnalyseBtn(btn, dropZone, urlInput, activeTab) {
        btn.disabled = activeTab === 'upload'
            ? !dropZone.classList.contains('has-file')
            : urlInput.value.trim() === '';
    }

    /**
     * Render the static demo result panel (TEST_MODE only).
     * Reveals the pre-populated `#ma-result-section` without any API call,
     * so the full shell layout can be reviewed without a live backend.
     * @param {HTMLElement} resultSection
     * @param {HTMLElement} resultPlaceholder
     */
    _renderDemoResult(resultSection, resultPlaceholder) {
        resultPlaceholder.classList.add('hidden');
        resultSection.classList.remove('hidden');
        ui.showToast('🧪 Demo result rendered (TEST_MODE)', 'info');
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

    /**
     * Fetch and display the extension token modal.
     * Called from Settings button or ?ext=1 URL redirect.
     */
    async showExtensionTokenModal() {
        const tokenInput = document.getElementById('extension-token-value');
        if (tokenInput) tokenInput.value = 'Loading...';

        ui.showModal('extension-token-modal');

        try {
            const response = await api.getExtensionToken();
            if (response.success && tokenInput) {
                tokenInput.value = response.api_token;
            }
        } catch (error) {
            console.error('Failed to fetch extension token:', error);
            if (tokenInput) tokenInput.value = 'Error — please try again';
            ui.showToast('Failed to load extension token', 'error');
        }
    }

    /**
     * Copy the extension token to clipboard and give visual feedback.
     */
    async copyExtensionToken() {
        const tokenInput = document.getElementById('extension-token-value');
        const copyBtn = document.getElementById('copy-extension-token-btn');
        if (!tokenInput || !tokenInput.value || tokenInput.value === 'Loading...') return;

        try {
            await navigator.clipboard.writeText(tokenInput.value);
            if (copyBtn) {
                const original = copyBtn.innerHTML;
                copyBtn.innerHTML = '✅ Copied!';
                setTimeout(() => { copyBtn.innerHTML = original; }, 2000);
            }
            ui.showToast('Token copied to clipboard', 'success');
        } catch {
            // Fallback: select input text
            tokenInput.select();
            ui.showToast('Select and copy the token manually', 'info');
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
