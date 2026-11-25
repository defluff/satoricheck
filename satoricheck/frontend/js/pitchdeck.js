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
import storageSync from './storage-sync.js';

// Self-hosted PDF.js (see frontend/vendor/pdfjs-3.11.174/README.md for provenance)
const PDFJS_LIB_SRC = '/vendor/pdfjs-3.11.174/pdf.min.js';
const PDFJS_WORKER_SRC = '/vendor/pdfjs-3.11.174/pdf.worker.min.js';

const THESIS_STORAGE_KEY = 'authenix_pd_thesis_v1';
const ARCHIVE_STORAGE_KEY = 'authenix_pd_archive_v1';
const PENDING_STORAGE_ACTION_KEY = 'authenix_pending_storage_action_v1';
const MAX_ARCHIVED_DEALS = 100;

const DECISION_LABELS = {
    advance_to_call: 'Advance to Call',
    pass: 'Pass',
    watchlist: 'Watchlist',
    pending: 'Pending'
};

const DEFAULT_THESIS_OPTIONS = {
    geographies: ['DACH', 'EU', 'UK', 'US', 'Global'],
    sectors: ['B2B SaaS', 'FinTech', 'HealthTech', 'DeepTech', 'AI / ML', 'CleanTech', 'Consumer'],
    stages: ['Pre-Seed', 'Seed', 'Series A', 'Series B+']
};

const REGION_SYNONYMS = {
    'dach': ['dach', 'switzerland', 'swiss', 'zurich', 'zürich', 'geneva', 'basel', 'lausanne', 'zug', 'bern', 'germany', 'german', 'berlin', 'munich', 'münchen', 'frankfurt', 'hamburg', 'austria', 'austrian', 'vienna', 'wien'],
    'eu': ['eu', 'europe', 'european', 'emea', 'dach', 'switzerland', 'germany', 'austria', 'france', 'paris', 'spain', 'madrid', 'barcelona', 'italy', 'milan', 'netherlands', 'amsterdam', 'sweden', 'stockholm', 'nordics', 'denmark', 'copenhagen', 'finland', 'helsinki', 'norway', 'oslo', 'poland', 'warsaw', 'belgium', 'brussels', 'ireland', 'dublin', 'portugal', 'lisbon', 'estonia', 'tallinn'],
    'uk': ['uk', 'united kingdom', 'britain', 'great britain', 'british', 'england', 'london', 'scotland', 'edinburgh', 'cambridge', 'oxford', 'manchester'],
    'us': ['us', 'usa', 'united states', 'america', 'north america', 'san francisco', 'bay area', 'silicon valley', 'new york', 'nyc', 'boston', 'austin', 'seattle', 'los angeles', 'chicago', 'miami', 'delaware', 'california']
};

const SECTOR_SYNONYMS = {
    'b2b saas': ['b2b saas', 'saas', 'b2b', 'enterprise software', 'cloud software', 'workflow', 'devtools', 'cybersecurity', 'martech', 'hrtech', 'erp', 'crm'],
    'fintech': ['fintech', 'financial', 'payments', 'banking', 'insurtech', 'wealthtech', 'regtech', 'defi', 'crypto', 'blockchain', 'lending', 'neobank', 'treasury'],
    'healthtech': ['healthtech', 'digital health', 'medtech', 'medical', 'clinical', 'biotech', 'pharma', 'therapeutics', 'diagnostics', 'healthcare', 'patient', 'medication', 'care'],
    'deeptech': ['deeptech', 'deep tech', 'hardware', 'robotics', 'quantum', 'photonics', 'semiconductors', 'materials', 'aerospace', 'defense', 'spacetech', 'microfluidics', 'industrial'],
    'ai / ml': ['ai / ml', 'ai/ml', 'ai', 'artificial intelligence', 'machine learning', 'llm', 'genai', 'generative ai', 'computer vision', 'nlp', 'autonomous'],
    'cleantech': ['cleantech', 'clean tech', 'climatetech', 'climate', 'energy', 'renewable', 'solar', 'battery', 'grid', 'carbon', 'water', 'desalination', 'sustainability', 'circular', 'agtech'],
    'consumer': ['consumer', 'b2c', 'd2c', 'e-commerce', 'ecommerce', 'marketplace', 'retail', 'gaming', 'social', 'creator', 'edtech', 'foodtech', 'proptech']
};

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

        // Claims state (Defaults to Risk Priority View)
        this.activeFilter = 'risk'; // 'risk' | 'slide' | 'all' | 'financials'
        this.collapsedRiskBuckets = new Set(['low']);
        this.claimsBySlideMap = new Map();
        this.extractedClaims = [];
        this.globalDeckContext = null;

        // Thesis Alignment state
        this.thesisConfig = this.loadThesisConfig();
        this.lastAnalysisResult = null;
        this.currentThesisAlignment = null;

        // Feature C: Deal Decision & Zero-File Archive state
        this.currentAnalysisId = null;
        this.currentAnalyzedAt = null;
        this.currentDecision = 'pending';
        this.currentNotes = '';
        this.archivedDeals = this.loadArchivedDeals();
        this.priorBoomerangDeal = null;
        this.lastExportedDocUrl = null;
        this.batchQueue = [];
        this.isBatchRunning = false;
        this.sessionBatchFiles = new Map();
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

            // Upload Zone & Batch Check Controls
            uploadZone: document.getElementById('pd-upload-zone'),
            fileInput: document.getElementById('pd-file-input'),
            folderInput: document.getElementById('pd-folder-input'),
            localFolderBtn: document.getElementById('pd-local-folder-btn'),
            localFolderConnectedBtn: document.getElementById('pd-local-folder-connected-btn'),
            batchProgressBar: document.getElementById('pd-batch-progress-bar'),
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
            verifyPublicBtn: document.getElementById('pd-verify-public-btn'),
            checkAllBtn: document.getElementById('pd-check-all-btn'),
            filterTabs: document.getElementById('pd-claims-filter-tabs'),
            claimsCard: document.getElementById('pd-claims-card'),
            claimsHeaderTitle: document.getElementById('pd-claims-header-title'),
            claimsList: document.getElementById('pd-claims-list'),
            claimsPlaceholder: document.getElementById('pd-claims-placeholder'),

            // Feature A: Thesis Configuration & Scorecard
            thesisGate: document.getElementById('pd-thesis-gate'),
            thesisStatusDot: document.getElementById('pd-thesis-status-dot'),
            thesisToggleBtn: document.getElementById('pd-thesis-toggle-btn'),
            thesisEditor: document.getElementById('pd-thesis-editor'),
            thesisEnabledCheckbox: document.getElementById('pd-thesis-enabled-checkbox'),
            thesisStatusHint: document.getElementById('pd-thesis-status-hint'),
            thesisClearBtn: document.getElementById('pd-thesis-clear-btn'),
            thesisDoneBtn: document.getElementById('pd-thesis-done-btn'),
            thesisDimensions: document.getElementById('pd-thesis-dimensions'),
            thesisGeoTags: document.getElementById('pd-thesis-geo-tags'),
            thesisSectorTags: document.getElementById('pd-thesis-sector-tags'),
            thesisStageTags: document.getElementById('pd-thesis-stage-tags'),
            thesisGeoInput: document.getElementById('pd-thesis-geo-input'),
            thesisGeoAddBtn: document.getElementById('pd-thesis-geo-add-btn'),
            thesisSectorInput: document.getElementById('pd-thesis-sector-input'),
            thesisSectorAddBtn: document.getElementById('pd-thesis-sector-add-btn'),
            thesisScorecard: document.getElementById('pd-thesis-scorecard'),
            scorecardTitleGroup: document.getElementById('pd-scorecard-title-group'),
            scorecardGrid: document.getElementById('pd-scorecard-grid'),
            thesisOverallBadge: document.getElementById('pd-thesis-overall-badge'),
            scorecardGeoVal: document.getElementById('pd-scorecard-geo-val'),
            scorecardGeoBadge: document.getElementById('pd-scorecard-geo-badge'),
            scorecardSectorVal: document.getElementById('pd-scorecard-sector-val'),
            scorecardSectorBadge: document.getElementById('pd-scorecard-sector-badge'),
            scorecardStageVal: document.getElementById('pd-scorecard-stage-val'),
            scorecardStageBadge: document.getElementById('pd-scorecard-stage-badge'),

            // Feature C & Export: Archive, Decision, Notes, Clipboard & PDF
            archiveToggleBtn: document.getElementById('pd-archive-toggle-btn'),
            archiveCountBadge: document.getElementById('pd-archive-count'),
            archiveDrawer: document.getElementById('pd-archive-drawer'),
            archiveList: document.getElementById('pd-archive-list'),
            boomerangBanner: document.getElementById('pd-boomerang-banner'),
            boomerangText: document.getElementById('pd-boomerang-text'),
            boomerangViewBtn: document.getElementById('pd-boomerang-view-btn'),
            decisionGroup: document.getElementById('pd-decision-group'),
            investorNotesInput: document.getElementById('pd-investor-notes-input'),
            notesSavedIndicator: document.getElementById('pd-notes-saved-indicator'),
            copyBriefBtn: document.getElementById('pd-copy-brief-btn'),
            exportPdfBtn: document.getElementById('pd-export-pdf-btn'),
            printReportContainer: document.getElementById('pd-print-report'),

            // Unified Cloud Sync Split-Button Controls
            headerSyncNowBtn: document.getElementById('pd-cloud-sync-now-btn'),
            dealSyncNowBtn: document.getElementById('pd-deal-sync-now-btn'),
            cloudMemoResultLink: document.getElementById('pd-cloud-memo-result-link'),
            cloudSheetResultLink: document.getElementById('pd-cloud-sheet-result-link')
        };

        if (!this.elements.pitchdeckView) {
            console.warn('[Pitchdeck] Pitchdeck view not found in DOM');
            return;
        }

        // Configure PDF.js worker if available
        this.configurePdfWorker();

        this.renderThesisConfigUI();
        this.renderArchiveListUI();
        storageSync.onBatchCheckRequested = (prov) => this.runCloudBatchCheck(prov);
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

            // Drag and drop (single PDF or multi-PDF batch)
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
                const droppedFiles = e.dataTransfer?.files;
                if (droppedFiles && droppedFiles.length > 1 && storageSync.isEnabled) {
                    this.handleLocalFolderUpload(droppedFiles);
                } else if (droppedFiles?.[0]) {
                    this.handleFileUpload(droppedFiles[0]);
                }
            });
        }

        const openLocalFolderPicker = () => {
            if (!storageSync.isEnabled || this.isBatchRunning) return;
            this.elements.folderInput?.click();
        };
        this.elements.localFolderBtn?.addEventListener('click', openLocalFolderPicker);
        this.elements.localFolderConnectedBtn?.addEventListener('click', openLocalFolderPicker);
        this.elements.folderInput?.addEventListener('change', (e) => {
            if (e.target.files?.length) {
                this.handleLocalFolderUpload(e.target.files);
            }
            e.target.value = '';
        });

        this.elements.batchProgressBar?.addEventListener('click', (e) => {
            const pill = e.target.closest('button.pd-batch-pill[data-analysis-id]');
            if (pill?.dataset.analysisId) {
                this.loadDealFromArchive(pill.dataset.analysisId);
            }
        });

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

        // Verify Public claims button (Feature B)
        this.elements.verifyPublicBtn?.addEventListener('click', () => {
            this.verifyPublicClaims();
        });

        // Check all claims button
        this.elements.checkAllBtn?.addEventListener('click', () => {
            this.checkAllClaims();
        });

        // Claims list event delegation (Bucket accordion toggle, Check claim button, OR jump to slide)
        if (this.elements.claimsList) {
            this.elements.claimsList.addEventListener('click', async (e) => {
                const bucketHeader = e.target.closest('.pd-risk-bucket-header');
                if (bucketHeader && bucketHeader.dataset.riskBucket) {
                    this.toggleRiskBucket(bucketHeader.dataset.riskBucket);
                    return;
                }

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

        // Feature A: Thesis Mandate Gate listeners
        this.elements.thesisToggleBtn?.addEventListener('click', () => {
            const editor = this.elements.thesisEditor;
            if (!editor) return;
            const isHidden = editor.classList.toggle('hidden');
            this.elements.thesisToggleBtn.setAttribute('aria-expanded', String(!isHidden));
        });

        this.elements.thesisDoneBtn?.addEventListener('click', () => {
            this.elements.thesisEditor?.classList.add('hidden');
            this.elements.thesisToggleBtn?.setAttribute('aria-expanded', 'false');
        });

        this.elements.thesisEnabledCheckbox?.addEventListener('change', (e) => {
            this.thesisConfig.enabled = Boolean(e.target.checked);
            this.saveThesisConfig();
        });

        this.elements.thesisClearBtn?.addEventListener('click', () => {
            this.thesisConfig.geographies = [];
            this.thesisConfig.sectors = [];
            this.thesisConfig.stages = [];
            this.saveThesisConfig();
        });

        [this.elements.thesisGeoTags, this.elements.thesisSectorTags, this.elements.thesisStageTags].forEach(container => {
            container?.addEventListener('click', (e) => {
                const pill = e.target.closest('.pd-thesis-tag-btn');
                if (!pill) return;
                const dimension = container.dataset.dimension;
                const tagValue = pill.dataset.tag;
                if (dimension && tagValue) {
                    this.toggleThesisTag(dimension, tagValue);
                }
            });
        });

        this.elements.thesisGeoAddBtn?.addEventListener('click', () => {
            this.addCustomThesisTag('geographies', this.elements.thesisGeoInput);
        });
        this.elements.thesisGeoInput?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.addCustomThesisTag('geographies', this.elements.thesisGeoInput);
            }
        });

        this.elements.thesisSectorAddBtn?.addEventListener('click', () => {
            this.addCustomThesisTag('sectors', this.elements.thesisSectorInput);
        });
        this.elements.thesisSectorInput?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.addCustomThesisTag('sectors', this.elements.thesisSectorInput);
            }
        });

        // Feature C: Deal Archive Drawer & Decision Controls
        this.elements.archiveToggleBtn?.addEventListener('click', () => {
            const drawer = this.elements.archiveDrawer;
            if (!drawer) return;
            const isHidden = drawer.classList.toggle('hidden');
            this.elements.archiveToggleBtn.setAttribute('aria-expanded', String(!isHidden));
        });

        this.elements.archiveList?.addEventListener('click', (e) => {
            const openBtn = e.target.closest('.pd-archive-open-btn');
            if (openBtn && openBtn.dataset.analysisId) {
                this.loadDealFromArchive(openBtn.dataset.analysisId);
                return;
            }
            const delBtn = e.target.closest('.pd-archive-delete-btn');
            if (delBtn && delBtn.dataset.analysisId) {
                this.deleteDealFromArchive(delBtn.dataset.analysisId);
            }
        });

        this.elements.boomerangViewBtn?.addEventListener('click', () => {
            if (this.priorBoomerangDeal?.analysis_id) {
                this.loadDealFromArchive(this.priorBoomerangDeal.analysis_id);
            }
        });

        this.elements.decisionGroup?.addEventListener('click', (e) => {
            const btn = e.target.closest('.pd-decision-btn');
            if (btn && btn.dataset.decision) {
                this.setInvestorDecision(btn.dataset.decision);
            }
        });

        let notesDebounce = null;
        this.elements.investorNotesInput?.addEventListener('input', (e) => {
            this.currentNotes = e.target.value;
            clearTimeout(notesDebounce);
            notesDebounce = setTimeout(() => {
                this.syncCurrentDealToArchive(true);
            }, 400);
        });

        // Phase 5: Copy Deal Brief & Export PDF
        this.elements.copyBriefBtn?.addEventListener('click', () => {
            this.copyDealBriefToClipboard();
        });

        this.elements.exportPdfBtn?.addEventListener('click', () => {
            this.exportDealBriefPdf();
        });

        // Batch Check card CTA vs Deal Header Memo Export CTA
        this.elements.headerSyncNowBtn?.addEventListener('click', () => {
            this.runCloudBatchCheck();
        });

        this.elements.dealSyncNowBtn?.addEventListener('click', () => {
            this.exportCurrentDealToCloud(this.elements.dealSyncNowBtn);
        });
    }

    /**
     * Load persistent investor thesis configuration from localStorage
     */
    loadThesisConfig() {
        const defaultConfig = {
            enabled: false,
            geographies: ['DACH', 'EU'],
            sectors: ['B2B SaaS', 'DeepTech', 'FinTech'],
            stages: ['Pre-Seed', 'Seed'],
            customGeographies: [],
            customSectors: []
        };
        try {
            const raw = localStorage.getItem(THESIS_STORAGE_KEY);
            if (!raw) return defaultConfig;
            const parsed = JSON.parse(raw);
            return {
                enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : false,
                geographies: Array.isArray(parsed.geographies) ? parsed.geographies : defaultConfig.geographies,
                sectors: Array.isArray(parsed.sectors) ? parsed.sectors : defaultConfig.sectors,
                stages: Array.isArray(parsed.stages) ? parsed.stages : defaultConfig.stages,
                customGeographies: Array.isArray(parsed.customGeographies) ? parsed.customGeographies : [],
                customSectors: Array.isArray(parsed.customSectors) ? parsed.customSectors : []
            };
        } catch (err) {
            console.warn('[Pitchdeck] Failed to read thesis config from localStorage:', err);
            return defaultConfig;
        }
    }

    /**
     * Persist investor thesis configuration and immediately refresh scorecard
     */
    saveThesisConfig() {
        try {
            localStorage.setItem(THESIS_STORAGE_KEY, JSON.stringify(this.thesisConfig));
        } catch (err) {
            console.warn('[Pitchdeck] Failed to save thesis config to localStorage:', err);
        }
        this.renderThesisConfigUI();
        if (this.lastAnalysisResult) {
            this.renderThesisScorecard(this.lastAnalysisResult);
            // Keep the archived snapshot consistent with the re-evaluated scorecard
            this.syncCurrentDealToArchive();
        }
    }

    /**
     * Toggle a thesis tag in the specified dimension
     */
    toggleThesisTag(dimension, tagValue) {
        const list = this.thesisConfig[dimension];
        if (!Array.isArray(list)) return;
        const idx = list.indexOf(tagValue);
        if (idx >= 0) {
            list.splice(idx, 1);
        } else {
            list.push(tagValue);
        }
        this.saveThesisConfig();
    }

    /**
     * Add a custom geography or sector tag
     */
    addCustomThesisTag(dimension, inputEl) {
        if (!inputEl) return;
        const val = inputEl.value.trim();
        if (!val) return;
        const cleanVal = DOMPurify.sanitize(val).replace(/<[^>]*>/g, '').trim().slice(0, 40);
        if (!cleanVal) return;

        const customKey = dimension === 'geographies' ? 'customGeographies' : 'customSectors';
        const baseOptions = DEFAULT_THESIS_OPTIONS[dimension] || [];
        const allOptions = [...baseOptions, ...(this.thesisConfig[customKey] || [])];

        if (!allOptions.some(t => t.toLowerCase() === cleanVal.toLowerCase())) {
            this.thesisConfig[customKey].push(cleanVal);
        }
        if (!this.thesisConfig[dimension].some(t => t.toLowerCase() === cleanVal.toLowerCase())) {
            this.thesisConfig[dimension].push(cleanVal);
        }
        inputEl.value = '';
        this.saveThesisConfig();
    }

    /**
     * Render the Thesis Configuration status dot, activation state, and tag buttons
     */
    renderThesisConfigUI() {
        const {
            thesisStatusDot,
            thesisEnabledCheckbox,
            thesisStatusHint,
            thesisClearBtn,
            thesisDimensions,
            thesisGeoTags,
            thesisSectorTags,
            thesisStageTags
        } = this.elements;

        const isEnabled = Boolean(this.thesisConfig.enabled);
        const totalSelected =
            (this.thesisConfig.geographies?.length || 0) +
            (this.thesisConfig.sectors?.length || 0) +
            (this.thesisConfig.stages?.length || 0);

        if (thesisStatusDot) {
            thesisStatusDot.classList.toggle('active', isEnabled);
        }
        if (thesisEnabledCheckbox) {
            thesisEnabledCheckbox.checked = isEnabled;
        }
        if (thesisStatusHint) {
            thesisStatusHint.textContent = isEnabled
                ? 'Active — deck will be scored against your target mandate.'
                : 'Off — deck will be fact-checked without thesis fit scoring.';
        }
        if (thesisDimensions) {
            thesisDimensions.classList.toggle('disabled', !isEnabled);
        }
        if (thesisClearBtn) {
            thesisClearBtn.disabled = !isEnabled || totalSelected === 0;
        }

        // 2. Render interactive tag buttons in collapsible editor
        const renderDimensionTags = (container, dimension, customKey) => {
            if (!container) return;
            const defaults = DEFAULT_THESIS_OPTIONS[dimension] || [];
            const customs = customKey ? (this.thesisConfig[customKey] || []) : [];
            const allTags = [...new Set([...defaults, ...customs])];
            const selectedSet = new Set((this.thesisConfig[dimension] || []).map(s => s.toLowerCase()));

            container.innerHTML = '';
            allTags.forEach(tag => {
                const btn = document.createElement('button');
                btn.type = 'button';
                const isSelected = selectedSet.has(tag.toLowerCase());
                btn.className = `pd-thesis-tag-btn ${isSelected ? 'active' : ''}`;
                btn.dataset.tag = tag;
                btn.setAttribute('aria-pressed', String(isSelected));
                btn.textContent = isSelected ? `✓ ${tag}` : tag;
                container.appendChild(btn);
            });
        };

        renderDimensionTags(thesisGeoTags, 'geographies', 'customGeographies');
        renderDimensionTags(thesisSectorTags, 'sectors', 'customSectors');
        renderDimensionTags(thesisStageTags, 'stages', null);
    }

    /**
     * Evaluate extracted deck profile against the user's persistent thesis configuration
     * @param {Object} result
     * @returns {Object} thesis_alignment object
     */
    evaluateThesisAlignment(result) {
        const profile = result?.startup_profile || {};
        const extractedGeo = (profile.geography && profile.geography !== 'Unknown')
            ? profile.geography
            : 'Unclear';
        const geoTags = Array.isArray(profile.geography_tags) ? profile.geography_tags : [];

        const rawSector = (profile.sector && profile.sector !== 'Unknown')
            ? profile.sector
            : (result?.sector || result?.industry || 'Unclear');
        const extractedSector = rawSector && rawSector !== '—' ? rawSector : 'Unclear';

        const extractedStage = (profile.stage && profile.stage !== 'Unknown')
            ? profile.stage
            : 'Unclear';

        const geoStatus = this._matchGeography(extractedGeo, geoTags, this.thesisConfig.geographies);
        const sectorStatus = this._matchSector(extractedSector, result?.industry, result?.sector, this.thesisConfig.sectors);
        const stageStatus = this._matchStage(extractedStage, result?.funding_ask, this.thesisConfig.stages);

        return {
            geography: { extracted: extractedGeo, status: geoStatus },
            sector: { extracted: extractedSector, status: sectorStatus },
            stage: { extracted: extractedStage, status: stageStatus }
        };
    }

    _matchGeography(extractedGeo, geoTags, targetGeos) {
        if (!Array.isArray(targetGeos) || targetGeos.length === 0) return 'partial';
        const targetLower = targetGeos.map(g => g.toLowerCase().trim());

        if (targetLower.includes('global')) {
            return extractedGeo === 'Unclear' ? 'partial' : 'match';
        }
        if (extractedGeo === 'Unclear' && geoTags.length === 0) {
            return 'partial';
        }

        const deckGeoTokens = [
            extractedGeo.toLowerCase(),
            ...geoTags.map(t => String(t).toLowerCase())
        ].join(' ');

        for (const target of targetLower) {
            if (deckGeoTokens.includes(target)) return 'match';
            const synonyms = REGION_SYNONYMS[target] || [];
            if (synonyms.some(syn => deckGeoTokens.includes(syn))) {
                return 'match';
            }
        }
        return 'mismatch';
    }

    _matchSector(extractedSector, rawIndustry, rawNiche, targetSectors) {
        if (!Array.isArray(targetSectors) || targetSectors.length === 0) return 'partial';
        if (extractedSector === 'Unclear' && !rawIndustry && !rawNiche) return 'partial';

        const combinedDeckSector = [extractedSector, rawIndustry || '', rawNiche || '']
            .join(' ')
            .toLowerCase();

        for (const target of targetSectors) {
            const targetKey = target.toLowerCase().trim();
            if (combinedDeckSector.includes(targetKey)) return 'match';
            const synonyms = SECTOR_SYNONYMS[targetKey] || [];
            if (synonyms.some(syn => combinedDeckSector.includes(syn))) {
                return 'match';
            }
        }
        return 'mismatch';
    }

    _matchStage(extractedStage, fundingAsk, targetStages) {
        if (!Array.isArray(targetStages) || targetStages.length === 0) return 'partial';
        if (extractedStage === 'Unclear' && !fundingAsk) return 'partial';

        const targetLower = targetStages.map(s => s.toLowerCase().trim());
        const stageLower = extractedStage.toLowerCase().trim();

        if (targetLower.includes(stageLower)) {
            return 'match';
        }
        if (extractedStage === 'Unclear') {
            return 'partial';
        }
        return 'mismatch';
    }

    /**
     * Render the 3-dimension Thesis Match Scorecard at the top of the workspace
     */
    renderThesisScorecard(result) {
        const scorecard = this.elements.thesisScorecard;
        if (!scorecard || !result) return;

        if (!this.thesisConfig.enabled) {
            this.currentThesisAlignment = null;
            this.elements.scorecardTitleGroup?.classList.add('hidden');
            this.elements.scorecardGrid?.classList.add('hidden');
            scorecard.classList.remove('hidden');
            return;
        }

        this.elements.scorecardTitleGroup?.classList.remove('hidden');
        this.elements.scorecardGrid?.classList.remove('hidden');

        const alignment = this.evaluateThesisAlignment(result);
        this.currentThesisAlignment = alignment;

        const applyDimensionBadge = (valEl, badgeEl, dimData) => {
            if (valEl) valEl.innerHTML = DOMPurify.sanitize(dimData.extracted || 'Unclear');
            if (!badgeEl) return;

            badgeEl.className = 'badge';
            if (dimData.status === 'match') {
                badgeEl.classList.add('badge-good');
                badgeEl.textContent = '✓ Match';
            } else if (dimData.status === 'mismatch') {
                badgeEl.classList.add('badge-critical');
                badgeEl.textContent = '✕ Mismatch';
            } else {
                badgeEl.classList.add('badge-caution');
                badgeEl.textContent = '◐ Partial / Unclear';
            }
        };

        applyDimensionBadge(this.elements.scorecardGeoVal, this.elements.scorecardGeoBadge, alignment.geography);
        applyDimensionBadge(this.elements.scorecardSectorVal, this.elements.scorecardSectorBadge, alignment.sector);
        applyDimensionBadge(this.elements.scorecardStageVal, this.elements.scorecardStageBadge, alignment.stage);

        const statuses = [alignment.geography.status, alignment.sector.status, alignment.stage.status];
        const matchCount = statuses.filter(s => s === 'match').length;
        const mismatchCount = statuses.filter(s => s === 'mismatch').length;

        const overallBadge = this.elements.thesisOverallBadge;
        if (overallBadge) {
            overallBadge.className = 'badge';
            if (mismatchCount > 0) {
                overallBadge.classList.add('badge-critical');
                overallBadge.textContent = `${mismatchCount} Thesis Mismatch${mismatchCount > 1 ? 'es' : ''}`;
            } else if (matchCount === 3) {
                overallBadge.classList.add('badge-good');
                overallBadge.textContent = '3/3 Thesis Match';
            } else {
                overallBadge.classList.add('badge-caution');
                overallBadge.textContent = `${matchCount}/3 Match · Needs Review`;
            }
        }

        scorecard.classList.remove('hidden');
    }

    /**
     * Point PDF.js at the self-hosted worker (no-op until the library is loaded).
     * The worker is self-hosted because Web Workers cannot be SRI-pinned.
     */
    configurePdfWorker() {
        if (window.pdfjsLib && !window.pdfjsLib.GlobalWorkerOptions?.workerSrc) {
            window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_SRC;
        }
    }

    /**
     * Ensure the PDF.js library script is loaded in window and worker configured
     */
    async ensurePdfJsLibLoaded() {
        if (!window.pdfjsLib) {
            await new Promise((resolve) => {
                const script = document.createElement('script');
                script.src = PDFJS_LIB_SRC;
                script.onload = resolve;
                script.onerror = resolve;
                document.head.appendChild(script);
            });
        }

        this.configurePdfWorker();
        return Boolean(window.pdfjsLib);
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
                ui.showToast(`Loaded ${this.totalSlides} slides. Click "Analyze" to extract intelligence.`, 'info');
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

            this.elements.thesisScorecard?.classList.add('hidden');

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

            summarySkeleton?.classList.add('hidden');
            summaryLoading?.classList.add('hidden');
            marketSkeleton?.classList.add('hidden');
            marketLoading?.classList.add('hidden');

            this.elements.generateBtn.disabled = false;
            this.elements.generateBtn.textContent = 'Analyze';
            this.elements.generateBtn.classList.remove('analyzing');
        } finally {
            clearTimeout(timeoutId);
        }
    }

    /**
     * Display analysis results in top dashboard and populate claims map
     * @param {Object} result
     * @param {Object} options - { fromArchive: boolean, archiveEntry: Object|null }
     */
    async displayResults(result, options = {}) {
        const { fromArchive = false, archiveEntry = null } = options;
        this.lastAnalysisResult = result;
        this.lastExportedDocUrl = null;
        this.elements.cloudMemoResultLink?.classList.add('hidden');
        this.elements.cloudSheetResultLink?.classList.add('hidden');

        const summaryLoading = document.getElementById('pd-summary-loading');
        const summaryResults = document.getElementById('pd-summary-results');
        const marketLoading = document.getElementById('pd-market-loading');
        const marketResults = document.getElementById('pd-market-results');

        summaryLoading?.classList.add('hidden');
        summaryResults?.classList.remove('hidden');
        marketLoading?.classList.add('hidden');
        marketResults?.classList.remove('hidden');

        // Feature C: Check for Boomerang Startup BEFORE archiving new deal
        if (!fromArchive) {
            const prior = this.findPriorDealByCompany(result.company_name);
            if (prior) {
                this.priorBoomerangDeal = prior;
                if (this.elements.boomerangBanner && this.elements.boomerangText) {
                    const dateStr = prior.analyzed_at ? new Date(prior.analyzed_at).toLocaleDateString() : 'previous session';
                    const decisionLabel = DECISION_LABELS[prior.investor_decision] || 'Pending';
                    this.elements.boomerangText.textContent = `Boomerang Startup: Previously analyzed on ${dateStr} — Decision: ${decisionLabel}`;
                    this.elements.boomerangBanner.classList.remove('hidden');
                }
            } else {
                this.priorBoomerangDeal = null;
                this.elements.boomerangBanner?.classList.add('hidden');
            }
            this.currentAnalysisId = (typeof crypto !== 'undefined' && crypto.randomUUID)
                ? crypto.randomUUID()
                : `pd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
            this.currentAnalyzedAt = new Date().toISOString();
            this.currentDecision = 'pending';
            this.currentNotes = '';
        } else if (archiveEntry) {
            this.priorBoomerangDeal = null;
            this.elements.boomerangBanner?.classList.add('hidden');
            this.currentAnalysisId = archiveEntry.analysis_id;
            this.currentAnalyzedAt = archiveEntry.analyzed_at || new Date().toISOString();
            this.currentDecision = archiveEntry.investor_decision || 'pending';
            this.currentNotes = archiveEntry.investor_notes || '';
        }

        // Sync Decision & Investor Notes controls
        this.renderDecisionControlUI();
        if (this.elements.investorNotesInput) {
            this.elements.investorNotesInput.value = this.currentNotes;
        }

        // Render Thesis Alignment Scorecard
        this.renderThesisScorecard(result);

        // Ensure PDF is loaded and active slide is rendered (if PDF file is available)
        if (!fromArchive) {
            await this.ensurePdfLoaded();
            if (this.pdfDoc) {
                await this.renderSlide(this.currentSlide || 1);
            }
        }

        // Company & Summary
        document.getElementById('pd-company-name').innerHTML = DOMPurify.sanitize(result.company_name || '—');
        this._displayCopyStyle(result.copy_style);
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
                    li.innerHTML = DOMPurify.sanitize(comp);
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
        this.elements.verifyPublicBtn?.classList.remove('hidden');
        this.elements.checkSlideBtn?.classList.remove('hidden');
        this.elements.checkAllBtn?.classList.remove('hidden');

        // Render claims according to active filter (defaults to Risk Priority View)
        this.renderFilteredClaims();

        // Persist lightweight deal snapshot into Zero-File Archive
        this.syncCurrentDealToArchive();
    }

    /**
     * Resolve effective risk level of a claim
     */
    _getClaimRiskLevel(claim) {
        if (claim.verificationResult?.risk_level) {
            return claim.verificationResult.risk_level;
        }
        if (claim.risk_level && ['high', 'medium', 'low', 'undefined'].includes(claim.risk_level)) {
            return claim.risk_level;
        }
        if (claim.verifiability === 'internal') {
            return 'medium';
        }
        return 'undefined';
    }

    /**
     * Toggle collapse state of a risk bucket in Risk Priority View
     */
    toggleRiskBucket(bucketKey) {
        if (this.collapsedRiskBuckets.has(bucketKey)) {
            this.collapsedRiskBuckets.delete(bucketKey);
        } else {
            this.collapsedRiskBuckets.add(bucketKey);
        }
        this.renderFilteredClaims();
    }

    /**
     * Create a single claim card DOM element
     */
    _createClaimCardElement(claim) {
        const card = document.createElement('div');
        const riskLevel = this._getClaimRiskLevel(claim);
        card.className = `pd-claim-card pd-claim-card--risk-${riskLevel}`;
        card.id = `pd-claim-${claim.originalIndex}`;
        card.dataset.slide = claim.slide_number || 1;
        card.dataset.claimIndex = claim.originalIndex;
        card.title = `Click to jump to Slide ${claim.slide_number || 1}`;

        if (claim.slide_number === this.currentSlide) {
            card.classList.add('slide-active');
        }

        const catLabel = (claim.category || 'other').replace('_', ' ').toUpperCase();
        const isVerified = Boolean(claim.verificationResult);

        const riskBadgeMap = {
            high: { label: '🚩 HIGH RISK', cls: 'badge-critical' },
            medium: { label: '❓ MEDIUM RISK', cls: 'badge-caution' },
            low: { label: '✅ LOW RISK', cls: 'badge-good' },
            undefined: { label: '⚪ UNDEFINED', cls: 'badge-neutral' }
        };
        const rBadge = riskBadgeMap[riskLevel] || riskBadgeMap.undefined;

        const verifiabilityBadge = claim.verifiability === 'internal'
            ? '<span class="badge badge-neutral" title="Proprietary metric — verify on call or in Data Room">Data Room</span>'
            : '<span class="badge badge-tag" title="Externally checkable public claim">Public</span>';

        const claimText = claim.claim || claim.claim_text || '';
        const findingText = claim.verificationResult?.verification_finding
            || claim.verificationResult?.explanation
            || claim.verification_finding
            || '';
        const suggestedQuestion = claim.verificationResult?.suggested_question
            || claim.suggested_question
            || '';

        card.innerHTML = DOMPurify.sanitize(`
            <div class="pd-claim-meta">
                <div class="pd-claim-badge-group">
                    <span class="badge ${rBadge.cls}">${rBadge.label}</span>
                    ${verifiabilityBadge}
                    <span class="badge badge-neutral">${catLabel}</span>
                    ${claim.slide_number ? `<span class="badge badge-tag pd-slide-tag" data-slide="${claim.slide_number}" title="Jump to Slide ${claim.slide_number}">Slide ${claim.slide_number}</span>` : ''}
                </div>
                <button class="btn-primary btn-sm pd-claim-check-btn ${isVerified ? 'verified' : ''}" data-claim-index="${claim.originalIndex}">
                    ${isVerified ? 'Verified ✓' : 'Verify Claim'}
                </button>
            </div>
            <div class="pd-claim-text">
                ${claimText}
            </div>
            ${claim.source_cited ? `<div class="pd-claim-source">Source cited: <em>${claim.source_cited}</em></div>` : ''}
            ${(!isVerified && findingText) ? `<div class="pd-claim-initial-finding">${findingText}</div>` : ''}
            <div class="pd-claim-result ${isVerified ? '' : 'hidden'}" id="pd-claim-result-${claim.originalIndex}"></div>
            ${((riskLevel === 'high' || riskLevel === 'medium') && suggestedQuestion) ? `
                <div class="pd-claim-question">
                    <span class="pd-claim-question-label">💬 Ask Founder:</span>
                    <span class="pd-claim-question-text">${suggestedQuestion}</span>
                </div>
            ` : ''}
        `);

        return card;
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
            if (this.activeFilter === 'risk') {
                titleEl.innerHTML = `Risk-Priority Triage`;
            } else if (this.activeFilter === 'slide') {
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
        list.innerHTML = '';

        // Feature B: Risk-Priority View (Default)
        if (this.activeFilter === 'risk') {
            if (countBadge) {
                countBadge.textContent = String(this.extractedClaims.length);
            }

            const buckets = {
                high: [],
                medium: [],
                low: [],
                undefined: []
            };

            this.extractedClaims.forEach(claim => {
                const level = this._getClaimRiskLevel(claim);
                if (buckets[level]) {
                    buckets[level].push(claim);
                } else {
                    buckets.undefined.push(claim);
                }
            });

            const bucketDefs = [
                {
                    key: 'high',
                    title: '🚩 High Risk — Material Contradictions & Red Flags',
                    subtitle: 'Contradicted or flagged quantitative claims',
                    cls: 'pd-risk-bucket--high'
                },
                {
                    key: 'medium',
                    title: '❓ Medium Risk — Founder Call Questions / Verify in Data Room',
                    subtitle: 'Proprietary metrics & unverifiable operational claims',
                    cls: 'pd-risk-bucket--medium'
                },
                {
                    key: 'low',
                    title: '✅ Low Risk — Verified Material Claims',
                    subtitle: 'Claims corroborated by external sources',
                    cls: 'pd-risk-bucket--low'
                },
                {
                    key: 'undefined',
                    title: '⚪ Undefined — Unverified Public Claims',
                    subtitle: 'Run "Verify Public Claims" to classify into High or Low risk',
                    cls: 'pd-risk-bucket--undefined'
                }
            ];

            bucketDefs.forEach(({ key, title, subtitle, cls }) => {
                const bucketClaims = buckets[key];
                const isCollapsed = this.collapsedRiskBuckets.has(key);

                const section = document.createElement('div');
                section.className = `pd-risk-bucket ${cls} ${bucketClaims.length === 0 ? 'pd-risk-bucket--empty' : ''}`;

                const header = document.createElement('button');
                header.type = 'button';
                header.className = 'pd-risk-bucket-header';
                header.dataset.riskBucket = key;
                header.setAttribute('aria-expanded', String(!isCollapsed));
                header.innerHTML = DOMPurify.sanitize(`
                    <div class="pd-risk-bucket-heading">
                        <span class="pd-risk-bucket-title">${title}</span>
                        <span class="pd-risk-bucket-subtitle">${subtitle}</span>
                    </div>
                    <div class="pd-risk-bucket-meta">
                        <span class="badge badge-neutral">${bucketClaims.length}</span>
                        <span class="pd-risk-bucket-chevron">${isCollapsed ? '▸' : '▾'}</span>
                    </div>
                `);

                const body = document.createElement('div');
                body.className = `pd-risk-bucket-body ${isCollapsed ? 'hidden' : ''}`;

                if (bucketClaims.length === 0) {
                    body.innerHTML = `<div class="pd-risk-bucket-empty-msg">No claims in this tier.</div>`;
                } else {
                    bucketClaims.forEach(claim => {
                        const card = this._createClaimCardElement(claim);
                        body.appendChild(card);
                    });
                }

                section.appendChild(header);
                section.appendChild(body);
                list.appendChild(section);
            });

            // Populate live verification results for any already-verified claims
            this.extractedClaims.forEach(claim => {
                if (claim.verificationResult) {
                    this.displayClaimResult(claim.originalIndex, claim.verificationResult);
                }
            });
            return;
        }

        // Slide-by-Slide, Financials, or All View
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

        if (claims.length === 0) {
            list.innerHTML = `
                <div class="pd-claims-empty-state" style="text-align: center; padding: 2rem 1rem; color: var(--color-text-muted);">
                    <div style="font-size: 1.2rem; margin-bottom: 0.5rem; opacity: 0.5;">—</div>
                    <p style="margin: 0; font-size: 0.9rem;">No specific claims extracted for Slide ${this.currentSlide}.</p>
                    <p style="margin: 0.25rem 0 0; font-size: 0.8rem; opacity: 0.8;">Navigate slides or switch to "Risk Priority" view.</p>
                </div>
            `;
            return;
        }

        claims.forEach(claim => {
            const card = this._createClaimCardElement(claim);
            list.appendChild(card);
            if (claim.verificationResult) {
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
                const labelSpan = document.createElement('span');
                labelSpan.className = 'pd-metric-label';
                labelSpan.textContent = label;
                const valueSpan = document.createElement('span');
                valueSpan.className = 'pd-metric-value pd-metric-value--muted';
                valueSpan.textContent = '—';
                const badgeSpan = document.createElement('span');
                badgeSpan.className = 'badge badge-neutral';
                badgeSpan.textContent = 'Not Disclosed';
                el.replaceChildren(labelSpan, valueSpan, badgeSpan);
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

    /**
     * Render the neutral copy-style marker. Informational only: 'inconclusive',
     * unknown, or missing values (e.g. legacy archive entries) hide the pill.
     */
    _displayCopyStyle(copyStyle) {
        const el = document.getElementById('pd-copy-style');
        if (!el) return;

        const styles = {
            distinctive: { label: 'Copy: Distinctive', badge: 'badge-neutral' },
            generic_ai: { label: 'Copy: Generic / AI-like', badge: 'badge-purple' },
        };
        const style = styles[copyStyle];
        el.classList.remove('badge-neutral', 'badge-purple');
        if (!style) {
            el.textContent = '';
            el.classList.add('hidden');
            return;
        }
        el.textContent = style.label;
        el.classList.add(style.badge);
        el.classList.remove('hidden');
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
            li.innerHTML = DOMPurify.sanitize(trimmed);
            list.appendChild(li);
        });
        container.classList.remove('hidden');
    }

    /**
     * Apply finding updates onto a claim object
     */
    _applyFindingToClaim(claim, finding) {
        claim.verificationResult = finding;
        if (finding.risk_level) {
            claim.risk_level = finding.risk_level;
        }
        if (finding.verification_finding || finding.explanation) {
            claim.verification_finding = finding.verification_finding || finding.explanation;
        }
        if (finding.suggested_question) {
            claim.suggested_question = finding.suggested_question;
        }
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
                    context: claim.context,
                    verifiability: claim.verifiability,
                    suggested_question: claim.suggested_question
                }]
            };

            const data = await api.verifyMarketClaims(payload);

            if (data.success && data.findings && data.findings.length > 0) {
                const finding = data.findings[0];
                this._applyFindingToClaim(claim, finding);
                if (this.activeFilter === 'risk') {
                    this.renderFilteredClaims();
                } else {
                    this.displayClaimResult(claimIndex, finding);
                }
                this.syncCurrentDealToArchive();
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
     * Verify a batch of claims in a single API call (up to 8 claims per batch, aligned with backend MAX_CLAIMS_PER_PROMPT)
     */
    async verifyClaimsBatch(claimsToVerify) {
        if (!claimsToVerify || claimsToVerify.length === 0) return;

        const BATCH_SIZE = 8;
        for (let i = 0; i < claimsToVerify.length; i += BATCH_SIZE) {
            const batch = claimsToVerify.slice(i, i + BATCH_SIZE);

            // Set loading state on all buttons in this batch
            batch.forEach(claim => {
                const checkBtn = document.querySelector(`.pd-claim-check-btn[data-claim-index="${claim.originalIndex}"]`);
                if (checkBtn) {
                    checkBtn.disabled = true;
                    checkBtn.innerHTML = '<span class="spinner" style="width:12px;height:12px;margin-right:6px;"></span> Checking...';
                    checkBtn.classList.add('loading');
                }
            });

            try {
                const payload = {
                    company: this.globalDeckContext?.company,
                    summary: this.globalDeckContext?.summary,
                    industry: this.globalDeckContext?.industry,
                    cache_name: this.globalDeckContext?.cache_name,
                    verifiable_claims: batch.map(claim => ({
                        claim: claim.claim,
                        category: claim.category,
                        source_cited: claim.source_cited,
                        slide_number: claim.slide_number,
                        context: claim.context,
                        verifiability: claim.verifiability,
                        suggested_question: claim.suggested_question
                    }))
                };

                const data = await api.verifyMarketClaims(payload);

                if (data.success && Array.isArray(data.findings)) {
                    batch.forEach((claim, idx) => {
                        const finding = data.findings[idx];
                        const checkBtn = document.querySelector(`.pd-claim-check-btn[data-claim-index="${claim.originalIndex}"]`);
                        if (finding) {
                            this._applyFindingToClaim(claim, finding);
                            this.displayClaimResult(claim.originalIndex, finding);
                            if (checkBtn) {
                                checkBtn.disabled = false;
                                checkBtn.innerHTML = 'Verified ✓';
                                checkBtn.classList.remove('loading');
                                checkBtn.classList.add('verified');
                            }
                        } else if (checkBtn) {
                            checkBtn.disabled = false;
                            checkBtn.textContent = 'Retry';
                            checkBtn.classList.remove('loading');
                        }
                    });
                } else {
                    throw new Error('Verification returned no findings');
                }

                if (data.new_balance !== undefined) {
                    ui.updateBalance(data.new_balance);
                }
            } catch (error) {
                console.error('[Pitchdeck] Batch claim verification error:', error);
                batch.forEach(claim => {
                    const checkBtn = document.querySelector(`.pd-claim-check-btn[data-claim-index="${claim.originalIndex}"]`);
                    if (checkBtn) {
                        checkBtn.disabled = false;
                        checkBtn.textContent = 'Retry';
                        checkBtn.classList.remove('loading');
                    }
                });
                ui.showToast(error.message || 'Verification failed. Please try again.', 'error');
                if (error.status === 403 || error.status === 401) break;
            }
        }

        if (this.activeFilter === 'risk') {
            this.renderFilteredClaims();
        }
        this.syncCurrentDealToArchive();
    }

    /**
     * Verify only externally verifiable public claims (saves CP by skipping internal Data Room claims)
     */
    async verifyPublicClaims() {
        const publicClaims = this.extractedClaims.filter(c => !c.verificationResult && c.verifiability !== 'internal');
        if (publicClaims.length === 0) {
            ui.showToast('All public claims are already verified.', 'info');
            return;
        }

        const btn = this.elements.verifyPublicBtn;
        if (btn) {
            btn.disabled = true;
            btn.textContent = `Verifying ${publicClaims.length} Public Claims...`;
        }

        await this.verifyClaimsBatch(publicClaims);

        if (btn) {
            btn.disabled = false;
            btn.textContent = '🔍 Verify Public Claims';
        }
        ui.showToast(`Verified ${publicClaims.length} public claims!`, 'success');
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

        await this.verifyClaimsBatch(slideClaims);

        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Verify Slide';
        }
        ui.showToast(`Slide ${this.currentSlide} claims verification complete!`, 'success');
    }

    /**
     * Verify all claims in the deck in batches
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

        await this.verifyClaimsBatch(unverified);

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

    // =========================================================================
    // Feature C: Zero-File Deal Archive & Investor Decision Log
    // =========================================================================

    loadArchivedDeals() {
        try {
            const raw = localStorage.getItem(ARCHIVE_STORAGE_KEY);
            if (!raw) return [];
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed : [];
        } catch (err) {
            console.warn('[Pitchdeck] Failed to load deal archive from localStorage:', err);
            return [];
        }
    }

    saveArchivedDeals() {
        try {
            if (this.archivedDeals.length > MAX_ARCHIVED_DEALS) {
                this.archivedDeals = this.archivedDeals.slice(0, MAX_ARCHIVED_DEALS);
            }
            localStorage.setItem(ARCHIVE_STORAGE_KEY, JSON.stringify(this.archivedDeals));
        } catch (err) {
            console.warn('[Pitchdeck] Failed to save deal archive to localStorage:', err);
        }
    }

    findPriorDealByCompany(companyName) {
        if (!companyName || typeof companyName !== 'string') return null;
        const normalized = companyName.trim().toLowerCase();
        if (!normalized || normalized === '—' || normalized === 'unknown company') return null;
        return this.archivedDeals.find(deal =>
            deal.company_name && deal.company_name.trim().toLowerCase() === normalized
        ) || null;
    }

    setInvestorDecision(decision) {
        if (!['pass', 'watchlist', 'advance_to_call', 'pending'].includes(decision)) return;
        this.currentDecision = this.currentDecision === decision ? 'pending' : decision;
        this.renderDecisionControlUI();
        this.syncCurrentDealToArchive(true);
    }

    renderDecisionControlUI() {
        const group = this.elements.decisionGroup;
        if (!group) return;
        group.querySelectorAll('.pd-decision-btn').forEach(btn => {
            const isMatch = btn.dataset.decision === this.currentDecision;
            btn.classList.toggle('active', isMatch);
            btn.setAttribute('aria-pressed', String(isMatch));
        });
    }

    _buildArchiveRecordFromResult(result, {
        analysisId,
        analyzedAt = new Date().toISOString(),
        decision = 'pending',
        notes = '',
        claimsSource = null,
        thesisAlignment = null,
        exportedDocUrl = null
    } = {}) {
        const r = result || {};
        const rawClaims = claimsSource || r.verifiable_claims || [];
        const retainedClaims = rawClaims.map(c => ({
            slide_number: c.slide_number || 1,
            risk_level: this._getClaimRiskLevel(c),
            verifiability: c.verifiability || 'public',
            category: c.category || 'other',
            claim_text: c.claim || c.claim_text || '',
            source_cited: c.source_cited || null,
            verification_finding: c.verificationResult?.verification_finding
                || c.verificationResult?.explanation
                || c.verification_finding
                || '',
            suggested_question: c.verificationResult?.suggested_question
                || c.suggested_question
                || '',
            verificationResult: c.verificationResult || null
        }));

        const resolvedAlignment = thesisAlignment
            || (this.thesisConfig.enabled ? this.evaluateThesisAlignment(r) : null)
            || {
                geography: { extracted: r.startup_profile?.geography || 'Not Disclosed', status: 'partial' },
                sector: { extracted: r.startup_profile?.sector || r.sector || 'Not Disclosed', status: 'partial' },
                stage: { extracted: r.startup_profile?.stage || 'Not Disclosed', status: 'partial' }
            };

        return {
            analysis_id: analysisId,
            company_name: r.company_name || 'Unnamed Startup',
            analyzed_at: analyzedAt,
            investor_decision: decision,
            investor_notes: notes,
            thesis_alignment: resolvedAlignment,
            executive_summary: r.summary || '',
            usp: r.usp || '',
            industry: r.industry || '',
            sector: r.sector || '',
            funding_ask: r.funding_ask || '',
            page_count: r.page_count || this.totalSlides || null,
            market_size: r.market_size || '',
            competition: r.competition || [],
            team_highlights: r.team_highlights || '',
            startup_profile: r.startup_profile || {},
            vc_metrics: r.vc_metrics || {},
            red_flags: r.red_flags || [],
            copy_style: r.copy_style || 'inconclusive',
            retained_claims: retainedClaims,
            exported_doc_url: exportedDocUrl || null
        };
    }

    syncCurrentDealToArchive(showIndicator = false) {
        if (!this.lastAnalysisResult || !this.currentAnalysisId) return;

        const existing = this.archivedDeals.find(d => d.analysis_id === this.currentAnalysisId);
        const archiveRecord = this._buildArchiveRecordFromResult(this.lastAnalysisResult, {
            analysisId: this.currentAnalysisId,
            analyzedAt: this.currentAnalyzedAt || new Date().toISOString(),
            decision: this.currentDecision || 'pending',
            notes: this.currentNotes || '',
            claimsSource: this.extractedClaims,
            thesisAlignment: this.currentThesisAlignment,
            exportedDocUrl: this.lastExportedDocUrl || existing?.exported_doc_url || null
        });

        const existingIdx = this.archivedDeals.findIndex(d => d.analysis_id === this.currentAnalysisId);
        if (existingIdx >= 0) {
            this.archivedDeals[existingIdx] = archiveRecord;
        } else {
            this.archivedDeals.unshift(archiveRecord);
        }

        this.saveArchivedDeals();
        this.renderArchiveListUI();

        if (showIndicator && this.elements.notesSavedIndicator) {
            this.elements.notesSavedIndicator.classList.remove('hidden');
            clearTimeout(this._savedIndicatorTimeout);
            this._savedIndicatorTimeout = setTimeout(() => {
                this.elements.notesSavedIndicator?.classList.add('hidden');
            }, 1800);
        }
    }

    archiveBatchDeal(result, { exportedDocUrl = null } = {}) {
        const analysisId = (typeof crypto !== 'undefined' && crypto.randomUUID)
            ? crypto.randomUUID()
            : `pd-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
        const archiveRecord = this._buildArchiveRecordFromResult(result, {
            analysisId,
            analyzedAt: new Date().toISOString(),
            decision: 'pending',
            notes: '',
            exportedDocUrl
        });
        this.archivedDeals.unshift(archiveRecord);
        this.saveArchivedDeals();
        this.renderArchiveListUI();
        return archiveRecord;
    }

    renderArchiveListUI() {
        if (this.elements.archiveCountBadge) {
            this.elements.archiveCountBadge.textContent = String(this.archivedDeals.length);
        }
        const listEl = this.elements.archiveList;
        if (!listEl) return;

        if (this.archivedDeals.length === 0) {
            listEl.innerHTML = `<div class="pd-archive-empty">No archived deals yet. Analyzed pitch decks will appear here.</div>`;
            return;
        }

        const decisionBadgeClass = {
            advance_to_call: 'badge-good',
            watchlist: 'badge-caution',
            pass: 'badge-critical',
            pending: 'badge-neutral'
        };

        listEl.innerHTML = DOMPurify.sanitize(this.archivedDeals.map(deal => {
            const dateStr = deal.analyzed_at ? new Date(deal.analyzed_at).toLocaleDateString() : '—';
            const decLabel = DECISION_LABELS[deal.investor_decision] || 'Pending';
            const decCls = decisionBadgeClass[deal.investor_decision] || 'badge-neutral';
            const highCount = (deal.retained_claims || []).filter(c => c.risk_level === 'high').length;
            const medCount = (deal.retained_claims || []).filter(c => c.risk_level === 'medium').length;
            const isCurrent = deal.analysis_id === this.currentAnalysisId;
            const memoLinkHtml = (typeof deal.exported_doc_url === 'string' && deal.exported_doc_url.startsWith('https://'))
                ? `<a href="${deal.exported_doc_url}" target="_blank" rel="noopener noreferrer" class="btn-secondary btn-sm">Memo ↗</a>`
                : '';

            return `
                <div class="pd-archive-item ${isCurrent ? 'pd-archive-item--active' : ''}">
                    <div class="pd-archive-item-main">
                        <div class="pd-archive-item-title-row">
                            <strong class="pd-archive-company">${deal.company_name}</strong>
                            <span class="badge ${decCls}">${decLabel}</span>
                            <span class="pd-archive-date">${dateStr}</span>
                        </div>
                        <div class="pd-archive-item-meta">
                            <span>Geo: ${deal.thesis_alignment?.geography?.extracted || '—'}</span>
                            <span>·</span>
                            <span>Sector: ${deal.thesis_alignment?.sector?.extracted || '—'}</span>
                            <span>·</span>
                            <span>Stage: ${deal.thesis_alignment?.stage?.extracted || '—'}</span>
                            <span>·</span>
                            <span class="pd-archive-risk-counts">🚩 ${highCount} High · ❓ ${medCount} Med</span>
                        </div>
                        ${deal.investor_notes ? `<div class="pd-archive-notes">"${deal.investor_notes}"</div>` : ''}
                    </div>
                    <div class="pd-archive-item-actions">
                        ${memoLinkHtml}
                        <button type="button" class="btn-secondary btn-sm pd-archive-open-btn" data-analysis-id="${deal.analysis_id}">
                            ${isCurrent ? 'Active' : 'Load Brief'}
                        </button>
                        <button type="button" class="btn-text btn-sm pd-archive-delete-btn" data-analysis-id="${deal.analysis_id}" title="Remove from Archive">✕</button>
                    </div>
                </div>
            `;
        }).join(''));
    }

    async loadDealFromArchive(analysisId) {
        const entry = this.archivedDeals.find(d => d.analysis_id === analysisId);
        if (!entry) return;

        // Reconstruct result payload compatible with displayResults
        const reconstructed = {
            company_name: entry.company_name,
            summary: entry.executive_summary,
            usp: entry.usp || '—',
            industry: entry.industry || '—',
            sector: entry.sector || entry.thesis_alignment?.sector?.extracted || '—',
            funding_ask: entry.funding_ask || '',
            page_count: entry.page_count ?? null,
            market_size: entry.market_size || 'Not specified',
            competition: entry.competition || [],
            team_highlights: entry.team_highlights || '',
            startup_profile: entry.startup_profile || {
                geography: entry.thesis_alignment?.geography?.extracted || 'Not Disclosed',
                geography_tags: [],
                sector: entry.thesis_alignment?.sector?.extracted || 'Not Disclosed',
                stage: entry.thesis_alignment?.stage?.extracted || 'Not Disclosed'
            },
            vc_metrics: entry.vc_metrics || {},
            red_flags: entry.red_flags || [],
            copy_style: entry.copy_style || 'inconclusive',
            verifiable_claims: (entry.retained_claims || []).map(c => ({
                claim: c.claim_text || c.claim || '',
                category: c.category || 'other',
                slide_number: c.slide_number || 1,
                source_cited: c.source_cited || null,
                verifiability: c.verifiability || 'public',
                risk_level: c.risk_level || 'undefined',
                verification_finding: c.verification_finding || '',
                suggested_question: c.suggested_question || '',
                verificationResult: c.verificationResult || null
            }))
        };

        this.elements.triageSection?.classList.remove('hidden');
        this.elements.workstation?.classList.remove('hidden');

        // Restore in-memory PDF slides if uploaded during this tab session; otherwise show Zero-File placeholder
        const sessionFile = this.sessionBatchFiles.get(analysisId);
        this.pdfDoc = null;
        if (sessionFile) {
            this.uploadedFile = sessionFile;
            this.uploadedFileData = null;
            const loaded = await this.ensurePdfLoaded();
            if (loaded && this.pdfDoc) {
                this.currentSlide = 1;
                await this.renderSlide(1);
            }
        } else {
            this.uploadedFile = null;
            this.uploadedFileData = null;
            this.elements.slideCanvas?.classList.add('hidden');
            if (this.elements.canvasPlaceholder) {
                this.elements.canvasPlaceholder.classList.remove('hidden');
                const titleP = document.createElement('p');
                titleP.textContent = `Deal Brief: ${entry.company_name}`;

                const children = [titleP];
                if (typeof entry.exported_doc_url === 'string' && entry.exported_doc_url.startsWith('https://')) {
                    const docLink = document.createElement('a');
                    docLink.href = entry.exported_doc_url;
                    docLink.target = '_blank';
                    docLink.rel = 'noopener noreferrer';
                    docLink.className = 'btn-secondary btn-sm';
                    docLink.textContent = 'Open Cloud Memo ↗';
                    children.push(docLink);
                }
                this.elements.canvasPlaceholder.replaceChildren(...children);
            }
        }

        await this.displayResults(reconstructed, { fromArchive: true, archiveEntry: entry });
        if (typeof entry.exported_doc_url === 'string' && entry.exported_doc_url.startsWith('https://')) {
            this.lastExportedDocUrl = entry.exported_doc_url;
            if (this.elements.cloudMemoResultLink) {
                this.elements.cloudMemoResultLink.href = entry.exported_doc_url;
                this.elements.cloudMemoResultLink.textContent = entry.exported_doc_url.includes('docs.google.com')
                    ? 'Open Doc ↗'
                    : 'Open Memo ↗';
                this.elements.cloudMemoResultLink.classList.remove('hidden');
            }
        }
        this.renderBatchProgressUI();
        this.elements.archiveDrawer?.classList.add('hidden');
        this.elements.archiveToggleBtn?.setAttribute('aria-expanded', 'false');
        ui.showToast(`Loaded Deal Brief for ${entry.company_name}`, 'info');
    }

    deleteDealFromArchive(analysisId) {
        this.archivedDeals = this.archivedDeals.filter(d => d.analysis_id !== analysisId);
        this.saveArchivedDeals();
        this.renderArchiveListUI();
        ui.showToast('Removed deal from local archive.', 'info');
    }

    // =========================================================================
    // Phase 5: One-Click Copy Deal Brief & Structured PDF Export
    // =========================================================================

    _decodeHtmlEntities(str) {
        if (!str || typeof str !== 'string') return str || '';
        const entityMap = {
            '&amp;': '&',
            '&lt;': '<',
            '&gt;': '>',
            '&quot;': '"',
            '&#x27;': "'",
            '&#39;': "'",
            '&#x2F;': '/'
        };
        return str.replace(/&(?:amp|lt|gt|quot|#x27|#39|#x2F);/g, match => entityMap[match] || match);
    }

    compileDealBriefMarkdown() {
        const r = this.lastAnalysisResult || {};
        const dec = val => this._decodeHtmlEntities(val);
        const companyName = dec(r.company_name) || 'Unknown Startup';
        const analyzedDate = this.currentAnalyzedAt
            ? new Date(this.currentAnalyzedAt).toISOString().split('T')[0]
            : new Date().toISOString().split('T')[0];
        const decisionStr = DECISION_LABELS[this.currentDecision] || 'Pending';
        const notesStr = (this.currentNotes || '').trim() || 'None';

        const align = this.currentThesisAlignment;
        const thesisLines = align ? [
            ``,
            `## Thesis Alignment`,
            `* **Geography:** ${dec(align.geography.extracted)} (${align.geography.status})`,
            `* **Sector:** ${dec(align.sector.extracted)} (${align.sector.status})`,
            `* **Stage:** ${dec(align.stage.extracted)} (${align.stage.status})`
        ] : [];

        const highClaims = (this.extractedClaims || []).filter(c => this._getClaimRiskLevel(c) === 'high');
        const medClaims = (this.extractedClaims || []).filter(c => this._getClaimRiskLevel(c) === 'medium');

        const formatHighClaims = highClaims.length > 0
            ? highClaims.map(c => {
                const finding = dec(c.verificationResult?.verification_finding
                    || c.verificationResult?.explanation
                    || c.verification_finding
                    || 'Contradicted or flagged as high risk.');
                const question = dec(c.verificationResult?.suggested_question
                    || c.suggested_question
                    || 'Can you provide primary source documentation for this claim?');
                return `* **[Slide ${c.slide_number || 1}]** "${dec(c.claim)}"\n  * *Finding:* ${finding}\n  * *Ask Founder:* ${question}`;
            }).join('\n')
            : '* None identified.';

        const formatMedClaims = medClaims.length > 0
            ? medClaims.map(c => {
                const context = dec(c.verificationResult?.verification_finding
                    || c.verificationResult?.explanation
                    || c.verification_finding
                    || 'Unverifiable publicly. Requires internal Data Room proof.');
                const question = dec(c.verificationResult?.suggested_question
                    || c.suggested_question
                    || 'Please share underlying cohort or financial schedules in the Data Room.');
                return `* **[Slide ${c.slide_number || 1}]** "${dec(c.claim)}"\n  * *Context:* ${context}\n  * *Ask Founder:* ${question}`;
            }).join('\n')
            : '* None identified.';

        return [
            `# Authenix Deal Brief: ${companyName}`,
            `**Date Analyzed:** ${analyzedDate}`,
            `**Decision:** ${decisionStr}`,
            `**Investor Notes:** ${notesStr}`,
            ...thesisLines,
            ``,
            `## Executive Summary`,
            `${dec(r.summary) || 'No summary available.'}`,
            ``,
            `## High-Risk Red Flags (Material Contradictions)`,
            formatHighClaims,
            ``,
            `## Medium-Risk Claims (Verify on Call / Data Room)`,
            formatMedClaims
        ].join('\n');
    }

    async copyDealBriefToClipboard() {
        if (!this.lastAnalysisResult) {
            ui.showToast('Analyze a pitch deck first to copy a Deal Brief.', 'warning');
            return;
        }
        const markdown = this.compileDealBriefMarkdown();
        try {
            await navigator.clipboard.writeText(markdown);
            const btn = this.elements.copyBriefBtn;
            if (btn) {
                const originalText = btn.textContent;
                btn.textContent = '✓ Copied to Clipboard';
                btn.classList.add('copied');
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.classList.remove('copied');
                }, 2000);
            }
            ui.showToast('Deal Brief copied to clipboard (Markdown)!', 'success');
        } catch (err) {
            console.error('[Pitchdeck] Clipboard write failed:', err);
            ui.showToast('Could not access clipboard. Please try again.', 'error');
        }
    }

    exportDealBriefPdf() {
        if (!this.lastAnalysisResult) {
            ui.showToast('Analyze a pitch deck first to export a PDF Deal Brief.', 'warning');
            return;
        }

        const r = this.lastAnalysisResult;
        const printContainer = this.elements.printReportContainer || document.getElementById('pd-print-report');
        if (!printContainer) return;

        const analyzedDate = this.currentAnalyzedAt
            ? new Date(this.currentAnalyzedAt).toLocaleDateString()
            : new Date().toLocaleDateString();
        const decisionStr = DECISION_LABELS[this.currentDecision] || 'Pending';
        const notesStr = (this.currentNotes || '').trim() || 'None';

        const align = this.currentThesisAlignment;
        const thesisRowHtml = align ? `
            <div class="pd-print-thesis-row">
                <div class="pd-print-thesis-pill"><strong>Geography:</strong> ${align.geography.extracted} (${align.geography.status.toUpperCase()})</div>
                <div class="pd-print-thesis-pill"><strong>Sector:</strong> ${align.sector.extracted} (${align.sector.status.toUpperCase()})</div>
                <div class="pd-print-thesis-pill"><strong>Stage:</strong> ${align.stage.extracted} (${align.stage.status.toUpperCase()})</div>
            </div>
        ` : '';

        const highClaims = (this.extractedClaims || []).filter(c => this._getClaimRiskLevel(c) === 'high');
        const medClaims = (this.extractedClaims || []).filter(c => this._getClaimRiskLevel(c) === 'medium');
        const lowClaims = (this.extractedClaims || []).filter(c => this._getClaimRiskLevel(c) === 'low');

        const highHtml = highClaims.length > 0
            ? highClaims.map(c => {
                const finding = c.verificationResult?.verification_finding || c.verificationResult?.explanation || c.verification_finding || 'Material contradiction or structural risk flag.';
                const question = c.verificationResult?.suggested_question || c.suggested_question || '';
                return `
                    <div class="pd-print-claim pd-print-claim--high">
                        <div class="pd-print-claim-head"><strong>[Slide ${c.slide_number || 1}]</strong> "${c.claim}"</div>
                        <div class="pd-print-claim-body"><strong>Finding:</strong> ${finding}</div>
                        ${question ? `<div class="pd-print-claim-q"><strong>Ask Founder:</strong> ${question}</div>` : ''}
                    </div>
                `;
            }).join('')
            : '<p class="pd-print-empty">No high-risk material contradictions flagged.</p>';

        const medHtml = medClaims.length > 0
            ? medClaims.map(c => {
                const finding = c.verificationResult?.verification_finding || c.verificationResult?.explanation || c.verification_finding || 'Unverifiable publicly. Verify in Data Room.';
                const question = c.verificationResult?.suggested_question || c.suggested_question || '';
                return `
                    <div class="pd-print-claim pd-print-claim--medium">
                        <div class="pd-print-claim-head"><strong>[Slide ${c.slide_number || 1}]</strong> "${c.claim}"</div>
                        <div class="pd-print-claim-body"><strong>Context:</strong> ${finding}</div>
                        ${question ? `<div class="pd-print-claim-q"><strong>Ask Founder:</strong> ${question}</div>` : ''}
                    </div>
                `;
            }).join('')
            : '<p class="pd-print-empty">No medium-risk interrogation points flagged.</p>';

        const lowRowsHtml = lowClaims.length > 0
            ? lowClaims.map(c => {
                const finding = c.verificationResult?.verification_finding || c.verificationResult?.explanation || c.verification_finding || 'Corroborated';
                return `
                    <tr>
                        <td>Slide ${c.slide_number || 1}</td>
                        <td>${c.claim}</td>
                        <td>${finding}</td>
                    </tr>
                `;
            }).join('')
            : '<tr><td colspan="3">No verified low-risk claims recorded yet.</td></tr>';

        printContainer.innerHTML = DOMPurify.sanitize(`
            <div class="pd-print-header">
                <div>
                    <h1 class="pd-print-title">Authenix Deal Brief: ${r.company_name || 'Startup'}</h1>
                    <div class="pd-print-sub">Date Analyzed: ${analyzedDate} · Industry: ${r.industry || '—'} · Sector: ${r.sector || '—'}</div>
                </div>
                <div class="pd-print-decision">Decision: <strong>${decisionStr}</strong></div>
            </div>
            <div class="pd-print-notes"><strong>Investor Notes:</strong> ${notesStr}</div>
            ${thesisRowHtml}
            <div class="pd-print-section">
                <h2>Executive Summary</h2>
                <p>${r.summary || '—'}</p>
                <p><strong>Unique Selling Proposition:</strong> ${r.usp || '—'}</p>
                <div class="pd-print-counters">
                    <span>🚩 High Risk: <strong>${highClaims.length}</strong></span> ·
                    <span>❓ Medium Risk (Call Prep): <strong>${medClaims.length}</strong></span> ·
                    <span>✅ Verified (Low Risk): <strong>${lowClaims.length}</strong></span>
                </div>
            </div>
            <div class="pd-print-section">
                <h2>Section 1 — High-Risk Red Flags (Material Contradictions)</h2>
                ${highHtml}
            </div>
            <div class="pd-print-section">
                <h2>Section 2 — Medium-Risk Interrogation Points (Verify on Call / Data Room)</h2>
                ${medHtml}
            </div>
            <div class="pd-print-section">
                <h2>Appendix — Verified (Low-Risk) Claims</h2>
                <table class="pd-print-table">
                    <thead>
                        <tr>
                            <th>Slide #</th>
                            <th>Claim</th>
                            <th>Verification Finding</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${lowRowsHtml}
                    </tbody>
                </table>
            </div>
        `);

        window.print();
    }

    buildCloudExportPayload(resultOverride = null) {
        const isOverride = Boolean(resultOverride);
        const r = resultOverride || this.lastAnalysisResult || {};
        const dec = val => this._decodeHtmlEntities(val);
        const align = isOverride
            ? (this.thesisConfig.enabled ? this.evaluateThesisAlignment(r) : null)
            : this.currentThesisAlignment;
        const flatThesis = align ? {
            geography: dec(align.geography?.extracted) || 'Unknown',
            sector: dec(align.sector?.extracted) || 'Unknown',
            stage: dec(align.stage?.extracted) || 'Unknown',
            status: isOverride ? '' : (this.elements.thesisOverallBadge?.textContent || '').trim()
        } : undefined;

        const sourceClaims = isOverride ? (r.verifiable_claims || []) : (this.extractedClaims || []);
        const claims = sourceClaims.slice(0, 50).map(c => ({
            claim: c.claim || c.claim_text || '',
            slide_number: c.slide_number || 1,
            category: c.category || 'other',
            verifiability: c.verifiability || 'public',
            risk_level: this._getClaimRiskLevel(c),
            verification_finding: c.verificationResult?.verification_finding
                || c.verificationResult?.explanation
                || c.verification_finding
                || '',
            suggested_question: c.verificationResult?.suggested_question
                || c.suggested_question
                || ''
        }));

        const rawPageCount = r.page_count ?? (!isOverride && this.pdfDoc ? this.totalSlides : undefined);
        const payload = {
            company_name: dec(r.company_name) || 'Unknown Startup',
            summary: dec(r.summary) || '',
            analyzed_at: (!isOverride && this.currentAnalyzedAt)
                ? new Date(this.currentAnalyzedAt).toISOString().split('T')[0]
                : new Date().toISOString().split('T')[0],
            industry: r.industry || '',
            sector: r.sector || '',
            funding_ask: r.funding_ask || '',
            page_count: typeof rawPageCount === 'number' && rawPageCount > 0 ? rawPageCount : undefined,
            investor_decision: isOverride ? 'Pending' : (DECISION_LABELS[this.currentDecision] || 'Pending'),
            investor_notes: isOverride ? '' : (this.currentNotes || ''),
            usp: r.usp || '',
            market_size: r.market_size || '',
            competition: Array.isArray(r.competition) ? r.competition.slice(0, 20) : [],
            team_highlights: r.team_highlights || '',
            vc_metrics: r.vc_metrics || {},
            red_flags: Array.isArray(r.red_flags) ? r.red_flags.slice(0, 20) : [],
            verifiable_claims: claims,
            startup_profile: r.startup_profile || {},
            thesis_alignment: flatThesis
        };

        // Guard against exceeding backend 256 KB JSON cap
        if (JSON.stringify(payload).length > 240 * 1024) {
            payload.verifiable_claims = claims.slice(0, 15);
            payload.summary = payload.summary.slice(0, 5000);
        }
        return payload;
    }

    async exportCurrentDealToCloud(triggerBtn = null) {
        if (!storageSync.isEnabled) return;
        if (!this.lastAnalysisResult) {
            ui.showToast('Check a pitch deck first to export a Deal Memo.', 'warning');
            return;
        }

        const activeConn = await storageSync.getPrimaryConnectedConnection();
        if (!activeConn) {
            ui.showToast('Connect Google Drive, Dropbox, or OneDrive to export Deal Memos.', 'info');
            await storageSync.openModal();
            return;
        }

        const provider = activeConn.provider;
        const btn = triggerBtn || this.elements.dealSyncNowBtn;
        const originalText = btn ? btn.textContent : 'Export Memo';
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Exporting...';
        }

        try {
            const exportRes = await api.exportPitchdeckCloudSync({
                provider,
                ...this.buildCloudExportPayload()
            });
            if (exportRes?.memo_url && exportRes.memo_url.startsWith('https://')) {
                this.lastExportedDocUrl = exportRes.memo_url;
                if (this.elements.cloudMemoResultLink) {
                    this.elements.cloudMemoResultLink.href = exportRes.memo_url;
                    this.elements.cloudMemoResultLink.textContent = provider === 'google_drive' ? 'Open Doc ↗' : 'Open Memo ↗';
                    this.elements.cloudMemoResultLink.classList.remove('hidden');
                }
                this.syncCurrentDealToArchive();
            }
            if (exportRes?.sheet_url && exportRes.sheet_url.startsWith('https://') && this.elements.cloudSheetResultLink) {
                this.elements.cloudSheetResultLink.href = exportRes.sheet_url;
                this.elements.cloudSheetResultLink.textContent = 'Open Sheet ↗';
                this.elements.cloudSheetResultLink.classList.remove('hidden');
            }
            const sheetNote = exportRes?.updated_sheet_range ? ` + Sheet (${exportRes.updated_sheet_range})` : '';
            ui.showToast(`Deal Memo exported (${exportRes.mode || 'brief'})${sheetNote}.`, 'success');
        } catch (error) {
            if (error.status === 401 && (error.data?.needs_reauth || error.data?.needs_google_auth)) {
                try {
                    sessionStorage.setItem(PENDING_STORAGE_ACTION_KEY, JSON.stringify({
                        action: 'cloud-sync',
                        provider,
                        analysis_id: this.currentAnalysisId
                    }));
                } catch (_) {}
                ui.showToast('Cloud storage authorization expired. Reconnecting...', 'info');
                window.location.assign(`/api/storage/${encodeURIComponent(provider)}/connect`);
                return;
            }
            ui.showToast('Export Memo failed: ' + error.message, 'error');
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
        }
    }

    _setWorkstationBatchLoading(isLoading) {
        const summarySkeleton = document.getElementById('pd-summary-skeleton');
        const summaryLoading = document.getElementById('pd-summary-loading');
        const summaryResults = document.getElementById('pd-summary-results');
        const marketSkeleton = document.getElementById('pd-market-skeleton');
        const marketLoading = document.getElementById('pd-market-loading');
        const marketResults = document.getElementById('pd-market-results');

        if (isLoading) {
            summarySkeleton?.classList.add('hidden');
            summaryResults?.classList.add('hidden');
            summaryLoading?.classList.remove('hidden');
            marketSkeleton?.classList.add('hidden');
            marketResults?.classList.add('hidden');
            marketLoading?.classList.remove('hidden');
            this.elements.thesisScorecard?.classList.add('hidden');
        } else {
            summaryLoading?.classList.add('hidden');
            marketLoading?.classList.add('hidden');
        }
    }

    async _openBatchDealInWorkstation(result, { file = null, exportedDocUrl = null, provider = null } = {}) {
        this.pdfDoc = null;
        if (file) {
            this.uploadedFile = file;
            this.uploadedFileData = null;
            this.showUploadSuccess(file.name);
        } else {
            this.uploadedFile = null;
            this.uploadedFileData = null;
            this.elements.slideCanvas?.classList.add('hidden');
            if (this.elements.canvasPlaceholder) {
                this.elements.canvasPlaceholder.classList.remove('hidden');
                const titleP = document.createElement('p');
                titleP.textContent = `Deal Brief: ${result.company_name || 'Checked Startup'}`;
                const children = [titleP];
                if (typeof exportedDocUrl === 'string' && exportedDocUrl.startsWith('https://')) {
                    const docLink = document.createElement('a');
                    docLink.href = exportedDocUrl;
                    docLink.target = '_blank';
                    docLink.rel = 'noopener noreferrer';
                    docLink.className = 'btn-secondary btn-sm';
                    docLink.textContent = 'Open Cloud Memo ↗';
                    children.push(docLink);
                }
                this.elements.canvasPlaceholder.replaceChildren(...children);
            }
        }

        await this.displayResults(result);
        if (file && this.currentAnalysisId) {
            this.sessionBatchFiles.set(this.currentAnalysisId, file);
        }
        if (typeof exportedDocUrl === 'string' && exportedDocUrl.startsWith('https://')) {
            this.lastExportedDocUrl = exportedDocUrl;
            if (this.elements.cloudMemoResultLink) {
                this.elements.cloudMemoResultLink.href = exportedDocUrl;
                this.elements.cloudMemoResultLink.textContent = provider === 'google_drive' ? 'Open Doc ↗' : 'Open Memo ↗';
                this.elements.cloudMemoResultLink.classList.remove('hidden');
            }
            this.syncCurrentDealToArchive();
        }
        return this.currentAnalysisId;
    }

    renderBatchProgressUI(headlineOverride = null) {
        const container = this.elements.batchProgressBar;
        if (!container) return;

        if (!Array.isArray(this.batchQueue) || this.batchQueue.length === 0) {
            container.classList.add('hidden');
            return;
        }

        const total = this.batchQueue.length;
        const doneCount = this.batchQueue.filter(i => i.status === 'done').length;
        const errCount = this.batchQueue.filter(i => i.status === 'error').length;
        const runningIdx = this.batchQueue.findIndex(i => i.status === 'running');

        let statusLine = headlineOverride;
        if (!statusLine) {
            if (runningIdx >= 0) {
                statusLine = `Batch Check — Checking ${runningIdx + 1} of ${total}: ${this.batchQueue[runningIdx].name}…`;
            } else {
                const suffix = errCount > 0 ? ` (${errCount} skipped/failed)` : '';
                statusLine = `Batch Check — Checked ${doneCount} of ${total} deck${total === 1 ? '' : 's'}${suffix}`;
            }
        }

        const header = document.createElement('div');
        header.className = 'pd-batch-progress-header';

        const statusSpan = document.createElement('span');
        statusSpan.className = 'pd-batch-progress-status';
        statusSpan.textContent = statusLine;
        header.appendChild(statusSpan);

        const hintSpan = document.createElement('span');
        hintSpan.className = 'pd-batch-progress-hint';
        hintSpan.textContent = doneCount > 0
            ? 'All checked briefs are saved in Deal Archive — click any deck below to view in the Workstation.'
            : 'Results will open right here in the Workstation as each deck finishes.';
        header.appendChild(hintSpan);

        const pillsRow = document.createElement('div');
        pillsRow.className = 'pd-batch-pills';

        this.batchQueue.forEach((item) => {
            if (item.status === 'done' && item.analysisId) {
                const btn = document.createElement('button');
                btn.type = 'button';
                const isViewing = item.analysisId === this.currentAnalysisId;
                btn.className = `pd-batch-pill ${isViewing ? 'pd-batch-pill--active' : ''}`;
                btn.dataset.analysisId = item.analysisId;
                btn.textContent = `✓ ${item.companyName || item.name}`;
                btn.title = `Open ${item.companyName || item.name} in Workstation`;
                pillsRow.appendChild(btn);
            } else {
                const span = document.createElement('span');
                const cls = item.status === 'running'
                    ? 'pd-batch-pill pd-batch-pill--running'
                    : (item.status === 'error' ? 'pd-batch-pill pd-batch-pill--error' : 'pd-batch-pill');
                span.className = cls;
                const prefix = item.status === 'running' ? 'Checking: ' : (item.status === 'error' ? '✕ ' : '');
                span.textContent = `${prefix}${item.name}${item.error ? ` (${item.error})` : ''}`;
                pillsRow.appendChild(span);
            }
        });

        container.replaceChildren(header, pillsRow);
        container.classList.remove('hidden');
    }

    async runCloudBatchCheck(providerOverride = null) {
        if (!storageSync.isEnabled || this.isBatchRunning) return;

        const activeConn = providerOverride
            ? (storageSync.connections || []).find(c => c.provider === providerOverride && c.connected)
                || await storageSync.getPrimaryConnectedConnection()
            : await storageSync.getPrimaryConnectedConnection();

        if (!activeConn) {
            ui.showToast('Connect Google Drive, Dropbox, or OneDrive, or use Local Folder.', 'info');
            await storageSync.openModal();
            return;
        }

        const provider = activeConn.provider;
        const cardBtn = this.elements.headerSyncNowBtn;

        if (provider !== 'google_drive' && (!activeConn.input_folder_ref || !activeConn.output_folder_ref)) {
            await storageSync.openModal(provider);
            return;
        }

        let driveDecks = [];
        if (provider === 'google_drive') {
            try {
                if (cardBtn) {
                    cardBtn.disabled = true;
                    cardBtn.textContent = 'Opening Picker...';
                }
                driveDecks = await storageSync.getStagedOrPickGoogleDriveDecks();
            } catch (err) {
                if (cardBtn) {
                    cardBtn.disabled = false;
                    cardBtn.textContent = 'Check Decks';
                }
                if (err.status === 401 && err.data?.needs_reauth) {
                    ui.showToast('Google Drive authorization expired. Reconnecting...', 'info');
                    window.location.assign('/api/storage/google_drive/connect');
                    return;
                }
                ui.showToast('Unable to open Google Picker: ' + err.message, 'error');
                return;
            }
            if (!driveDecks.length) {
                if (cardBtn) {
                    cardBtn.disabled = false;
                    cardBtn.textContent = 'Check Decks';
                }
                return;
            }
        }

        ui.hideModal('cloud-storage-modal');
        this.isBatchRunning = true;
        let openedFirstInWorkstation = false;

        try {
            if (provider === 'google_drive') {
                this.batchQueue = driveDecks.map(d => ({
                    key: d.id,
                    name: d.name,
                    companyName: null,
                    status: 'queued',
                    analysisId: null,
                    error: null
                }));
                this._setWorkstationBatchLoading(true);

                for (let i = 0; i < driveDecks.length; i++) {
                    const deck = driveDecks[i];
                    this.batchQueue[i].status = 'running';
                    const stepLabel = `Checking ${i + 1} of ${driveDecks.length}`;
                    if (cardBtn) {
                        cardBtn.disabled = true;
                        cardBtn.textContent = `${stepLabel}…`;
                    }
                    storageSync.setBatchStatusText(`${stepLabel}: ${deck.name}`);
                    this.renderBatchProgressUI();

                    const res = await api.triggerStorageSync('google_drive', { file_ids: [deck.id] });
                    if (typeof res?.new_balance === 'number') {
                        ui.updateBalance(res.new_balance);
                    }

                    const rec = Array.isArray(res?.records) ? res.records[0] : null;
                    if (rec?.status === 'completed' && rec.analysis_result) {
                        this.batchQueue[i].companyName = rec.analysis_result.company_name || deck.name;
                        if (!openedFirstInWorkstation) {
                            openedFirstInWorkstation = true;
                            const analysisId = await this._openBatchDealInWorkstation(rec.analysis_result, {
                                exportedDocUrl: rec.exported_doc_url,
                                provider: 'google_drive'
                            });
                            this.batchQueue[i].analysisId = analysisId;
                        } else {
                            const archived = this.archiveBatchDeal(rec.analysis_result, {
                                exportedDocUrl: rec.exported_doc_url
                            });
                            this.batchQueue[i].analysisId = archived.analysis_id;
                        }
                        this.batchQueue[i].status = 'done';
                    } else {
                        this.batchQueue[i].status = 'error';
                        this.batchQueue[i].error = rec?.error_message
                            || (res?.skipped_duplicate ? 'Already checked' : 'Skipped');
                    }
                    this.renderBatchProgressUI();
                }

                storageSync.clearStagedDriveFiles();
            } else {
                this.batchQueue = [{
                    key: 'folder',
                    name: `${provider} folder`,
                    companyName: null,
                    status: 'running',
                    analysisId: null,
                    error: null
                }];
                if (cardBtn) {
                    cardBtn.disabled = true;
                    cardBtn.textContent = 'Checking…';
                }
                storageSync.setBatchStatusText('Checking folder decks…');
                this._setWorkstationBatchLoading(true);
                this.renderBatchProgressUI('Batch Check — Checking folder pitch decks…');

                const res = await api.triggerStorageSync(provider);
                if (typeof res?.new_balance === 'number') {
                    ui.updateBalance(res.new_balance);
                }

                const records = Array.isArray(res?.records) ? res.records : [];
                this.batchQueue = [];
                for (const rec of records) {
                    if (rec.status === 'completed' && rec.analysis_result) {
                        let analysisId;
                        if (!openedFirstInWorkstation) {
                            openedFirstInWorkstation = true;
                            analysisId = await this._openBatchDealInWorkstation(rec.analysis_result, {
                                exportedDocUrl: rec.exported_doc_url,
                                provider
                            });
                        } else {
                            const archived = this.archiveBatchDeal(rec.analysis_result, {
                                exportedDocUrl: rec.exported_doc_url
                            });
                            analysisId = archived.analysis_id;
                        }
                        this.batchQueue.push({
                            key: rec.external_file_id,
                            name: rec.file_name || 'Deck.pdf',
                            companyName: rec.analysis_result.company_name || rec.file_name,
                            status: 'done',
                            analysisId,
                            error: null
                        });
                    } else {
                        this.batchQueue.push({
                            key: rec.external_file_id || rec.file_name,
                            name: rec.file_name || 'Deck.pdf',
                            companyName: null,
                            status: 'error',
                            analysisId: null,
                            error: rec.error_message || rec.status || 'Skipped'
                        });
                    }
                }
                this.renderBatchProgressUI();
            }

            if (!openedFirstInWorkstation) {
                this._setWorkstationBatchLoading(false);
            }
            const doneCount = this.batchQueue.filter(i => i.status === 'done').length;
            const totalCount = this.batchQueue.length;
            await storageSync.loadConnections({ silent: true, focusProvider: provider });
            storageSync.setBatchStatusText(
                doneCount > 0 ? `Checked ${doneCount} of ${totalCount} deck${totalCount === 1 ? '' : 's'}` : 'No new decks checked'
            );
            ui.showToast(
                doneCount > 0
                    ? `Batch Check complete: ${doneCount} deck${doneCount === 1 ? '' : 's'} saved to Deal Archive.`
                    : 'No new pitch decks were checked.',
                doneCount > 0 ? 'success' : 'info'
            );
        } catch (error) {
            this._setWorkstationBatchLoading(false);
            if (error.status === 401 && (error.data?.needs_reauth || error.data?.needs_google_auth)) {
                ui.showToast('Cloud storage authorization expired. Reconnecting...', 'info');
                window.location.assign(`/api/storage/${encodeURIComponent(provider)}/connect`);
                return;
            }
            ui.showToast('Batch Check failed: ' + error.message, 'error');
        } finally {
            this.isBatchRunning = false;
            if (cardBtn) {
                cardBtn.disabled = false;
                cardBtn.textContent = 'Check Decks';
            }
        }
    }

    _readFileAsBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = (e) => {
                const raw = String(e.target?.result || '');
                const parts = raw.split(',');
                resolve(parts.length > 1 ? parts[1] : '');
            };
            reader.onerror = () => reject(new Error('Failed to read PDF file'));
            reader.readAsDataURL(file);
        });
    }

    async handleLocalFolderUpload(fileList) {
        if (!storageSync.isEnabled || this.isBatchRunning) return;

        const MAX_FILE_SIZE = 25 * 1024 * 1024;
        const MAX_LOCAL_BATCH = 10;
        const allPdfFiles = Array.from(fileList || [])
            .filter(f => this.isValidPdf(f) && !f.name.startsWith('._'))
            .sort((a, b) => a.name.localeCompare(b.name));

        if (allPdfFiles.length === 0) {
            ui.showToast('No PDF files found in the selected folder.', 'warning');
            return;
        }

        // Single PDF dropped/picked -> standard single-deck upload preview flow
        if (allPdfFiles.length === 1 && !fileList[0]?.webkitRelativePath) {
            await this.handleFileUpload(allPdfFiles[0]);
            return;
        }

        const pdfFiles = allPdfFiles.slice(0, MAX_LOCAL_BATCH);
        if (allPdfFiles.length > MAX_LOCAL_BATCH) {
            ui.showToast(`Folder contains ${allPdfFiles.length} PDFs — checking the first ${MAX_LOCAL_BATCH} in this batch.`, 'info');
        }

        this.isBatchRunning = true;
        const cardBtn = this.elements.headerSyncNowBtn;
        const localBtn = this.elements.localFolderBtn;
        const localConnBtn = this.elements.localFolderConnectedBtn;
        let openedFirstInWorkstation = false;

        this.batchQueue = pdfFiles.map((f, idx) => ({
            key: `${idx}-${f.name}`,
            name: f.name,
            companyName: null,
            status: 'queued',
            analysisId: null,
            error: null
        }));
        this._setWorkstationBatchLoading(true);
        this.renderBatchProgressUI();

        const activeConn = await storageSync.getPrimaryConnectedConnection();
        const canAutoExportCloud = Boolean(
            activeConn && (activeConn.provider === 'google_drive' || activeConn.output_folder_ref)
        );

        try {
            for (let i = 0; i < pdfFiles.length; i++) {
                const file = pdfFiles[i];
                this.batchQueue[i].status = 'running';
                const stepLabel = `Checking ${i + 1} of ${pdfFiles.length}`;
                if (cardBtn) {
                    cardBtn.disabled = true;
                    cardBtn.textContent = `${stepLabel}…`;
                }
                if (localBtn) localBtn.disabled = true;
                if (localConnBtn) localConnBtn.disabled = true;
                storageSync.setBatchStatusText(`${stepLabel}: ${file.name}`);
                this.renderBatchProgressUI();

                if (file.size === 0 || file.size > MAX_FILE_SIZE) {
                    this.batchQueue[i].status = 'error';
                    this.batchQueue[i].error = file.size === 0 ? 'Empty file' : 'Exceeds 25MB';
                    this.renderBatchProgressUI();
                    continue;
                }

                try {
                    const base64Data = await this._readFileAsBase64(file);
                    const result = await api.analyzePitchDeck(base64Data);
                    if (typeof result?.new_balance === 'number') {
                        ui.updateBalance(result.new_balance);
                    }

                    let exportedDocUrl = null;
                    if (canAutoExportCloud && activeConn) {
                        try {
                            const expRes = await api.exportPitchdeckCloudSync({
                                provider: activeConn.provider,
                                ...this.buildCloudExportPayload(result)
                            });
                            if (expRes?.memo_url?.startsWith('https://')) {
                                exportedDocUrl = expRes.memo_url;
                            }
                        } catch (cloudErr) {
                            console.warn('[Pitchdeck] Non-fatal cloud memo export during local batch:', cloudErr);
                        }
                    }

                    this.batchQueue[i].companyName = result.company_name || file.name;
                    if (!openedFirstInWorkstation) {
                        openedFirstInWorkstation = true;
                        const analysisId = await this._openBatchDealInWorkstation(result, {
                            file,
                            exportedDocUrl,
                            provider: activeConn?.provider || null
                        });
                        this.batchQueue[i].analysisId = analysisId;
                    } else {
                        const archived = this.archiveBatchDeal(result, { exportedDocUrl });
                        this.sessionBatchFiles.set(archived.analysis_id, file);
                        this.batchQueue[i].analysisId = archived.analysis_id;
                    }
                    this.batchQueue[i].status = 'done';
                } catch (deckErr) {
                    this.batchQueue[i].status = 'error';
                    this.batchQueue[i].error = deckErr.message || 'Check failed';
                    if (deckErr.status === 403 || deckErr.status === 402 || deckErr.status === 429) {
                        this.renderBatchProgressUI();
                        ui.showToast(deckErr.message || 'Batch stopped.', 'warning');
                        break;
                    }
                }
                this.renderBatchProgressUI();
            }

            if (!openedFirstInWorkstation) {
                this._setWorkstationBatchLoading(false);
            }
            const doneCount = this.batchQueue.filter(i => i.status === 'done').length;
            storageSync.setBatchStatusText(
                doneCount > 0 ? `Checked ${doneCount} of ${pdfFiles.length} deck${pdfFiles.length === 1 ? '' : 's'}` : 'Batch check finished'
            );
            ui.showToast(
                doneCount > 0
                    ? `Batch Check complete: ${doneCount} of ${pdfFiles.length} deck${pdfFiles.length === 1 ? '' : 's'} saved to Deal Archive.`
                    : 'No pitch decks could be checked.',
                doneCount > 0 ? 'success' : 'warning'
            );
        } finally {
            this.isBatchRunning = false;
            if (cardBtn) {
                cardBtn.disabled = false;
                cardBtn.textContent = 'Check Decks';
            }
            if (localBtn) localBtn.disabled = false;
            if (localConnBtn) localConnBtn.disabled = false;
        }
    }

    async resumePendingStorageExport(provider) {
        let rawPending = null;
        try {
            rawPending = sessionStorage.getItem(PENDING_STORAGE_ACTION_KEY);
            if (!rawPending) return false;
            sessionStorage.removeItem(PENDING_STORAGE_ACTION_KEY);
        } catch (err) {
            return false;
        }

        let pending = null;
        try {
            pending = JSON.parse(rawPending);
        } catch (err) {
            return false;
        }
        if (!pending || !pending.action) return false;

        if (!this.lastAnalysisResult && pending.analysis_id) {
            await this.loadDealFromArchive(pending.analysis_id);
        }
        if (!this.lastAnalysisResult) return false;

        if (pending.action === 'cloud-sync' || provider) {
            await this.exportCurrentDealToCloud();
            return true;
        }
        return false;
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
