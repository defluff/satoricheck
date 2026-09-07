/**
 * Pitch Deck Module — Investor Workstation
 * 
 * Interactive due-diligence workstation for early-stage investors:
 * - Instant client-side PDF rendering (PDF.js) with 0ms visual delay.
 * - Top Executive Dashboard: Company Summary, VC Lens Scorecard, and Red Flags.
 * - Side-by-side Workstation: Crisp slide viewer canvas on the left, slide-mapped
 *   claims with on-demand verification on the right.
 * - Privacy-first: File remains in browser memory; never stored on disk.
 */

import ui from './ui.js';
import api from './api.js';

class PitchdeckModule {
    constructor() {
        this.isActive = false;
        this.elements = {};

        // Ephemeral uploaded file state
        this.uploadedFile = null;
        this.uploadedFileData = null;

        // PDF.js rendering state
        this.pdfDoc = null;
        this.currentSlide = 1;
        this.totalSlides = 1;
        this.zoomLevel = 1.0;
        this.renderTask = null;

        // Claims state
        this.activeFilter = 'slide'; // 'slide' | 'all' | 'financials'
        this.claimsBySlideMap = new Map();
        this.extractedClaims = [];
        this.globalDeckContext = null;
    }

    /**
     * Initialize the module and cache DOM elements
     */
    init() {
        this.elements = {
            // Views & Navigation
            navBtnFactcheck: document.getElementById('nav-factcheck-btn'),
            navBtnPitchdeck: document.getElementById('nav-pitchdeck-btn'),
            factcheckView: document.getElementById('factcheck-view'),
            pitchdeckView: document.getElementById('pitchdeck-view'),

            // Upload Zone
            uploadZone: document.getElementById('pd-upload-zone'),
            fileInput: document.getElementById('pd-file-input'),
            uploadTitle: document.querySelector('.pd-upload-title'),
            uploadSubtitle: document.querySelector('.pd-upload-subtitle'),
            uploadedInfo: document.querySelector('.pd-uploaded-info'),
            uploadedFilename: document.querySelector('.pd-uploaded-filename'),
            uploadedPagesBadge: document.getElementById('pd-uploaded-pages-badge'),
            generateBtn: document.getElementById('pd-generate-btn'),

            // Slide Viewer Controls
            prevSlideBtn: document.getElementById('pd-prev-slide-btn'),
            nextSlideBtn: document.getElementById('pd-next-slide-btn'),
            canvasPrevBtn: document.getElementById('pd-canvas-prev-btn'),
            canvasNextBtn: document.getElementById('pd-canvas-next-btn'),
            currentSlideNum: document.getElementById('pd-current-slide-num'),
            totalSlidesNum: document.getElementById('pd-total-slides-num'),
            zoomInBtn: document.getElementById('pd-zoom-in'),
            zoomOutBtn: document.getElementById('pd-zoom-out'),
            zoomFitBtn: document.getElementById('pd-zoom-fit'),
            zoomVal: document.getElementById('pd-zoom-val'),
            slideCanvas: document.getElementById('pd-slide-canvas'),
            canvasWrapper: document.getElementById('pd-canvas-wrapper'),
            canvasPlaceholder: document.getElementById('pd-canvas-placeholder'),

            // Claims Desk Controls
            activeSlideBadge: document.getElementById('pd-active-slide-badge'),
            slideClaimsCount: document.getElementById('pd-slide-claims-count'),
            checkSlideBtn: document.getElementById('pd-check-slide-btn'),
            checkAllBtn: document.getElementById('pd-check-all-btn'),
            filterTabs: document.getElementById('pd-claims-filter-tabs'),
            claimsCard: document.getElementById('pd-claims-card'),
            claimsHeaderTitle: document.getElementById('pd-claims-header-title'),
            claimsList: document.getElementById('pd-claims-list'),
            claimsPlaceholder: document.getElementById('pd-claims-placeholder')
        };

        if (!this.elements.pitchdeckView) {
            console.warn('[Pitchdeck] Pitchdeck view not found in DOM');
            return;
        }

        // Configure PDF.js worker if available
        if (window.pdfjsLib) {
            window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
        }

        this.setupEventListeners();
    }

    /**
     * Attach event listeners
     */
    setupEventListeners() {
        // Upload zone click & file select
        if (this.elements.uploadZone && this.elements.fileInput) {
            this.elements.uploadZone.addEventListener('click', () => {
                this.elements.fileInput.click();
            });

            this.elements.fileInput.addEventListener('change', (e) => {
                const file = e.target.files?.[0];
                if (file) this.handleFileUpload(file);
            });

            // Drag and drop
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
                if (file) this.handleFileUpload(file);
            });
        }

        // Generate Overview button
        this.elements.generateBtn?.addEventListener('click', () => {
            this.startAnalysis();
        });

        // Slide navigation buttons (Toolbar & On-Canvas)
        this.elements.prevSlideBtn?.addEventListener('click', () => {
            this.goToSlide(this.currentSlide - 1);
        });

        this.elements.nextSlideBtn?.addEventListener('click', () => {
            this.goToSlide(this.currentSlide + 1);
        });

        this.elements.canvasPrevBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.goToSlide(this.currentSlide - 1);
        });

        this.elements.canvasNextBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            this.goToSlide(this.currentSlide + 1);
        });

        // Zoom controls
        this.elements.zoomInBtn?.addEventListener('click', () => {
            this.setZoom(this.zoomLevel + 0.2);
        });

        this.elements.zoomOutBtn?.addEventListener('click', () => {
            this.setZoom(this.zoomLevel - 0.2);
        });

        this.elements.zoomFitBtn?.addEventListener('click', () => {
            this.setZoom(1.0);
        });

        // Keyboard arrow navigation
        window.addEventListener('keydown', (e) => {
            if (!this.isActive || !this.pdfDoc) return;
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
            if (e.key === 'ArrowLeft') {
                this.goToSlide(this.currentSlide - 1);
            } else if (e.key === 'ArrowRight') {
                this.goToSlide(this.currentSlide + 1);
            }
        });

        // Claims filter tabs
        this.elements.filterTabs?.addEventListener('click', (e) => {
            const btn = e.target.closest('.pd-tab-btn');
            if (btn && btn.dataset.filter) {
                this.elements.filterTabs.querySelectorAll('.pd-tab-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.activeFilter = btn.dataset.filter;
                this.renderFilteredClaims();
            }
        });

        // Verify Slide claims button
        this.elements.checkSlideBtn?.addEventListener('click', () => {
            this.verifyCurrentSlideClaims();
        });

        // Check all claims button
        this.elements.checkAllBtn?.addEventListener('click', () => {
            this.checkAllClaims();
        });

        // Claims list event delegation (Check claim button OR jump to slide)
        if (this.elements.claimsList) {
            this.elements.claimsList.addEventListener('click', async (e) => {
                const checkBtn = e.target.closest('.pd-claim-check-btn');
                if (checkBtn && !checkBtn.disabled) {
                    const index = parseInt(checkBtn.dataset.claimIndex, 10);
                    if (!isNaN(index)) {
                        this.verifySingleClaim(index);
                    }
                    return;
                }

                // If user clicks anywhere on a claim card or slide tag
                const card = e.target.closest('.pd-claim-card');
                if (card && card.dataset.slide) {
                    const slideNum = parseInt(card.dataset.slide, 10);
                    if (!isNaN(slideNum)) {
                        this.elements.claimsList.querySelectorAll('.pd-claim-card').forEach(c => c.classList.remove('selected-claim'));
                        card.classList.add('selected-claim');
                        await this.goToSlide(slideNum);
                    }
                }
            });
        }
    }

    /**
     * Ensure the PDF.js library script is loaded in window and worker configured
     */
    async ensurePdfJsLibLoaded() {
        if (window.pdfjsLib) {
            if (!window.pdfjsLib.GlobalWorkerOptions?.workerSrc) {
                window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
            }
            return true;
        }

        return new Promise((resolve) => {
            const script = document.createElement('script');
            script.src = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.min.js';
            script.onload = () => {
                if (window.pdfjsLib) {
                    window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
                    resolve(true);
                } else {
                    resolve(false);
                }
            };
            script.onerror = () => {
                const fallbackScript = document.createElement('script');
                fallbackScript.src = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
                fallbackScript.onload = () => {
                    if (window.pdfjsLib) {
                        window.pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
                        resolve(true);
                    } else {
                        resolve(false);
                    }
                };
                fallbackScript.onerror = () => resolve(false);
                document.head.appendChild(fallbackScript);
            };
            document.head.appendChild(script);
        });
    }

    /**
     * Ensure PDF.js is ready and the uploaded PDF is parsed into this.pdfDoc
     */
    async ensurePdfLoaded() {
        if (this.pdfDoc) return true;

        const libReady = await this.ensurePdfJsLibLoaded();
        if (!libReady || !window.pdfjsLib) {
            console.error('[Pitchdeck] PDF.js library could not be loaded');
            return false;
        }

        try {
            let dataBuffer = null;
            if (this.uploadedFile) {
                dataBuffer = await this.uploadedFile.arrayBuffer();
            } else if (this.uploadedFileData) {
                const binaryStr = atob(this.uploadedFileData);
                const bytes = new Uint8Array(binaryStr.length);
                for (let i = 0; i < binaryStr.length; i++) {
                    bytes[i] = binaryStr.charCodeAt(i);
                }
                dataBuffer = bytes;
            }

            if (!dataBuffer) return false;

            const loadingTask = window.pdfjsLib.getDocument({
                data: dataBuffer,
                isEvalSupported: false,
                enableScripting: false
            });

            this.pdfDoc = await loadingTask.promise;
            this.totalSlides = this.pdfDoc.numPages;

            if (this.elements.totalSlidesNum) {
                this.elements.totalSlidesNum.textContent = this.totalSlides;
            }
            if (this.elements.uploadedPagesBadge) {
                this.elements.uploadedPagesBadge.textContent = `${this.totalSlides} slides`;
                this.elements.uploadedPagesBadge.classList.remove('hidden');
            }
            return true;
        } catch (err) {
            console.error('[Pitchdeck] Failed to initialize PDF document:', err);
            return false;
        }
    }

    /**
     * File upload handler — loads PDF into local PDF.js and reads base64 for backend.
     * @param {File} file
     */
    async handleFileUpload(file) {
        if (!this.isValidPdf(file)) {
            ui.showToast('Please upload a PDF file', 'error');
            this.resetUpload();
            return;
        }

        if (file.size === 0) {
            ui.showToast('File is empty', 'error');
            this.resetUpload();
            return;
        }

        const MAX_FILE_SIZE = 25 * 1024 * 1024;
        if (file.size > MAX_FILE_SIZE) {
            ui.showToast('File too large. Maximum size is 25MB.', 'error');
            this.resetUpload();
            return;
        }

        this.uploadedFile = file;

        // 1. Read Base64 for backend in background
        const reader = new FileReader();
        reader.onload = (e) => {
            this.uploadedFileData = e.target.result.split(',')[1];
            this.showUploadSuccess(file.name);
        };
        reader.onerror = () => {
            ui.showToast('Failed to read file', 'error');
            this.resetUpload();
        };
        reader.readAsDataURL(file);

        // 2. Load PDF into client-side PDF.js for instant slide viewing
        try {
            const loaded = await this.ensurePdfLoaded();
            if (loaded && this.pdfDoc) {
                this.currentSlide = 1;
                this.zoomLevel = 1.0;
                await this.renderSlide(1);
                ui.showToast(`Loaded ${this.totalSlides} slides. Click "Analyze Deck with AI" to extract intelligence.`, 'info');
            } else {
                ui.showToast('Could not preview PDF locally, but AI analysis is still available.', 'warning');
            }
        } catch (err) {
            console.error('[Pitchdeck] Local PDF preview error:', err);
            ui.showToast('Could not preview PDF locally, but AI analysis is still available.', 'warning');
        }
    }

    /**
     * Navigate to specific slide number
     * @param {number} pageNumber
     */
    async goToSlide(pageNumber) {
        await this.ensurePdfLoaded();
        if (!this.pdfDoc || pageNumber < 1 || pageNumber > this.totalSlides) return;
        await this.renderSlide(pageNumber);
    }

    /**
     * Set zoom level
     * @param {number} level
     */
    setZoom(level) {
        this.zoomLevel = Math.max(0.5, Math.min(2.5, Math.round(level * 10) / 10));
        if (this.elements.zoomVal) {
            this.elements.zoomVal.textContent = `${Math.round(this.zoomLevel * 100)}%`;
        }
        if (this.pdfDoc) {
            this.renderSlide(this.currentSlide);
        }
    }

    /**
     * Render a slide canvas with Retina / HiDPI crispness
     * @param {number} pageNumber
     */
    async renderSlide(pageNumber) {
        await this.ensurePdfLoaded();
        if (!this.pdfDoc) return;
        this.currentSlide = Math.max(1, Math.min(this.totalSlides, pageNumber));

        // Update toolbar and canvas indicators
        if (this.elements.currentSlideNum) this.elements.currentSlideNum.textContent = this.currentSlide;
        if (this.elements.activeSlideBadge) this.elements.activeSlideBadge.textContent = this.currentSlide;
        if (this.elements.prevSlideBtn) this.elements.prevSlideBtn.disabled = (this.currentSlide <= 1);
        if (this.elements.nextSlideBtn) this.elements.nextSlideBtn.disabled = (this.currentSlide >= this.totalSlides);
        if (this.elements.canvasPrevBtn) {
            this.elements.canvasPrevBtn.disabled = (this.currentSlide <= 1);
            this.elements.canvasPrevBtn.classList.remove('hidden');
        }
        if (this.elements.canvasNextBtn) {
            this.elements.canvasNextBtn.disabled = (this.currentSlide >= this.totalSlides);
            this.elements.canvasNextBtn.classList.remove('hidden');
        }

        try {
            const page = await this.pdfDoc.getPage(this.currentSlide);
            const canvas = this.elements.slideCanvas;
            if (!canvas) return;

            const ctx = canvas.getContext('2d');
            const wrapper = this.elements.canvasWrapper;
            const containerWidth = (wrapper?.clientWidth || 700) - 32;

            const baseViewport = page.getViewport({ scale: 1.0 });
            const fitScale = (containerWidth / baseViewport.width) * this.zoomLevel;
            const viewport = page.getViewport({ scale: Math.max(0.4, fitScale) });

            const dpr = window.devicePixelRatio || 1;
            canvas.width = Math.floor(viewport.width * dpr);
            canvas.height = Math.floor(viewport.height * dpr);
            canvas.style.width = `${Math.floor(viewport.width)}px`;
            canvas.style.height = `${Math.floor(viewport.height)}px`;

            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

            if (this.renderTask) {
                this.renderTask.cancel();
            }

            this.renderTask = page.render({
                canvasContext: ctx,
                viewport: viewport
            });

            await this.renderTask.promise;

            // Reveal canvas, hide placeholder
            this.elements.canvasPlaceholder?.classList.add('hidden');
            canvas.classList.remove('hidden');

            // Refresh claims panel if in slide-specific mode, else highlight active slide cards
            if (this.activeFilter === 'slide') {
                this.renderFilteredClaims();
            } else {
                this.highlightClaimsForSlide(this.currentSlide);
            }

        } catch (err) {
            if (err.name !== 'RenderingCancelledException') {
                console.error('[Pitchdeck] Slide rendering failed:', err);
            }
        }
    }

    /**
     * Highlight claims matching the current slide in multi-claim views
     * @param {number} slideNum
     */
    highlightClaimsForSlide(slideNum) {
        if (!this.elements.claimsList) return;
        const cards = this.elements.claimsList.querySelectorAll('.pd-claim-card');
        cards.forEach(card => {
            const cardSlide = parseInt(card.dataset.slide, 10);
            if (cardSlide === slideNum) {
                card.classList.add('slide-active');
            } else {
                card.classList.remove('slide-active');
            }
        });
    }

    /**
     * Start backend pitch deck analysis
     */
    async startAnalysis() {
        if (!this.uploadedFileData) {
            ui.showToast('Please upload a PDF first', 'error');
            return;
        }

        const summarySkeleton = document.getElementById('pd-summary-skeleton');
        const summaryLoading = document.getElementById('pd-summary-loading');
        const summaryResults = document.getElementById('pd-summary-results');
        const marketSkeleton = document.getElementById('pd-market-skeleton');
        const marketLoading = document.getElementById('pd-market-loading');
        const marketResults = document.getElementById('pd-market-results');

        let timeoutId;

        try {
            this.elements.generateBtn.disabled = true;
            this.elements.generateBtn.textContent = 'Analyzing Deck...';
            this.elements.generateBtn.classList.add('analyzing');

            ui.showToast('Analyzing file... (~5-8s)', 'info');

            summarySkeleton?.classList.add('hidden');
            summaryLoading?.classList.remove('hidden');
            summaryResults?.classList.add('hidden');

            marketSkeleton?.classList.add('hidden');
            marketLoading?.classList.remove('hidden');
            marketResults?.classList.add('hidden');

            const controller = new AbortController();
            timeoutId = setTimeout(() => controller.abort(), 120_000);

            const result = await api.analyzePitchDeck(this.uploadedFileData, controller.signal);

            await this.displayResults(result);

            if (result.success && result.cost_incurred) {
                ui.showToast(`Analysis complete (${result.cost_incurred} CP)`, 'success');
                if (result.new_balance !== undefined) {
                    ui.updateBalance(result.new_balance);
                } else {
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

            this.elements.generateBtn.textContent = 'Analysis Complete ✓';
            this.elements.generateBtn.classList.remove('analyzing');

        } catch (error) {
            if (error.name === 'AbortError') {
                ui.showToast('Analysis timed out. Try a smaller deck.', 'error');
            } else {
                console.error('[Pitchdeck] Analysis failed:', error);
                ui.showToast(error.message || 'Analysis failed. Please try again.', 'error');
            }

            summarySkeleton?.classList.remove('hidden');
            summaryLoading?.classList.add('hidden');
            marketSkeleton?.classList.remove('hidden');
            marketLoading?.classList.add('hidden');

            this.elements.generateBtn.disabled = false;
            this.elements.generateBtn.textContent = '⚡ Analyze Deck with AI';
            this.elements.generateBtn.classList.remove('analyzing');
        } finally {
            clearTimeout(timeoutId);
        }
    }

    /**
     * Display analysis results in top dashboard and populate claims map
     * @param {Object} result
     */
    async displayResults(result) {
        const summaryLoading = document.getElementById('pd-summary-loading');
        const summaryResults = document.getElementById('pd-summary-results');
        const marketLoading = document.getElementById('pd-market-loading');
        const marketResults = document.getElementById('pd-market-results');

        summaryLoading?.classList.add('hidden');
        summaryResults?.classList.remove('hidden');
        marketLoading?.classList.add('hidden');
        marketResults?.classList.remove('hidden');

        // Ensure PDF is loaded and active slide is rendered
        await this.ensurePdfLoaded();
        if (this.pdfDoc) {
            await this.renderSlide(this.currentSlide || 1);
        }

        // Company & Summary
        document.getElementById('pd-company-name').innerHTML = DOMPurify.sanitize(result.company_name || '—');
        document.getElementById('pd-summary-text').innerHTML = DOMPurify.sanitize(result.summary || '—');
        document.getElementById('pd-usp-text').innerHTML = DOMPurify.sanitize(result.usp || '—');

        // Market & Industry
        document.getElementById('pd-industry').innerHTML = DOMPurify.sanitize(result.industry || '—');
        const sectorEl = document.getElementById('pd-sector');
        const separatorEl = sectorEl?.previousElementSibling;
        const sectorValue = (result.sector || '').trim();
        const hasSector = sectorValue && sectorValue !== '.' && sectorValue !== '—';
        if (sectorEl) sectorEl.innerHTML = hasSector ? DOMPurify.sanitize(sectorValue) : '';
        if (separatorEl?.classList.contains('pd-separator')) {
            separatorEl.style.display = hasSector ? '' : 'none';
        }
        document.getElementById('pd-market-size').innerHTML = DOMPurify.sanitize(result.market_size || 'Not specified');

        // Competition
        const competitionList = document.getElementById('pd-competition-list');
        if (competitionList) {
            competitionList.innerHTML = '';
            const competitors = result.competition || [];
            if (competitors.length > 0) {
                competitors.forEach(comp => {
                    const li = document.createElement('li');
                    li.textContent = comp;
                    competitionList.appendChild(li);
                });
            } else {
                const li = document.createElement('li');
                li.textContent = 'Not specified';
                competitionList.appendChild(li);
            }
        }

        // VC Metrics Lens
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

        // Red Flags
        this._displayRedFlags(result);

        // Global Context for Claim Verification
        this.globalDeckContext = {
            company: result.company_name || 'Unknown Company',
            industry: result.industry || 'Unknown Industry',
            sector: result.sector || 'Unknown Sector',
            summary: result.summary || 'No summary available.',
            cache_name: result.cache_name || null
        };

        // Index claims by slide_number
        this.claimsBySlideMap = new Map();
        const rawClaims = result.verifiable_claims || [];
        this.extractedClaims = rawClaims.map((claim, index) => ({
            ...claim,
            originalIndex: index,
            slide_number: typeof claim.slide_number === 'number' ? claim.slide_number : 1
        }));

        this.extractedClaims.forEach(claim => {
            const slide = claim.slide_number;
            if (!this.claimsBySlideMap.has(slide)) {
                this.claimsBySlideMap.set(slide, []);
            }
            this.claimsBySlideMap.get(slide).push(claim);
        });

        // Show workstation buttons
        this.elements.checkSlideBtn?.classList.remove('hidden');
        this.elements.checkAllBtn?.classList.remove('hidden');

        // Render claims for current slide
        this.renderFilteredClaims();
    }

    /**
     * Render filtered claims into the right panel
     */
    renderFilteredClaims() {
        const list = this.elements.claimsList;
        const placeholder = this.elements.claimsPlaceholder;
        const countBadge = this.elements.slideClaimsCount;
        if (!list) return;

        // Update dynamic claims desk title
        const titleEl = this.elements.claimsHeaderTitle || document.getElementById('pd-claims-header-title');
        if (titleEl) {
            if (this.activeFilter === 'slide') {
                titleEl.innerHTML = `Claims for Slide <span id="pd-active-slide-badge">${this.currentSlide}</span>`;
            } else if (this.activeFilter === 'financials') {
                titleEl.innerHTML = `Financials & Traction`;
            } else {
                titleEl.innerHTML = `All Deck Claims`;
            }
        }

        if (this.extractedClaims.length === 0) {
            placeholder?.classList.remove('hidden');
            list?.classList.add('hidden');
            if (countBadge) countBadge.textContent = '0';
            return;
        }

        placeholder?.classList.add('hidden');
        list?.classList.remove('hidden');

        // Determine active subset
        let claims = [];
        if (this.activeFilter === 'slide') {
            claims = this.claimsBySlideMap.get(this.currentSlide) || [];
        } else if (this.activeFilter === 'financials') {
            const financialCats = new Set(['revenue', 'growth_rate', 'roi', 'cost_savings', 'customer_count']);
            claims = this.extractedClaims.filter(c => financialCats.has(c.category) || c.is_quantitative);
        } else {
            claims = this.extractedClaims;
        }

        if (countBadge) {
            countBadge.textContent = String(claims.length);
        }

        list.innerHTML = '';

        if (claims.length === 0) {
            list.innerHTML = `
                <div class="pd-claims-empty-state" style="text-align: center; padding: 2rem 1rem; color: var(--color-text-muted);">
                    <div style="font-size: 1.2rem; margin-bottom: 0.5rem; opacity: 0.5;">—</div>
                    <p style="margin: 0; font-size: 0.9rem;">No specific claims extracted for Slide ${this.currentSlide}.</p>
                    <p style="margin: 0.25rem 0 0; font-size: 0.8rem; opacity: 0.8;">Navigate slides or switch tab to "All Deck Claims".</p>
                </div>
            `;
            return;
        }

        claims.forEach(claim => {
            const card = document.createElement('div');
            card.className = 'pd-claim-card';
            card.id = `pd-claim-${claim.originalIndex}`;
            card.dataset.slide = claim.slide_number || 1;
            card.dataset.claimIndex = claim.originalIndex;
            card.title = `Click to jump to Slide ${claim.slide_number || 1}`;

            if (claim.slide_number === this.currentSlide) {
                card.classList.add('slide-active');
            }

            const catLabel = (claim.category || 'other').replace('_', ' ').toUpperCase();
            const isVerified = Boolean(claim.verificationResult);

            card.innerHTML = DOMPurify.sanitize(`
                <div class="pd-claim-meta">
                    <div class="pd-claim-badge-group">
                        <span class="badge badge-caution">${catLabel}</span>
                        ${claim.slide_number ? `<span class="badge badge-tag pd-slide-tag" data-slide="${claim.slide_number}" title="Jump to Slide ${claim.slide_number}">Slide ${claim.slide_number}</span>` : ''}
                    </div>
                    <button class="btn-primary btn-sm pd-claim-check-btn ${isVerified ? 'verified' : ''}" data-claim-index="${claim.originalIndex}">
                        ${isVerified ? 'Verified ✓' : 'Verify Claim'}
                    </button>
                </div>
                <div class="pd-claim-text">
                    ${claim.claim}
                </div>
                ${claim.source_cited ? `<div class="pd-claim-source">Source cited: <em>${claim.source_cited}</em></div>` : ''}
                <div class="pd-claim-result ${isVerified ? '' : 'hidden'}" id="pd-claim-result-${claim.originalIndex}"></div>
            `);

            list.appendChild(card);

            if (isVerified) {
                this.displayClaimResult(claim.originalIndex, claim.verificationResult);
            }
        });
    }

    /**
     * Render metrics into a container
     */
    _renderMetricsContainer(containerId, metrics) {
        const container = document.getElementById(containerId);
        if (!container) return;

        const header = container.querySelector('.pd-metrics-header');
        container.innerHTML = '';
        if (header) container.appendChild(header);

        metrics.forEach(({ label, data }) => {
            const el = document.createElement('div');
            el.className = 'pd-metric-item';

            if (!data) {
                el.innerHTML = `
                    <span class="pd-metric-label">${label}</span>
                    <span class="pd-metric-value pd-metric-value--muted">—</span>
                    <span class="badge badge-neutral">Not Disclosed</span>
                `;
            } else {
                const badgeClass = this._getMetricBadgeClass(data.assessment);
                el.innerHTML = DOMPurify.sanitize(`
                    <span class="pd-metric-label">${label}</span>
                    <span class="pd-metric-value">${data.value || '—'}</span>
                    <span class="badge ${badgeClass}">${data.assessment || 'Not Disclosed'}</span>
                    ${data.detail ? `<span class="pd-metric-detail">${data.detail}</span>` : ''}
                `);
            }
            container.appendChild(el);
        });
    }

    _getMetricBadgeClass(assessment) {
        const map = {
            'Elite': 'badge-good',
            'Good': 'badge-good',
            'Caution': 'badge-caution',
            'Red Flag': 'badge-critical',
            'Not Disclosed': 'badge-neutral',
            'Pre-Revenue': 'badge-purple',
        };
        return map[assessment] || 'badge-neutral';
    }

    _displayRedFlags(analysisResult) {
        const container = document.getElementById('pd-red-flags');
        const list = document.getElementById('pd-red-flags-list');
        if (!container || !list) return;

        const flags = analysisResult.red_flags;
        if (!Array.isArray(flags) || flags.length === 0) {
            container.classList.add('hidden');
            return;
        }

        list.innerHTML = '';
        flags.forEach(flagText => {
            if (typeof flagText !== 'string' || !flagText.trim()) return;
            const li = document.createElement('li');
            const trimmed = flagText.trim();
            const isCritical = trimmed.toUpperCase().startsWith('CRITICAL');
            li.className = isCritical ? 'callout-item callout-item--critical' : 'callout-item';
            li.textContent = trimmed;
            list.appendChild(li);
        });
        container.classList.remove('hidden');
    }

    /**
     * Verify a single claim using cached deck context
     */
    async verifySingleClaim(claimIndex) {
        const claim = this.extractedClaims.find(c => c.originalIndex === claimIndex);
        if (!claim) return;

        const checkBtn = document.querySelector(`.pd-claim-check-btn[data-claim-index="${claimIndex}"]`);
        if (checkBtn) {
            checkBtn.disabled = true;
            checkBtn.innerHTML = '<span class="spinner" style="width:12px;height:12px;margin-right:6px;"></span> Checking...';
            checkBtn.classList.add('loading');
        }

        try {
            const payload = {
                company: this.globalDeckContext?.company,
                summary: this.globalDeckContext?.summary,
                industry: this.globalDeckContext?.industry,
                cache_name: this.globalDeckContext?.cache_name,
                verifiable_claims: [{
                    claim: claim.claim,
                    category: claim.category,
                    source_cited: claim.source_cited,
                    context: claim.context
                }]
            };

            const data = await api.verifyMarketClaims(payload);

            if (data.success && data.findings && data.findings.length > 0) {
                const finding = data.findings[0];
                claim.verificationResult = finding;
                this.displayClaimResult(claimIndex, finding);
            } else {
                throw new Error('Verification returned no findings');
            }

            if (checkBtn) {
                checkBtn.disabled = false;
                checkBtn.innerHTML = 'Verified ✓';
                checkBtn.classList.remove('loading');
                checkBtn.classList.add('verified');
            }

            if (data.new_balance !== undefined) {
                ui.updateBalance(data.new_balance);
            }

        } catch (error) {
            console.error('[Pitchdeck] Claim verification error:', error);
            if (checkBtn) {
                checkBtn.disabled = false;
                checkBtn.textContent = 'Retry';
                checkBtn.classList.remove('loading');
            }
            ui.showToast(error.message || 'Verification failed. Please try again.', 'error');
        }
    }

    /**
     * Verify all unverified claims on the current slide in batch
     */
    async verifyCurrentSlideClaims() {
        const slideClaims = (this.claimsBySlideMap.get(this.currentSlide) || []).filter(c => !c.verificationResult);
        if (slideClaims.length === 0) {
            ui.showToast('All claims on this slide are already verified.', 'info');
            return;
        }

        const btn = this.elements.checkSlideBtn;
        if (btn) {
            btn.disabled = true;
            btn.textContent = `Verifying ${slideClaims.length} Claims...`;
        }

        for (const claim of slideClaims) {
            await this.verifySingleClaim(claim.originalIndex);
            await new Promise(r => setTimeout(r, 600));
        }

        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Verify Slide';
        }
        ui.showToast(`Slide ${this.currentSlide} claims verification complete!`, 'success');
    }

    /**
     * Verify all claims in the deck sequentially
     */
    async checkAllClaims() {
        const unverified = this.extractedClaims.filter(c => !c.verificationResult);
        if (unverified.length === 0) {
            ui.showToast('All deck claims are verified.', 'info');
            return;
        }

        const btn = this.elements.checkAllBtn;
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Verifying Deck...';
        }

        for (const claim of unverified) {
            await this.verifySingleClaim(claim.originalIndex);
            await new Promise(r => setTimeout(r, 1000));
        }

        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Check All Complete';
            setTimeout(() => {
                if (btn) btn.textContent = 'Check All';
            }, 3000);
        }
        ui.showToast('Full deck verification complete!', 'success');
    }

    /**
     * Display single claim verification finding
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

        resultEl.innerHTML = DOMPurify.sanitize(`
            <div class="pd-claim-verdict">
                <span class="badge pd-verdict-badge ${verdictClass}">
                    ${verdictIcon} ${result.verdict}
                </span>
            </div>
            ${result.explanation ? `<div class="pd-claim-explanation">${result.explanation}</div>` : ''}
            ${sourcesHtml}
        `);
        resultEl.classList.remove('hidden');
    }

    getVerdictClass(verdict) {
        if (!verdict) return 'verdict-unverified';
        const v = String(verdict).toUpperCase();
        if (v === 'TRUE' || v === 'VERIFIED') return 'verdict-true';
        if (v === 'FALSE' || v === 'DISPUTED' || v === 'INCORRECT') return 'verdict-false';
        if (v === 'MISLEADING' || v === 'PARTIALLY TRUE') return 'verdict-misleading';
        return 'verdict-unverified';
    }

    getVerdictIcon(verdict) {
        if (!verdict) return '❓';
        const v = String(verdict).toUpperCase();
        if (v === 'TRUE' || v === 'VERIFIED') return '✅';
        if (v === 'FALSE' || v === 'DISPUTED' || v === 'INCORRECT') return '🚩';
        if (v === 'MISLEADING' || v === 'PARTIALLY TRUE') return '⚠️';
        return '❓';
    }

    isValidPdf(file) {
        if (file.type === 'application/pdf') return true;
        const name = file.name.toLowerCase();
        return name.endsWith('.pdf');
    }

    showUploadSuccess(filename) {
        const { uploadZone, uploadTitle, uploadSubtitle, uploadedInfo, uploadedFilename, generateBtn } = this.elements;
        uploadZone?.classList.add('uploaded');
        uploadTitle?.classList.add('hidden');
        uploadSubtitle?.classList.add('hidden');
        uploadedInfo?.classList.remove('hidden');
        if (uploadedFilename) uploadedFilename.textContent = filename;
        if (generateBtn) generateBtn.disabled = false;
    }

    resetUpload() {
        const { uploadZone, uploadTitle, uploadSubtitle, uploadedInfo, uploadedFilename, generateBtn, fileInput, uploadedPagesBadge, slideCanvas, canvasPlaceholder } = this.elements;
        this.uploadedFile = null;
        this.uploadedFileData = null;
        if (this.renderTask) {
            this.renderTask.cancel();
            this.renderTask = null;
        }
        this.pdfDoc = null;
        this.currentSlide = 1;
        this.totalSlides = 1;
        this.extractedClaims = [];
        this.claimsBySlideMap.clear();

        uploadZone?.classList.remove('uploaded');
        uploadTitle?.classList.remove('hidden');
        uploadSubtitle?.classList.remove('hidden');
        uploadedInfo?.classList.add('hidden');

        slideCanvas?.classList.add('hidden');
        canvasPlaceholder?.classList.remove('hidden');

        if (uploadedFilename) uploadedFilename.textContent = '';
        if (uploadedPagesBadge) {
            uploadedPagesBadge.textContent = '';
            uploadedPagesBadge.classList.add('hidden');
        }
        if (generateBtn) generateBtn.disabled = true;
        if (fileInput) fileInput.value = '';
    }

    show() {
        this.isActive = true;
        console.log('[Pitchdeck] Workstation activated');
    }

    hide() {
        this.isActive = false;
        console.log('[Pitchdeck] Workstation deactivated');
    }
}

export default new PitchdeckModule();
