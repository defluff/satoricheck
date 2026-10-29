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

// Self-hosted PDF.js (see frontend/vendor/pdfjs-3.11.174/README.md for provenance)
const PDFJS_LIB_SRC = '/vendor/pdfjs-3.11.174/pdf.min.js';
const PDFJS_WORKER_SRC = '/vendor/pdfjs-3.11.174/pdf.worker.min.js';

const THESIS_STORAGE_KEY = 'authenix_pd_thesis_v1';
const ARCHIVE_STORAGE_KEY = 'authenix_pd_archive_v1';
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
            verifyPublicBtn: document.getElementById('pd-verify-public-btn'),
            checkAllBtn: document.getElementById('pd-check-all-btn'),
            filterTabs: document.getElementById('pd-claims-filter-tabs'),
            claimsCard: document.getElementById('pd-claims-card'),
            claimsHeaderTitle: document.getElementById('pd-claims-header-title'),
            claimsList: document.getElementById('pd-claims-list'),
            claimsPlaceholder: document.getElementById('pd-claims-placeholder'),

            // Feature A: Thesis Configuration & Scorecard
            thesisGate: document.getElementById('pd-thesis-gate'),
            thesisActivePills: document.getElementById('pd-thesis-active-pills'),
            thesisToggleBtn: document.getElementById('pd-thesis-toggle-btn'),
            thesisEditor: document.getElementById('pd-thesis-editor'),
            thesisGeoTags: document.getElementById('pd-thesis-geo-tags'),
            thesisSectorTags: document.getElementById('pd-thesis-sector-tags'),
            thesisStageTags: document.getElementById('pd-thesis-stage-tags'),
            thesisGeoInput: document.getElementById('pd-thesis-geo-input'),
            thesisGeoAddBtn: document.getElementById('pd-thesis-geo-add-btn'),
            thesisSectorInput: document.getElementById('pd-thesis-sector-input'),
            thesisSectorAddBtn: document.getElementById('pd-thesis-sector-add-btn'),
            thesisScorecard: document.getElementById('pd-thesis-scorecard'),
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
            printReportContainer: document.getElementById('pd-print-report')
        };

        if (!this.elements.pitchdeckView) {
            console.warn('[Pitchdeck] Pitchdeck view not found in DOM');
            return;
        }

        // Configure PDF.js worker if available
        this.configurePdfWorker();

        this.renderThesisConfigUI();
        this.renderArchiveListUI();
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
            this.elements.thesisToggleBtn.textContent = isHidden ? '⚙️ Configure Thesis' : '✓ Done';
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
    }

    /**
     * Load persistent investor thesis configuration from localStorage
     */
    loadThesisConfig() {
        const defaultConfig = {
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
     * Render the Thesis Configuration bar summary pills and tag buttons
     */
    renderThesisConfigUI() {
        const { thesisActivePills, thesisGeoTags, thesisSectorTags, thesisStageTags } = this.elements;

        // 1. Render active mandate summary pills in header bar
        if (thesisActivePills) {
            const { geographies, sectors, stages } = this.thesisConfig;
            const totalSelected = geographies.length + sectors.length + stages.length;

            if (totalSelected === 0) {
                thesisActivePills.innerHTML = `<span class="pd-thesis-empty-hint">No mandate filters active — click Configure Thesis to set target Geography, Sector &amp; Stage</span>`;
            } else {
                const groups = [];
                if (geographies.length > 0) {
                    groups.push(`<span class="pd-mandate-chip"><strong>Geo:</strong> ${DOMPurify.sanitize(geographies.join(', '))}</span>`);
                }
                if (sectors.length > 0) {
                    groups.push(`<span class="pd-mandate-chip"><strong>Sector:</strong> ${DOMPurify.sanitize(sectors.join(', '))}</span>`);
                }
                if (stages.length > 0) {
                    groups.push(`<span class="pd-mandate-chip"><strong>Stage:</strong> ${DOMPurify.sanitize(stages.join(', '))}</span>`);
                }
                thesisActivePills.innerHTML = groups.join('');
            }
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

        const alignment = this.evaluateThesisAlignment(result);
        this.currentThesisAlignment = alignment;

        const applyDimensionBadge = (valEl, badgeEl, dimData) => {
            if (valEl) valEl.textContent = dimData.extracted || 'Unclear';
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
            this.elements.generateBtn.textContent = '⚡ Analyze Deck with AI';
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
     * Verify a batch of claims in a single API call (up to 10 claims per batch)
     */
    async verifyClaimsBatch(claimsToVerify) {
        if (!claimsToVerify || claimsToVerify.length === 0) return;

        const BATCH_SIZE = 10;
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

    syncCurrentDealToArchive(showIndicator = false) {
        if (!this.lastAnalysisResult || !this.currentAnalysisId) return;

        const r = this.lastAnalysisResult;
        const retainedClaims = (this.extractedClaims || []).map(c => ({
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

        const archiveRecord = {
            analysis_id: this.currentAnalysisId,
            company_name: r.company_name || 'Unnamed Startup',
            analyzed_at: this.currentAnalyzedAt || new Date().toISOString(),
            investor_decision: this.currentDecision || 'pending',
            investor_notes: this.currentNotes || '',
            thesis_alignment: this.currentThesisAlignment || {
                geography: { extracted: r.startup_profile?.geography || 'Not Disclosed', status: 'partial' },
                sector: { extracted: r.startup_profile?.sector || r.sector || 'Not Disclosed', status: 'partial' },
                stage: { extracted: r.startup_profile?.stage || 'Not Disclosed', status: 'partial' }
            },
            executive_summary: r.summary || '',
            usp: r.usp || '',
            industry: r.industry || '',
            sector: r.sector || '',
            market_size: r.market_size || '',
            competition: r.competition || [],
            startup_profile: r.startup_profile || {},
            vc_metrics: r.vc_metrics || {},
            red_flags: r.red_flags || [],
            retained_claims: retainedClaims
        };

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

    renderArchiveListUI() {
        if (this.elements.archiveCount) {
            this.elements.archiveCount.textContent = String(this.archivedDeals.length);
        }
        const listEl = this.elements.archiveList;
        if (!listEl) return;

        if (this.archivedDeals.length === 0) {
            listEl.innerHTML = `<div class="pd-archive-empty">No archived deals yet. Analyze a pitch deck to automatically log its lightweight Deal Brief here (zero PDF storage).</div>`;
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
            market_size: entry.market_size || 'Not specified',
            competition: entry.competition || [],
            startup_profile: entry.startup_profile || {
                geography: entry.thesis_alignment?.geography?.extracted || 'Not Disclosed',
                geography_tags: [],
                sector: entry.thesis_alignment?.sector?.extracted || 'Not Disclosed',
                stage: entry.thesis_alignment?.stage?.extracted || 'Not Disclosed'
            },
            vc_metrics: entry.vc_metrics || {},
            red_flags: entry.red_flags || [],
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

        // Clear slide canvas since raw PDF was purged (Zero-File Archive)
        this.pdfDoc = null;
        this.elements.slideCanvas?.classList.add('hidden');
        if (this.elements.canvasPlaceholder) {
            this.elements.canvasPlaceholder.classList.remove('hidden');
            // localStorage is user-editable, so sanitize before interpolating into HTML
            this.elements.canvasPlaceholder.innerHTML = DOMPurify.sanitize(`
                <div class="pd-placeholder-icon">🗂️</div>
                <p>Archived Deal Brief (${entry.company_name})</p>
                <p style="font-size: 0.78rem; opacity: 0.75; margin-top: 4px;">Zero-File Archive: Raw PDF was purged after analysis for privacy.</p>
            `);
        }

        await this.displayResults(reconstructed, { fromArchive: true, archiveEntry: entry });
        this.elements.archiveDrawer?.classList.add('hidden');
        this.elements.archiveToggleBtn?.setAttribute('aria-expanded', 'false');
        ui.showToast(`Loaded archived Deal Brief for ${entry.company_name}`, 'info');
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

    compileDealBriefMarkdown() {
        const r = this.lastAnalysisResult || {};
        const companyName = r.company_name || 'Unknown Startup';
        const analyzedDate = this.currentAnalyzedAt
            ? new Date(this.currentAnalyzedAt).toISOString().split('T')[0]
            : new Date().toISOString().split('T')[0];
        const decisionStr = DECISION_LABELS[this.currentDecision] || 'Pending';
        const notesStr = (this.currentNotes || '').trim() || 'None';

        const align = this.currentThesisAlignment || {
            geography: { extracted: r.startup_profile?.geography || 'Not Disclosed', status: 'partial' },
            sector: { extracted: r.startup_profile?.sector || r.sector || 'Not Disclosed', status: 'partial' },
            stage: { extracted: r.startup_profile?.stage || 'Not Disclosed', status: 'partial' }
        };

        const highClaims = (this.extractedClaims || []).filter(c => this._getClaimRiskLevel(c) === 'high');
        const medClaims = (this.extractedClaims || []).filter(c => this._getClaimRiskLevel(c) === 'medium');

        const formatHighClaims = highClaims.length > 0
            ? highClaims.map(c => {
                const finding = c.verificationResult?.verification_finding
                    || c.verificationResult?.explanation
                    || c.verification_finding
                    || 'Contradicted or flagged as high risk.';
                const question = c.verificationResult?.suggested_question
                    || c.suggested_question
                    || 'Can you provide primary source documentation for this claim?';
                return `* **[Slide ${c.slide_number || 1}]** "${c.claim}"\n  * *Finding:* ${finding}\n  * *Ask Founder:* ${question}`;
            }).join('\n')
            : '* None identified.';

        const formatMedClaims = medClaims.length > 0
            ? medClaims.map(c => {
                const context = c.verificationResult?.verification_finding
                    || c.verificationResult?.explanation
                    || c.verification_finding
                    || 'Unverifiable publicly. Requires internal Data Room proof.';
                const question = c.verificationResult?.suggested_question
                    || c.suggested_question
                    || 'Please share underlying cohort or financial schedules in the Data Room.';
                return `* **[Slide ${c.slide_number || 1}]** "${c.claim}"\n  * *Context:* ${context}\n  * *Ask Founder:* ${question}`;
            }).join('\n')
            : '* None identified.';

        return [
            `# Authenix Deal Brief: ${companyName}`,
            `**Date Analyzed:** ${analyzedDate}`,
            `**Decision:** ${decisionStr}`,
            `**Investor Notes:** ${notesStr}`,
            ``,
            `## Thesis Alignment`,
            `* **Geography:** ${align.geography.extracted} (${align.geography.status})`,
            `* **Sector:** ${align.sector.extracted} (${align.sector.status})`,
            `* **Stage:** ${align.stage.extracted} (${align.stage.status})`,
            ``,
            `## Executive Summary`,
            `${r.summary || 'No summary available.'}`,
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

        const align = this.currentThesisAlignment || {
            geography: { extracted: r.startup_profile?.geography || 'Not Disclosed', status: 'partial' },
            sector: { extracted: r.startup_profile?.sector || r.sector || 'Not Disclosed', status: 'partial' },
            stage: { extracted: r.startup_profile?.stage || 'Not Disclosed', status: 'partial' }
        };

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
            <div class="pd-print-thesis-row">
                <div class="pd-print-thesis-pill"><strong>Geography:</strong> ${align.geography.extracted} (${align.geography.status.toUpperCase()})</div>
                <div class="pd-print-thesis-pill"><strong>Sector:</strong> ${align.sector.extracted} (${align.sector.status.toUpperCase()})</div>
                <div class="pd-print-thesis-pill"><strong>Stage:</strong> ${align.stage.extracted} (${align.stage.status.toUpperCase()})</div>
            </div>
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
