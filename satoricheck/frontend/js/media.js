/**
 * Media Authenticity & Stream Verification Module — Desktop Workstation
 * 
 * Capabilities:
 * - Direct file drop/upload and public media URL inspection.
 * - Supports images (JPEG, PNG, WebP), video (MP4, WebM, MOV), and audio (MP3, WAV, WebM, OGG, M4A).
 * - Instant in-browser preview player with click-to-seek timestamp navigation.
 * - Multi-layered forensic analysis: Physics, Bio, Temporal, Audio, Context, Compression, Metadata.
 * - Interactive Claims Desk: Extracts spoken falsifiable claims and verifies them via batch fact-checking.
 */

import ui from './ui.js';
import api from './api.js';

class MediaModule {
    constructor() {
        this.isActive = false;
        this.elements = {};

        this.currentFile = null;
        this.currentUrl = null;
        this.currentBlobUrl = null;
        this.mediaType = null; // 'video' | 'audio' | 'image'
        this.extractedClaims = [];
        this.verifiedClaimsMap = new Map();

        /** @type {AbortController|null} Active analysis request controller */
        this._analysisAbortController = null;
        this._analysisInProgress = false;
    }

    /**
     * Initialize DOM references and event listeners.
     */
    init() {
        this.elements = {
            // Navigation
            navMediaBtn: document.getElementById('nav-media-btn'),
            mediaView: document.getElementById('media-view'),

            // Tabs
            tabUpload: document.getElementById('ma-tab-upload'),
            tabUrl: document.getElementById('ma-tab-url'),
            uploadPanel: document.getElementById('ma-upload-panel'),
            urlPanel: document.getElementById('ma-url-panel'),

            // Inputs
            dropZone: document.getElementById('ma-drop-zone'),
            fileInput: document.getElementById('ma-file-input'),
            urlInput: document.getElementById('ma-url-input'),
            selectedFilename: document.getElementById('ma-selected-filename'),
            changeFileBtn: document.querySelector('.ma-file-selected-change'),
            analyseBtn: document.getElementById('ma-analyse-btn'),
            uploadAnalyseBtn: document.getElementById('ma-upload-analyse-btn'),

            // Preview Player
            previewCard: document.getElementById('ma-preview-card'),
            previewIcon: document.getElementById('ma-preview-icon'),
            previewFilename: document.getElementById('ma-preview-filename'),
            previewBadge: document.getElementById('ma-preview-badge'),
            player: document.getElementById('ma-player'),
            audioPlayer: document.getElementById('ma-audio-player'),
            imagePreview: document.getElementById('ma-image-preview'),
            youtubeContainer: document.getElementById('ma-youtube-container'),
            youtubeThumbnailWrapper: document.getElementById('ma-yt-thumbnail-wrapper'),
            youtubeThumb: document.getElementById('ma-yt-thumb'),
            youtubeExternalLink: document.getElementById('ma-yt-external-link'),
            youtubePlayer: document.getElementById('ma-youtube-player'),
            scannerBeam: document.getElementById('ma-scanner-beam'),

            // Loading & Progress
            loadingSection: document.getElementById('ma-loading-section'),
            loadingStep: document.getElementById('ma-loading-step'),

            // Results & Verdict
            resultSection: document.getElementById('ma-result-section'),
            resultPlaceholder: document.getElementById('ma-result-placeholder'),
            verdictCard: document.getElementById('ma-verdict-card'),
            verdictLabel: document.getElementById('ma-verdict-label'),
            verdictEmoji: document.getElementById('ma-verdict-emoji'),
            reasoningText: document.getElementById('ma-reasoning-text'),
            confidenceValue: document.getElementById('ma-confidence-value'),
            confidenceArc: document.getElementById('ma-confidence-arc'),

            // Claims Desk
            claimsDesk: document.getElementById('ma-claims-desk'),
            claimsList: document.getElementById('ma-claims-list'),
            claimsCountBadge: document.getElementById('ma-claims-count-badge'),
            verifyAllBtn: document.getElementById('ma-verify-all-btn'),
        };

        if (!this.elements.tabUpload) return; // Guard: media view not present

        this._bindEvents();
    }

    show() {
        this.isActive = true;
        if (this.elements.mediaView) {
            this.elements.mediaView.classList.remove('hidden');
        }
    }

    hide() {
        this.isActive = false;
        if (this.elements.mediaView) {
            this.elements.mediaView.classList.add('hidden');
        }
        this._pausePlayback();
        this._abortActiveAnalysis();
    }

    _bindEvents() {
        const { tabUpload, tabUrl, uploadPanel, urlPanel, dropZone, fileInput, urlInput, analyseBtn, changeFileBtn, verifyAllBtn } = this.elements;

        // --- Tabs ---
        tabUpload?.addEventListener('click', () => {
            tabUpload.classList.add('active');
            tabUrl.classList.remove('active');
            uploadPanel.classList.remove('hidden');
            urlPanel.classList.add('hidden');
            this._updateAnalyseBtnState();
        });

        tabUrl?.addEventListener('click', () => {
            tabUrl.classList.add('active');
            tabUpload.classList.remove('active');
            urlPanel.classList.remove('hidden');
            uploadPanel.classList.add('hidden');
            this._updateAnalyseBtnState();
        });

        // --- File input & Dropzone ---
        dropZone?.addEventListener('click', () => fileInput.click());
        dropZone?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') fileInput.click();
        });

        dropZone?.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZone.classList.add('drag-over');
        });

        dropZone?.addEventListener('dragleave', () => {
            dropZone.classList.remove('drag-over');
        });

        dropZone?.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZone.classList.remove('drag-over');
            const file = e.dataTransfer.files[0];
            if (file) this._handleFileSelected(file);
        });

        fileInput?.addEventListener('change', () => {
            const file = fileInput.files[0];
            if (file) this._handleFileSelected(file);
        });

        changeFileBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            this._resetFileSelection();
        });

        // --- URL Input ---
        urlInput?.addEventListener('input', () => {
            this.currentUrl = urlInput.value.trim();
            this._updateAnalyseBtnState();
            this._handleUrlPreview(this.currentUrl);
        });

        urlInput?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                if (urlInput.value.trim()) this.analyseMedia();
            }
        });

        // --- Analyse Actions ---
        analyseBtn?.addEventListener('click', () => this.analyseMedia());
        this.elements.uploadAnalyseBtn?.addEventListener('click', () => this.analyseMedia());

        // --- YouTube Player Activation ---
        this.elements.youtubeThumbnailWrapper?.addEventListener('click', () => {
            this._activateYouTubeIframe();
        });

        // --- Claims Desk Verification ---
        verifyAllBtn?.addEventListener('click', () => this.verifyAllClaims());
    }

    _handleFileSelected(file) {
        this.currentFile = file;
        this.currentUrl = null;

        // Clean up previous blob URL
        if (this.currentBlobUrl) {
            URL.revokeObjectURL(this.currentBlobUrl);
        }
        this.currentBlobUrl = URL.createObjectURL(file);

        // Update Dropzone UI
        this.elements.dropZone.classList.add('has-file');
        if (this.elements.selectedFilename) {
            this.elements.selectedFilename.textContent = file.name;
        }

        // Determine Media Type
        const mime = file.type || '';
        if (mime.startsWith('video/')) {
            this.mediaType = 'video';
        } else if (mime.startsWith('audio/')) {
            this.mediaType = 'audio';
        } else {
            this.mediaType = 'image';
        }

        this._setupPreviewPlayer(this.currentBlobUrl, file.name, this.mediaType);
        this._updateAnalyseBtnState();
    }

    _extractYouTubeId(url) {
        if (!url || typeof url !== 'string') return null;
        try {
            const parsed = new URL(url.trim());
            const hostname = parsed.hostname.toLowerCase().replace(/^www\./, '');
            if (hostname === 'youtube.com' || hostname === 'm.youtube.com') {
                if (parsed.pathname === '/watch') {
                    const v = parsed.searchParams.get('v');
                    if (v && /^[a-zA-Z0-9_-]{11}$/.test(v)) return v;
                } else if (parsed.pathname.startsWith('/embed/') || parsed.pathname.startsWith('/shorts/') || parsed.pathname.startsWith('/v/')) {
                    const parts = parsed.pathname.split('/').filter(Boolean);
                    if (parts.length >= 2 && /^[a-zA-Z0-9_-]{11}$/.test(parts[1])) return parts[1];
                }
            } else if (hostname === 'youtu.be') {
                const id = parsed.pathname.replace(/^\//, '').split('/')[0].split('?')[0];
                if (/^[a-zA-Z0-9_-]{11}$/.test(id)) return id;
            }
        } catch (_) {}
        return null;
    }

    _handleUrlPreview(url) {
        if (!url) {
            this._hidePreview();
            return;
        }

        const ytId = this._extractYouTubeId(url);
        if (ytId) {
            this.mediaType = 'youtube';
            this._setupPreviewPlayer(url, `YouTube (${ytId})`, 'youtube', ytId);
            return;
        }

        const lower = url.toLowerCase();
        let detectedType = 'image';
        if (lower.match(/\.(mp4|webm|mov|mkv)(\?.*)?$/)) {
            detectedType = 'video';
        } else if (lower.match(/\.(mp3|wav|ogg|m4a|aac)(\?.*)?$/)) {
            detectedType = 'audio';
        }

        this.mediaType = detectedType;
        const displayName = url.split('/').pop()?.split('?')[0] || 'Remote Media';
        this._setupPreviewPlayer(url, displayName, detectedType);
    }

    _setupPreviewPlayer(src, filename, type, ytId = null) {
        const { previewCard, previewFilename, previewBadge, previewIcon, player, audioPlayer, imagePreview, youtubeContainer, youtubeThumbnailWrapper, youtubeThumb, youtubeExternalLink, youtubePlayer } = this.elements;
        if (!previewCard) return;

        previewCard.classList.remove('hidden');
        previewFilename.textContent = filename;

        player.classList.add('hidden');
        audioPlayer.classList.add('hidden');
        imagePreview.classList.add('hidden');
        if (youtubeContainer) youtubeContainer.classList.add('hidden');
        if (youtubePlayer) {
            youtubePlayer.classList.add('hidden');
            youtubePlayer.src = '';
        }

        if (type === 'youtube' && ytId) {
            this.currentYtId = ytId;
            previewIcon.textContent = '▶️';
            previewBadge.textContent = 'YOUTUBE';
            if (youtubeContainer) youtubeContainer.classList.remove('hidden');
            if (youtubeThumbnailWrapper) youtubeThumbnailWrapper.classList.remove('hidden');
            if (youtubeThumb) youtubeThumb.src = `https://img.youtube.com/vi/${ytId}/hqdefault.jpg`;
            if (youtubeExternalLink) youtubeExternalLink.href = `https://www.youtube.com/watch?v=${ytId}`;
        } else if (type === 'video') {
            previewIcon.textContent = '🎬';
            previewBadge.textContent = 'VIDEO';
            player.src = src;
            player.classList.remove('hidden');
        } else if (type === 'audio') {
            previewIcon.textContent = '🎵';
            previewBadge.textContent = 'AUDIO';
            audioPlayer.src = src;
            audioPlayer.classList.remove('hidden');
        } else {
            previewIcon.textContent = '🖼️';
            previewBadge.textContent = 'IMAGE';
            imagePreview.src = src;
            imagePreview.classList.remove('hidden');
        }
    }

    _activateYouTubeIframe() {
        if (!this.currentYtId || !this.elements.youtubePlayer) return;
        this.elements.youtubeThumbnailWrapper?.classList.add('hidden');
        this.elements.youtubePlayer.src = `https://www.youtube-nocookie.com/embed/${this.currentYtId}?autoplay=1&enablejsapi=1`;
        this.elements.youtubePlayer.classList.remove('hidden');
    }

    _hidePreview() {
        this._pausePlayback();
        if (this.elements.previewCard) {
            this.elements.previewCard.classList.add('hidden');
        }
    }

    _pausePlayback() {
        if (this.elements.player) {
            this.elements.player.pause();
        }
        if (this.elements.audioPlayer) {
            this.elements.audioPlayer.pause();
        }
        if (this.elements.youtubePlayer && !this.elements.youtubePlayer.classList.contains('hidden')) {
            try {
                this.elements.youtubePlayer.contentWindow?.postMessage(JSON.stringify({
                    event: 'command',
                    func: 'pauseVideo',
                    args: []
                }), '*');
            } catch (_) {}
        }
    }

    _resetFileSelection() {
        if (this.currentBlobUrl) {
            URL.revokeObjectURL(this.currentBlobUrl);
            this.currentBlobUrl = null;
        }
        this.currentFile = null;
        if (this.elements.fileInput) this.elements.fileInput.value = '';
        if (this.elements.selectedFilename) this.elements.selectedFilename.textContent = '';
        this.elements.dropZone.classList.remove('has-file');
        this._hidePreview();
        this._updateAnalyseBtnState();
    }

    _updateAnalyseBtnState() {
        const hasFile = Boolean(this.currentFile);
        const hasUrl = Boolean(this.elements.urlInput?.value.trim());

        if (this.elements.uploadAnalyseBtn) {
            this.elements.uploadAnalyseBtn.disabled = !hasFile;
        }
        if (this.elements.analyseBtn) {
            this.elements.analyseBtn.disabled = !hasUrl;
        }
    }

    /**
     * Send media to backend authenticity pipeline.
     */
    async analyseMedia() {
        const { analyseBtn, uploadAnalyseBtn, tabUpload, resultPlaceholder, resultSection, loadingSection, loadingStep, scannerBeam } = this.elements;
        const isUpload = tabUpload?.classList.contains('active');

        // Abort any previous in-flight analysis
        this._abortActiveAnalysis();

        const abortController = new AbortController();
        this._analysisAbortController = abortController;
        this._analysisInProgress = true;

        const activeBtn = isUpload ? (uploadAnalyseBtn || analyseBtn) : analyseBtn;
        const originalText = activeBtn ? activeBtn.innerHTML : 'Analyse Media';

        if (activeBtn) {
            activeBtn.disabled = true;
            activeBtn.classList.add('is-loading');
            activeBtn.innerHTML = '<span class="loading-spinner"></span> Analysing Media...';
        }

        // Show explicit forensic workstation loading state
        resultPlaceholder?.classList.add('hidden');
        resultSection?.classList.add('hidden');
        loadingSection?.classList.remove('hidden');
        scannerBeam?.classList.remove('hidden');

        // Dynamic step progression ticker - clean & understated
        const steps = [
            "Checking audio and visual authenticity...",
            "Analyzing spoken dialogue and claims...",
            "Compiling verification signals..."
        ];
        let stepIdx = 0;
        if (loadingStep) loadingStep.textContent = steps[0];
        const stepInterval = setInterval(() => {
            stepIdx = (stepIdx + 1) % steps.length;
            if (loadingStep) {
                loadingStep.style.opacity = '0';
                setTimeout(() => {
                    if (loadingStep) {
                        loadingStep.textContent = steps[stepIdx];
                        loadingStep.style.opacity = '1';
                    }
                }, 150);
            }
        }, 2200);

        try {
            let response;
            if (isUpload) {
                if (!this.currentFile) throw new Error('Please select a media file.');
                response = await api.analyzeMedia(this.currentFile, abortController.signal);
            } else {
                const url = this.elements.urlInput.value.trim();
                if (!url) throw new Error('Please enter a public media URL.');
                response = await api.analyzeMediaUrl(url, abortController.signal);
            }

            if (!response.success) {
                throw new Error(response.error || 'Media analysis failed');
            }

            loadingSection?.classList.add('hidden');
            scannerBeam?.classList.add('hidden');
            this.renderResult(response.result);
            ui.showToast('Media analysis complete', 'success');

            // Update user balance in header if returned
            if (response.new_balance !== undefined) {
                const balEl = document.getElementById('user-balance');
                if (balEl) balEl.textContent = response.new_balance;
            }

        } catch (error) {
            // User-initiated abort (navigation away or explicit cancel): reset UI silently
            if (error.name === 'AbortError') {
                console.log('[Media] Analysis cancelled by user');
                loadingSection?.classList.add('hidden');
                scannerBeam?.classList.add('hidden');
                resultPlaceholder?.classList.remove('hidden');
                return;
            }
            loadingSection?.classList.add('hidden');
            scannerBeam?.classList.add('hidden');
            resultPlaceholder?.classList.remove('hidden');
            ui.showToast(error.message, 'error');
        } finally {
            clearInterval(stepInterval);
            this._analysisInProgress = false;
            this._analysisAbortController = null;
            loadingSection?.classList.add('hidden');
            scannerBeam?.classList.add('hidden');
            if (activeBtn) {
                activeBtn.disabled = false;
                activeBtn.classList.remove('is-loading');
                activeBtn.innerHTML = originalText;
            }
            this._updateAnalyseBtnState();
        }
    }

    /**
     * Abort any in-flight media analysis request.
     * Called on hide() (module navigation away) and before starting a new analysis.
     * Backend work already in progress will complete and persist results;
     * tokens are not refunded since the compute was consumed.
     */
    _abortActiveAnalysis() {
        if (this._analysisAbortController) {
            this._analysisAbortController.abort();
            this._analysisAbortController = null;
            this._analysisInProgress = false;
        }
    }

    /**
     * Render the forensic scorecards and Spoken Claims Desk.
     */
    renderResult(result) {
        if (!result) return;
        const { resultSection, resultPlaceholder } = this.elements;

        resultPlaceholder.classList.add('hidden');
        resultSection.classList.remove('hidden');

        // 1. Verdict Card
        const verdictMap = {
            'AI Generated':       { type: 'ai',          emoji: '🤖' },
            'Likely Manipulated': { type: 'manipulated', emoji: '⚠️' },
            'Appears Authentic':  { type: 'authentic',   emoji: '✅' }
        };

        const verdictInfo = verdictMap[result.verdict] || { type: 'manipulated', emoji: '❓' };
        this.elements.verdictCard.setAttribute('data-verdict', verdictInfo.type);
        this.elements.verdictLabel.textContent = result.verdict;
        this.elements.verdictEmoji.textContent = verdictInfo.emoji;

        // Reasoning Text (Sanitized)
        const reasoning = result.explanation || '';
        if (window.DOMPurify) {
            this.elements.reasoningText.innerHTML = DOMPurify.sanitize(reasoning);
        } else {
            this.elements.reasoningText.textContent = reasoning;
        }

        // Confidence Arc
        const conf = result.confidence || 0;
        this.elements.confidenceValue.textContent = `${conf}%`;
        this.elements.confidenceArc.style.setProperty('--arc-fill', `${conf}%`);

        let arcColor = 'var(--color-warning)';
        if (verdictInfo.type === 'ai') arcColor = 'var(--color-false)';
        if (verdictInfo.type === 'authentic' && conf > 70) arcColor = 'var(--color-success)';
        this.elements.confidenceArc.style.setProperty('--arc-color', arcColor);

        // 2. Criteria Strips (All 7 Criteria)
        const criteria = result.criteria || {};
        this._updateCriterion('physics',     criteria.physics);
        this._updateCriterion('bio',         criteria.bio);
        this._updateCriterion('temporal',    criteria.temporal);
        this._updateCriterion('audio',       criteria.audio);
        this._updateCriterion('context',     criteria.context);
        this._updateCriterion('compression', criteria.compression);
        this._updateCriterion('metadata',    criteria.metadata);

        // 3. Claims Desk (Spoken Claims Extraction)
        this.extractedClaims = result.claims || [];
        this.renderClaimsDesk(this.extractedClaims);

        // Smooth scroll to results
        resultSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    _updateCriterion(id, data) {
        const signalTag = document.getElementById(`ma-signal-${id}`);
        const bar = document.getElementById(`ma-bar-${id}`);
        const desc = document.getElementById(`ma-desc-${id}`);

        if (!signalTag || !data) return;

        const tagLower = (data.tag || '').toLowerCase();
        let signal = 'uncertain';
        if (tagLower.includes('high') || tagLower.includes('suspicious')) signal = 'suspicious';
        else if (tagLower.includes('low') || tagLower.includes('clean') || tagLower.includes('clear')) signal = 'clear';

        const score = data.score || 0;
        signalTag.setAttribute('data-signal', signal);
        signalTag.textContent = data.tag || 'Clean';

        if (bar) {
            bar.setAttribute('data-signal', signal);
            bar.style.width = `${score}%`;
            bar.style.setProperty('--fill', `${score}%`);
        }

        if (desc) {
            const detail = data.detail || '';
            if (window.DOMPurify) {
                desc.innerHTML = DOMPurify.sanitize(detail);
            } else {
                desc.textContent = detail;
            }
        }
    }

    /**
     * Render the Spoken Claims Desk with seekable timestamps.
     */
    renderClaimsDesk(claims) {
        const { claimsDesk, claimsList, claimsCountBadge, verifyAllBtn } = this.elements;
        if (!claimsDesk) return;

        if (!claims || claims.length === 0) {
            claimsDesk.classList.add('hidden');
            return;
        }

        claimsDesk.classList.remove('hidden');
        claimsCountBadge.textContent = `${claims.length} claim${claims.length === 1 ? '' : 's'}`;
        claimsList.innerHTML = '';
        verifyAllBtn.disabled = false;
        verifyAllBtn.innerHTML = '<span class="btn-text">⚡ Verify All Claims</span>';

        claims.forEach((item, index) => {
            const card = document.createElement('div');
            card.className = 'ma-claim-item';
            card.id = `ma-claim-${index}`;

            const timestamp = item.timestamp || '00:00';
            const seconds = item.timestamp_seconds !== undefined ? item.timestamp_seconds : this._parseTimestampToSeconds(timestamp);

            card.innerHTML = `
                <div class="ma-claim-top">
                    <button class="ma-timestamp-tag" data-seconds="${seconds}" title="Click to seek player to ${timestamp}">
                        ▶ ${timestamp}
                    </button>
                    <span class="ma-claim-status-pill" data-verdict="pending" id="ma-pill-${index}">PENDING</span>
                </div>
                <p class="ma-claim-text">${this._escapeHtml(item.claim)}</p>
                <div class="ma-claim-verdict-details hidden" id="ma-details-${index}"></div>
            `;

            // Click-to-seek functionality
            const tsBtn = card.querySelector('.ma-timestamp-tag');
            tsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.seekToTimestamp(seconds);
            });

            claimsList.appendChild(card);
        });
    }

    /**
     * Seek video or audio player to specific second.
     */
    seekToTimestamp(seconds) {
        if (typeof seconds !== 'number' || isNaN(seconds)) return;

        if (this.mediaType === 'youtube') {
            if (this.elements.youtubePlayer && !this.elements.youtubePlayer.classList.contains('hidden')) {
                try {
                    this.elements.youtubePlayer.contentWindow?.postMessage(JSON.stringify({
                        event: 'command',
                        func: 'seekTo',
                        args: [seconds, true]
                    }), '*');
                    this.elements.youtubePlayer.contentWindow?.postMessage(JSON.stringify({
                        event: 'command',
                        func: 'playVideo',
                        args: []
                    }), '*');
                } catch (_) {}
            } else if (this.currentYtId) {
                window.open(`https://www.youtube.com/watch?v=${this.currentYtId}&t=${Math.floor(seconds)}s`, '_blank', 'noopener,noreferrer');
            }
            this.elements.previewCard?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else if (this.mediaType === 'video' && this.elements.player) {
            this.elements.player.currentTime = seconds;
            this.elements.player.play().catch(() => {});
            this.elements.previewCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
        } else if (this.mediaType === 'audio' && this.elements.audioPlayer) {
            this.elements.audioPlayer.currentTime = seconds;
            this.elements.audioPlayer.play().catch(() => {});
            this.elements.previewCard.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }

    /**
     * Batch verify all extracted claims using existing factcheck endpoint.
     */
    async verifyAllClaims() {
        if (!this.extractedClaims || this.extractedClaims.length === 0) return;
        const { verifyAllBtn } = this.elements;

        const originalHtml = verifyAllBtn.innerHTML;
        verifyAllBtn.disabled = true;
        verifyAllBtn.innerHTML = '<span class="spinner"></span> Verifying Claims...';

        try {
            const claimTexts = this.extractedClaims.map(c => c.claim);
            const batchResponse = await api.analyzeBatch(claimTexts);

            const results = batchResponse.results || [];
            results.forEach((item, index) => {
                const resultData = item.result || item;
                const pill = document.getElementById(`ma-pill-${index}`);
                const details = document.getElementById(`ma-details-${index}`);

                if (!pill || !resultData) return;

                const verdict = (resultData.verdict || 'UNVERIFIED').toUpperCase();
                pill.textContent = verdict;

                let verdictKey = 'unverified';
                if (verdict === 'TRUE') verdictKey = 'true';
                else if (verdict === 'FALSE') verdictKey = 'false';
                else if (verdict.includes('MISLEADING')) verdictKey = 'misleading';

                pill.setAttribute('data-verdict', verdictKey);

                if (details) {
                    details.classList.remove('hidden');
                    const sources = (resultData.sources || []).map(src => {
                        const rawUrl = typeof src === 'string' ? src : (src.url || '');
                        if (!rawUrl || (!rawUrl.startsWith('http://') && !rawUrl.startsWith('https://'))) {
                            return '';
                        }
                        let hostname = 'Source';
                        try {
                            hostname = new URL(rawUrl).hostname;
                        } catch (_) {}
                        const displayName = typeof src === 'string' ? hostname : (src.title || src.domain || hostname);
                        return `<a href="${this._escapeHtml(rawUrl)}" target="_blank" rel="noopener noreferrer" class="ma-source-link">🔗 ${this._escapeHtml(displayName)}</a>`;
                    }).filter(Boolean).join('');

                    const explanation = resultData.explanation || '';
                    details.innerHTML = `
                        <p class="ma-claim-explanation">${this._escapeHtml(explanation)}</p>
                        ${sources ? `<div class="ma-claim-sources">${sources}</div>` : ''}
                    `;
                }
            });

            ui.showToast(`Verified ${results.length} claims`, 'success');
        } catch (error) {
            ui.showToast(`Batch claim verification failed: ${error.message}`, 'error');
        } finally {
            verifyAllBtn.disabled = false;
            verifyAllBtn.innerHTML = '✓ Claims Verified';
        }
    }

    _parseTimestampToSeconds(tsStr) {
        if (!tsStr || typeof tsStr !== 'string') return 0;
        const parts = tsStr.split(':').map(Number);
        if (parts.length === 2) {
            return (parts[0] * 60) + parts[1];
        }
        if (parts.length === 3) {
            return (parts[0] * 3600) + (parts[1] * 60) + parts[2];
        }
        return 0;
    }

    _escapeHtml(text) {
        if (!text) return '';
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }
}

export default new MediaModule();
