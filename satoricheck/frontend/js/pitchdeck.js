/**
 * Pitch Deck Module
 * Handles view switching, PDF upload, and UI state for the Pitch Deck Analyst Workspace.
 * 
 * Privacy-first approach: Files are read client-side using FileReader API.
 * Data stays in browser memory and is never uploaded to the server for storage.
 */

import ui from './ui.js';
import api from './api.js';

class PitchdeckModule {
    constructor() {
        this.isActive = false;
        this.elements = {};

        // Uploaded file state (client-side only, ephemeral)
        this.uploadedFile = null;
        this.uploadedFileData = null;
    }

    /**
     * Initialize the module - wire up event listeners
     */
    init() {
        // Cache DOM elements
        this.elements = {
            navBtnFactcheck: document.getElementById('nav-factcheck-btn'),
            navBtnPitchdeck: document.getElementById('nav-pitchdeck-btn'),
            factcheckView: document.getElementById('factcheck-view'),
            pitchdeckView: document.getElementById('pitchdeck-view'),
            uploadZone: document.getElementById('pd-upload-zone'),
            fileInput: document.getElementById('pd-file-input'),
            uploadTitle: document.querySelector('.pd-upload-title'),
            uploadSubtitle: document.querySelector('.pd-upload-subtitle'),
            uploadedInfo: document.querySelector('.pd-uploaded-info'),
            uploadedFilename: document.querySelector('.pd-uploaded-filename'),
            generateBtn: document.getElementById('pd-generate-btn'),
            deepDiveBtn: document.getElementById('pd-deep-dive-btn')
        };

        // Check if pitchdeck elements exist
        if (!this.elements.pitchdeckView) {
            console.warn('[Pitchdeck] Module elements not found in DOM');
            return;
        }

        this.setupEventListeners();
    }

    /**
     * Set up event listeners for navigation and upload interactions
     */
    setupEventListeners() {
        // Navigation buttons
        this.elements.navBtnFactcheck?.addEventListener('click', () => this.hide());
        this.elements.navBtnPitchdeck?.addEventListener('click', () => this.show());

        // Upload zone - click to trigger file picker
        if (this.elements.uploadZone && this.elements.fileInput) {
            this.elements.uploadZone.addEventListener('click', () => {
                this.elements.fileInput.click();
            });

            // File input change handler
            this.elements.fileInput.addEventListener('change', (e) => {
                const file = e.target.files?.[0];
                if (file) {
                    this.handleFileUpload(file);
                }
            });

            // Drag and drop handlers
            this.elements.uploadZone.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this.elements.uploadZone.classList.add('dragover');
            });

            this.elements.uploadZone.addEventListener('dragleave', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this.elements.uploadZone.classList.remove('dragover');
            });

            this.elements.uploadZone.addEventListener('drop', (e) => {
                e.preventDefault();
                e.stopPropagation();
                this.elements.uploadZone.classList.remove('dragover');

                const file = e.dataTransfer?.files?.[0];
                if (file) {
                    this.handleFileUpload(file);
                }
            });
        }

        // Generate Overview button - triggers analysis
        this.elements.generateBtn?.addEventListener('click', () => {
            this.startAnalysis();
        });

        // Claims list event delegation
        const claimsList = document.getElementById('pd-claims-list');
        if (claimsList) {
            console.log('[Pitchdeck] Added event listener to claims list');
            claimsList.addEventListener('click', (e) => {
                console.log('[Pitchdeck] Click on claims list:', e.target);
                const btn = e.target.closest('.pd-claim-check-btn');
                if (btn) {
                    console.log('[Pitchdeck] Check button clicked:', btn);
                    if (!btn.disabled) {
                        const index = parseInt(btn.dataset.claimIndex, 10);
                        console.log('[Pitchdeck] Claim index:', index);
                        if (!isNaN(index)) {
                            this.verifySingleClaim(index);
                        } else {
                            console.error('[Pitchdeck] Invalid claim index');
                        }
                    } else {
                        console.log('[Pitchdeck] Button disabled');
                    }
                }
            });
        } else {
            console.error('[Pitchdeck] Claims list container not found during init');
        }

        // Check All Button
        const checkAllBtn = document.getElementById('pd-check-all-btn');
        if (checkAllBtn) {
            checkAllBtn.addEventListener('click', () => {
                this.checkAllClaims();
            });
        }
    }

    /**
     * Start the PDF analysis by calling the backend API.
     * Shows loading state and displays results when complete.
     */
    async startAnalysis() {
        if (!this.uploadedFileData) {
            ui.showToast('Please upload a PDF first', 'error');
            return;
        }

        // Get result container elements
        const summaryCard = document.getElementById('pd-summary-card');
        const summarySkeleton = document.getElementById('pd-summary-skeleton');
        const summaryLoading = document.getElementById('pd-summary-loading');
        const summaryResults = document.getElementById('pd-summary-results');
        const marketSkeleton = document.getElementById('pd-market-skeleton');
        const marketLoading = document.getElementById('pd-market-loading');
        const marketResults = document.getElementById('pd-market-results');

        try {
            // === SHOW LOADING STATE ===
            this.elements.generateBtn.disabled = true;
            this.elements.generateBtn.textContent = 'Analyzing...';
            this.elements.generateBtn.classList.add('analyzing');

            // Show toast — Pro + thinking mode on a PDF can take 60-120s on larger decks
            ui.showToast('🔍 Generating overview... Large decks may take up to 2 minutes.', 'info');

            // Hide skeletons, show loading spinners
            summarySkeleton?.classList.add('hidden');
            summaryLoading?.classList.remove('hidden');
            summaryResults?.classList.add('hidden');

            marketSkeleton?.classList.add('hidden');
            marketLoading?.classList.remove('hidden');
            marketResults?.classList.add('hidden');

            // === CONVERT FILE TO BASE64 ===
            const base64Data = this.arrayBufferToBase64(this.uploadedFileData);

            // === CALL API ===
            // Use AbortController for a 2-minute client-side timeout.
            // The backend itself allows up to 180s per attempt — without this the
            // browser can drop the connection before the server finishes.
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 120_000); // 2 min (90s backend + margin)

            let response;
            try {
                response = await fetch('/api/pitchdeck/analyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pdf_data: base64Data }),
                    signal: controller.signal
                });
            } catch (fetchError) {
                if (fetchError.name === 'AbortError') {
                    throw new Error('Analysis timed out. Your deck may be too large — try a smaller PDF.');
                }
                throw fetchError;
            } finally {
                clearTimeout(timeoutId);
            }

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({}));
                throw new Error(errorData.message || `Analysis failed (${response.status})`);
            }

            const result = await response.json();

            // === DISPLAY RESULTS ===
            this.displayResults(result);

            // Show cost toast if available
            if (result.success && result.cost_incurred) {
                ui.showToast(`Analysis complete`, 'success');
                // Refresh balance
                if (result.new_balance !== undefined) {
                    ui.updateBalance(result.new_balance);
                } else {
                    // Fallback: Fetch balance if not provided in response
                    try {
                        const balanceResponse = await api.getBalance();
                        if (balanceResponse && balanceResponse.balance !== undefined) {
                            ui.updateBalance(balanceResponse.balance);
                        }
                    } catch (e) {
                        console.warn('[Pitchdeck] Failed to refresh balance:', e);
                    }
                }
            }

            // Update button state
            this.elements.generateBtn.textContent = 'Overview Generated ✓';
            this.elements.generateBtn.classList.remove('analyzing');

        } catch (error) {
            console.error('[Pitchdeck] Analysis failed:', error);
            ui.showToast(error.message || 'Analysis failed. Please try again.', 'error');

            // Reset to skeleton state
            summarySkeleton?.classList.remove('hidden');
            summaryLoading?.classList.add('hidden');
            marketSkeleton?.classList.remove('hidden');
            marketLoading?.classList.add('hidden');

            // Reset button
            this.elements.generateBtn.disabled = false;
            this.elements.generateBtn.textContent = 'Generate Overview';
            this.elements.generateBtn.classList.remove('analyzing');
        }
    }

    /**
     * Display analysis results in the UI
     * @param {Object} result - Analysis result from API
     */
    displayResults(result) {
        const summaryLoading = document.getElementById('pd-summary-loading');
        const summaryResults = document.getElementById('pd-summary-results');
        const marketLoading = document.getElementById('pd-market-loading');
        const marketResults = document.getElementById('pd-market-results');

        // Hide loading, show results
        summaryLoading?.classList.add('hidden');
        summaryResults?.classList.remove('hidden');
        marketLoading?.classList.add('hidden');
        marketResults?.classList.remove('hidden');

        // Populate Summary & USP
        // Populate Summary & USP
        // Note: Using innerHTML because backend response is already HTML-escaped
        // This allows entities like &quot; to render correctly as "
        document.getElementById('pd-company-name').innerHTML = result.company_name || '—';
        document.getElementById('pd-summary-text').innerHTML = result.summary || '—';
        document.getElementById('pd-usp-text').innerHTML = result.usp || '—';

        // Populate Market & Competition
        document.getElementById('pd-industry').innerHTML = result.industry || '—';
        document.getElementById('pd-sector').innerHTML = result.sector || '—';
        document.getElementById('pd-market-size').innerHTML = result.market_size || 'Not specified';

        const competitionList = document.getElementById('pd-competition-list');
        competitionList.innerHTML = '';

        const competitors = result.competition || [];
        if (competitors.length > 0) {
            competitors.forEach(competitor => {
                const li = document.createElement('li');
                li.innerHTML = competitor; // Backend sanitized
                competitionList.appendChild(li);
            });
        } else {
            const li = document.createElement('li');
            li.textContent = 'Not specified';
            competitionList.appendChild(li);
        }

        // Populate VC Metrics
        const vcMetrics = result.vc_metrics || {};
        this._renderMetricsContainer('pd-metrics-summary', [
            { key: 'monthly_revenue_arr', label: 'Revenue / ARR', data: vcMetrics.monthly_revenue_arr },
            { key: 'burn_multiple', label: 'Burn Multiple', data: vcMetrics.burn_multiple },
            { key: 'nrr_percent', label: 'NRR', data: vcMetrics.nrr_percent },
        ]);
        this._renderMetricsContainer('pd-metrics-market', [
            { key: 'cac_payback_months', label: 'CAC Payback', data: vcMetrics.cac_payback_months },
            { key: 'ltv_cac_ratio', label: 'LTV:CAC', data: vcMetrics.ltv_cac_ratio },
            { key: 'runway_months', label: 'Runway', data: vcMetrics.runway_months },
        ]);

        // Store global context for claim verification
        this.globalDeckContext = {
            company: result.company_name || 'Unknown Company',
            industry: result.industry || 'Unknown Industry',
            sector: result.sector || 'Unknown Sector',
            summary: result.summary || 'No summary available.',
            cache_name: result.cache_name || null // Store Cache ID
        };

        // Display extracted claims (no auto-verification)
        this.displayClaims(result);
    }

    /**
     * Render a list of metric items into a container element.
     * @param {string} containerId - DOM id of the metrics container
     * @param {Array<{key: string, label: string, data: Object|null}>} metrics
     */
    _renderMetricsContainer(containerId, metrics) {
        const container = document.getElementById(containerId);
        if (!container) return;

        // Keep the header, clear dynamically added items
        const header = container.querySelector('.pd-metrics-header');
        container.innerHTML = '';
        if (header) container.appendChild(header);

        metrics.forEach(({ label, data }) => {
            container.appendChild(this._renderMetricItem(label, data));
        });
    }

    /**
     * Create a single metric item DOM element.
     * @param {string} label - Human-readable metric name
     * @param {Object|null} data - { value, assessment, detail } or null
     * @returns {HTMLElement}
     */
    _renderMetricItem(label, data) {
        const el = document.createElement('div');
        el.className = 'pd-metric-item';

        if (!data) {
            el.innerHTML = `
                <span class="pd-metric-label">${label}</span>
                <span class="pd-metric-value pd-metric-value--muted">—</span>
                <span class="pd-metric-badge pd-metric-badge--not-disclosed">Not Disclosed</span>
            `;
            return el;
        }

        const badgeClass = this._getMetricBadgeClass(data.assessment);

        el.innerHTML = `
            <span class="pd-metric-label">${label}</span>
            <span class="pd-metric-value">${data.value || '—'}</span>
            <span class="pd-metric-badge ${badgeClass}">${data.assessment || 'Not Disclosed'}</span>
            ${data.detail ? `<span class="pd-metric-detail">${data.detail}</span>` : ''}
        `;
        return el;
    }

    /**
     * Map assessment string to a CSS modifier class.
     * @param {string} assessment
     * @returns {string}
     */
    _getMetricBadgeClass(assessment) {
        const map = {
            'Elite': 'pd-metric-badge--elite',
            'Good': 'pd-metric-badge--good',
            'Caution': 'pd-metric-badge--caution',
            'Red Flag': 'pd-metric-badge--red-flag',
            'Not Disclosed': 'pd-metric-badge--not-disclosed',
            'Pre-Revenue': 'pd-metric-badge--pre-revenue',
        };
        return map[assessment] || 'pd-metric-badge--not-disclosed';
    }

    /**
     * Display extracted claims grouped by category with Check buttons
     * @param {Object} analysisResult - The analysis result containing claims
     */
    displayClaims(analysisResult) {
        const claimsPlaceholder = document.getElementById('pd-claims-placeholder');
        const claimsList = document.getElementById('pd-claims-list');
        const checkAllBtn = document.getElementById('pd-check-all-btn');

        // Get verifiable claims from result
        const claims = analysisResult.verifiable_claims || [];

        if (claims.length === 0) {
            claimsPlaceholder.innerHTML = '<p class="pd-placeholder-text">No verifiable claims found in deck</p>';
            checkAllBtn?.classList.add('hidden');
            return;
        }

        // Show claims list
        claimsPlaceholder?.classList.add('hidden');
        claimsList?.classList.remove('hidden');
        // checkAllBtn?.classList.remove('hidden'); 

        /**
         * NOTE: CHECK ALL DISABLED TEMPORARILY
         * Reason: The current client-side concurrency (Check All) triggers backend rate limits (Gemini API) 
         * and 503 errors because it spawns too many parallel "Smart Agent" requests.
         * 
         * Resolution Dependency: Requires implementation of a dedicated /api/factcheck/batch-analyze endpoint
         * to handle bulk claims in a single request.
         * 
         * See: PITCHDECK_IMPLEMENTATION_PLAN.md (Batch API section)
         */
        if (checkAllBtn) checkAllBtn.classList.add('hidden');

        claimsList.innerHTML = '';

        // Group claims by category
        const groupedClaims = {};
        claims.forEach((claim, index) => {
            const category = claim.category || 'other';
            if (!groupedClaims[category]) {
                groupedClaims[category] = [];
            }
            groupedClaims[category].push({ ...claim, originalIndex: index });
        });

        // Category display config
        const categoryConfig = {
            'market_size': { icon: '📊', label: 'Market Size' },
            'revenue': { icon: '💰', label: 'Revenue' },
            'growth_rate': { icon: '📈', label: 'Growth Rate' },
            'roi': { icon: '🎯', label: 'ROI' },
            'customer_count': { icon: '👥', label: 'Customers' },
            'cost_savings': { icon: '💡', label: 'Cost Savings' },
            'competitor': { icon: '🏢', label: 'Competition' },
            'technology': { icon: '⚡', label: 'Technology' },
            'other': { icon: '📌', label: 'Other' }
        };

        // Render grouped claims
        Object.entries(groupedClaims).forEach(([category, catClaims]) => {
            const config = categoryConfig[category] || categoryConfig['other'];

            const groupEl = document.createElement('div');
            groupEl.className = 'pd-claim-category-group';

            // Category header
            const headerEl = document.createElement('div');
            headerEl.className = 'pd-claim-category-header';
            headerEl.innerHTML = `${config.icon} ${config.label}`;
            groupEl.appendChild(headerEl);

            // Claims in this category
            const itemsEl = document.createElement('div');
            itemsEl.className = 'pd-claim-category-items';

            catClaims.forEach(claim => {
                const claimEl = document.createElement('div');
                claimEl.className = 'pd-claim-item';
                claimEl.id = `pd-claim-${claim.originalIndex}`;

                claimEl.innerHTML = `
                    <div class="pd-claim-header">
                        <div class="pd-claim-text">${claim.claim}</div>
                        <button class="pd-claim-check-btn" data-claim-index="${claim.originalIndex}" title="Fact-check this claim">
                            Check
                        </button>
                    </div>
                    ${claim.source_cited ? `<div class="pd-claim-source">Source: ${claim.source_cited}</div>` : ''}
                    <div class="pd-claim-result hidden" id="pd-claim-result-${claim.originalIndex}">
                        <!-- Verification result will be inserted here -->
                    </div>
                `;

                itemsEl.appendChild(claimEl);
            });

            groupEl.appendChild(itemsEl);
            claimsList.appendChild(groupEl);
        });

        // Store claims with originalIndex for verification
        this.extractedClaims = claims.map((claim, index) => ({
            ...claim,
            originalIndex: index
        }));

        console.log('[Pitchdeck] Displayed', claims.length, 'claims in', Object.keys(groupedClaims).length, 'categories');
    }

    /**
     * Verify all unverified claims with concurrency limit
     */
    async checkAllClaims() {
        const checkAllBtn = document.getElementById('pd-check-all-btn');
        if (checkAllBtn) {
            checkAllBtn.disabled = true;
            checkAllBtn.innerHTML = '<span class="pd-spinner" style="width:14px;height:14px;border-width:2px;display:inline-block;vertical-align:middle;margin-right:8px;"></span> Verifying...';
        }

        const unverifiedClaims = this.extractedClaims.filter(c => !c.verificationResult);
        if (unverifiedClaims.length === 0) {
            if (checkAllBtn) {
                checkAllBtn.disabled = false;
                checkAllBtn.textContent = 'Check All';
            }
            return;
        }

        const CONCURRENCY = 1; // Sequential execution to prevent 503 errors
        const queue = [...unverifiedClaims];
        const workers = [];

        const worker = async () => {
            while (queue.length > 0) {
                const claim = queue.shift();
                await this.verifySingleClaim(claim.originalIndex);
                // 1.5s delay to be gentle on the backend/API
                await new Promise(r => setTimeout(r, 1500));
            }
        };

        // Start workers
        for (let i = 0; i < Math.min(CONCURRENCY, unverifiedClaims.length); i++) {
            workers.push(worker());
        }

        await Promise.all(workers);

        if (checkAllBtn) {
            checkAllBtn.disabled = false;
            checkAllBtn.textContent = 'Check All Complete';
            // Reset after 3 seconds
            setTimeout(() => {
                if (checkAllBtn) checkAllBtn.textContent = 'Check All';
            }, 3000);
        }
        ui.showToast('Batch verification complete', 'success');
    }

    /**
     * Verify a single claim using the backend API
     * @param {number} claimIndex - Index of claim to verify
     */
    async verifySingleClaim(claimIndex) {
        const claim = this.extractedClaims.find(c => c.originalIndex === claimIndex);
        if (!claim) return;

        // UI Loading state
        const checkBtn = document.querySelector(`.pd-claim-check-btn[data-claim-index="${claimIndex}"]`);
        if (checkBtn) {
            checkBtn.disabled = true;
            checkBtn.innerHTML = '<span class="pd-spinner" style="width:14px;height:14px;border-width:2px;"></span> Checking...';
            checkBtn.classList.add('loading');
        }

        try {
            // New: Usage of Context Caching via specific endpoint
            // We use the batch endpoint '/verify-market' effectively as a single-claim verifier here
            // to leverage the backend caching logic we just added.

            const payload = {
                verifiable_claims: [{
                    claim: claim.claim,
                    category: claim.category,
                    source_cited: claim.source_cited,
                    context: claim.context
                }],
                industry: this.globalDeckContext?.industry,
                cache_name: this.globalDeckContext?.cache_name
            };

            const response = await fetch('/api/pitchdeck/verify-market', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(payload)
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.message || `Verification failed (${response.status})`);
            }

            const data = await response.json();

            if (data.success && data.findings && data.findings.length > 0) {
                // Store result
                const finding = data.findings[0];
                claim.verificationResult = finding;
                this.displayClaimResult(claimIndex, finding);
            } else {
                throw new Error('Invalid API response');
            }

            // Update button to verified state
            if (checkBtn) {
                checkBtn.disabled = false;
                checkBtn.innerHTML = 'Reprocess';
                checkBtn.classList.remove('loading');
                checkBtn.classList.add('verified');
            }

        } catch (error) {
            console.error('Claim verification failed:', error);
            if (checkBtn) {
                checkBtn.disabled = false;
                checkBtn.textContent = 'Retry';
                checkBtn.classList.remove('loading');
            }

            // Handle Server Busy / Rate Limit specifically
            if (error.status === 503 || error.status === 429 || (error.message && error.message.includes('temporarily unavailable'))) {
                ui.showToast('Server is busy. Please try again in a moment.', 'warning');
            } else {
                ui.showToast('Verification failed. Please try again.', 'error');
            }
        }
    }

    /**
     * Display verification result for a single claim
     * @param {number} claimIndex - Index of the claim
     * @param {Object} result - Verification result
     */
    displayClaimResult(claimIndex, result) {
        const resultEl = document.getElementById(`pd-claim-result-${claimIndex}`);
        if (!resultEl) return;

        const verdictClass = this.getVerdictClass(result.verdict);
        const verdictIcon = this.getVerdictIcon(result.verdict);

        let sourcesHtml = '';
        if (result.sources && result.sources.length > 0) {
            const sourceLinks = result.sources.map((src, i) => {
                const url = typeof src === 'string' ? src : (src.url || '#');
                const title = (typeof src === 'object' && src.title) ? src.title : (url.startsWith('http') ? new URL(url).hostname.replace('www.', '') : `Source ${i + 1}`);
                return `<a href="${url}" target="_blank" rel="noopener">${title}</a>`;
            }).join(' · ');
            sourcesHtml = `<div class="pd-claim-sources">Sources: ${sourceLinks}</div>`;
        }

        resultEl.innerHTML = `
            <div class="pd-claim-verdict">
                <span class="pd-verdict-badge ${verdictClass}">${verdictIcon} ${result.verdict}</span>
            </div>
            ${result.explanation ? `<div class="pd-claim-explanation">${result.explanation}</div>` : ''}
            ${sourcesHtml}
        `;
        resultEl.classList.remove('hidden');
    }

    /**
     * Get CSS class for verdict styling
     * @param {string} verdict
     * @returns {string}
     */
    getVerdictClass(verdict) {
        if (!verdict) return 'verdict-unverified';
        const v = String(verdict).toUpperCase();
        if (v === 'TRUE' || v === 'VERIFIED') return 'verdict-true';
        if (v === 'FALSE' || v === 'DISPUTED' || v === 'INCORRECT') return 'verdict-false';
        if (v === 'MISLEADING' || v === 'PARTIALLY TRUE') return 'verdict-misleading';
        return 'verdict-unverified';
    }

    /**
     * Get icon for verdict
     * @param {string} verdict
     * @returns {string}
     */
    getVerdictIcon(verdict) {
        if (!verdict) return '❓';
        const v = String(verdict).toUpperCase();
        if (v === 'TRUE' || v === 'VERIFIED') return '✅';
        if (v === 'FALSE' || v === 'DISPUTED' || v === 'INCORRECT') return '🚩';
        if (v === 'MISLEADING' || v === 'PARTIALLY TRUE') return '⚠️';
        return '❓';
    }

    /**
     * Convert ArrayBuffer to Base64 string
     * @param {ArrayBuffer} buffer
     * @returns {string}
     */
    arrayBufferToBase64(buffer) {
        let binary = '';
        const bytes = new Uint8Array(buffer);
        for (let i = 0; i < bytes.byteLength; i++) {
            binary += String.fromCharCode(bytes[i]);
        }
        return btoa(binary);
    }

    /**
     * Handle file upload - validates and reads the file into memory.
     * Privacy-first: Data stays in browser memory, never uploaded to server for storage.
     * @param {File} file - The uploaded file
     */
    handleFileUpload(file) {
        // Validate file type
        if (!this.isValidPdf(file)) {
            ui.showToast('Please upload a PDF file', 'error');
            this.resetUpload();
            return;
        }

        // Validate file is not empty
        if (file.size === 0) {
            ui.showToast('File is empty', 'error');
            this.resetUpload();
            return;
        }

        // Validate file size (max 25MB as per spec)
        const MAX_FILE_SIZE = 25 * 1024 * 1024; // 25MB
        if (file.size > MAX_FILE_SIZE) {
            ui.showToast('File too large. Maximum size is 25MB.', 'error');
            this.resetUpload();
            return;
        }

        // Read file into memory using FileReader (client-side only)
        const reader = new FileReader();

        reader.onload = (e) => {
            // Store file data in memory (ephemeral - discarded on session end)
            this.uploadedFile = file;
            this.uploadedFileData = e.target.result;

            // Update UI to show success state
            this.showUploadSuccess(file.name);
        };

        reader.onerror = () => {
            ui.showToast('Failed to read file', 'error');
            this.resetUpload();
        };

        // Read as ArrayBuffer (for later PDF processing)
        reader.readAsArrayBuffer(file);
    }

    /**
     * Validate that the file is a PDF
     * @param {File} file - The file to validate
     * @returns {boolean} True if valid PDF
     */
    isValidPdf(file) {
        // Check MIME type
        if (file.type === 'application/pdf') {
            return true;
        }

        // Fallback: Check file extension
        const name = file.name.toLowerCase();
        if (name.endsWith('.pdf')) {
            return true;
        }

        return false;
    }

    /**
     * Update UI to show upload success state
     * @param {string} filename - Name of the uploaded file
     */
    showUploadSuccess(filename) {
        const { uploadZone, uploadTitle, uploadSubtitle, uploadedInfo, uploadedFilename, generateBtn } = this.elements;

        // Add success class to upload zone
        uploadZone?.classList.add('uploaded');

        // Hide default text, show uploaded info
        uploadTitle?.classList.add('hidden');
        uploadSubtitle?.classList.add('hidden');
        uploadedInfo?.classList.remove('hidden');

        // Set filename
        if (uploadedFilename) {
            uploadedFilename.textContent = filename;
        }

        // Enable Generate Overview button
        if (generateBtn) {
            generateBtn.disabled = false;
        }
    }

    /**
     * Reset upload state (for error recovery or new upload)
     */
    resetUpload() {
        const { uploadZone, uploadTitle, uploadSubtitle, uploadedInfo, uploadedFilename, generateBtn, fileInput } = this.elements;

        // Clear state
        this.uploadedFile = null;
        this.uploadedFileData = null;

        // Reset UI
        uploadZone?.classList.remove('uploaded');
        uploadTitle?.classList.remove('hidden');
        uploadSubtitle?.classList.remove('hidden');
        uploadedInfo?.classList.add('hidden');

        if (uploadedFilename) {
            uploadedFilename.textContent = '';
        }

        if (generateBtn) {
            generateBtn.disabled = true;
        }

        // Clear file input (allows re-uploading same file)
        if (fileInput) {
            fileInput.value = '';
        }
    }

    /**
     * Get the currently uploaded file data
     * @returns {{file: File, data: ArrayBuffer} | null}
     */
    getUploadedFile() {
        if (this.uploadedFile && this.uploadedFileData) {
            return {
                file: this.uploadedFile,
                data: this.uploadedFileData
            };
        }
        return null;
    }

    /**
     * Show the Pitchdeck workspace, hide Factcheck view
     */
    show() {
        if (this.isActive) return;

        this.isActive = true;
        this.elements.factcheckView?.classList.add('hidden');
        this.elements.pitchdeckView?.classList.remove('hidden');
        this.elements.navBtnFactcheck?.classList.remove('active');
        this.elements.navBtnPitchdeck?.classList.add('active');

        console.log('[Pitchdeck] View activated');
    }

    /**
     * Hide the Pitchdeck workspace, show Factcheck view
     */
    hide() {
        if (!this.isActive) return;

        this.isActive = false;
        this.elements.pitchdeckView?.classList.add('hidden');
        this.elements.factcheckView?.classList.remove('hidden');
        this.elements.navBtnPitchdeck?.classList.remove('active');
        this.elements.navBtnFactcheck?.classList.add('active');

        console.log('[Pitchdeck] View deactivated');
    }
}

export default new PitchdeckModule();
