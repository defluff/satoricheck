/**
 * Fact Check Manager Module
 * Handles fact-checking logic and API communication
 */

import api from './api.js';
import ui from './ui.js';
import auth from './auth.js';

class FactCheckManager {
    constructor() {
        this.autoCheck = false;
        this.pendingChecks = new Set();
        this.checkedTexts = new Set(); // Track checked text blocks
        this.isAutoChecking = false;
        this.lastAutoCheckPosition = 0;
        this.sentenceHistory = []; // Store sentences for context window

        // Stop functionality state
        this.isProcessing = false;
        this.abortRequested = false;
    }

    setupEventListeners() {
        // Auto-check toggle
        ui.elements.autoCheckToggle.addEventListener('change', (e) => {
            this.autoCheck = e.target.checked;
            ui.showToast(
                this.autoCheck ? 'Auto-check enabled ✓' : 'Auto-check disabled',
                this.autoCheck ? 'success' : 'info'
            );
        });

        // Manual check button (serves as Check or Stop)
        ui.elements.checkNowBtn.addEventListener('click', () => {
            // Priority 1: Stop if processing
            if (this.isProcessing) {
                this.handleStop();
                return;
            }

            // Priority 2: Check based on mode
            const appMode = localStorage.getItem('analysisMode') || 'factcheck';

            if (appMode === 'aidetect') {
                this.handleAICheck();
            } else {
                this.handleManualCheck();
            }
        });

        // Enter key in editor triggers check (if auto-check enabled)
        ui.elements.transcriptContainer.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                // Default behavior: create new line

                if (this.autoCheck) {
                    setTimeout(() => {
                        this.handleAutoCheckFromTyping();
                    }, 10);
                }
            }
        });
    }

    async checkText(text, context = null) {
        if (!text || text.trim().length < 5) {
            return;
        }

        const trimmedText = text.trim();

        // Check if already pending
        if (this.pendingChecks.has(trimmedText)) {
            return;
        }

        this.pendingChecks.add(trimmedText);

        // Create pending card
        const cardId = ui.createCard(trimmedText, true);

        try {
            // Call API directly - Retries now handled in api.js based on 503/429 errors
            const response = await api.analyzeText(trimmedText, context);

            // Update card with results
            ui.updateCard(cardId, {
                ...response.result,
                isSmartAgentMode: true
            });
            this.checkedTexts.add(trimmedText);

            // Update balance
            ui.updateBalance(response.new_balance);

            // Show success toast
            const verdictEmoji = {
                'TRUE': '✅',
                'FALSE': '❌',
                'MISLEADING': '⚠️',
                'NOT_A_CLAIM': 'ℹ️',
                'COULD_NOT_VERIFY': '🔍'
            }[response.result.verdict] || '✓';

            ui.showToast(
                `${verdictEmoji} Check complete (${response.result.tokens_used} CP used)`,
                'success'
            );

        } catch (error) {
            // Update card with error
            ui.updateCard(cardId, {
                verdict: 'ERROR',
                explanation: error.message,
                fallacy: null,
                sources: []
            });

            ui.showToast('Fact-check failed: ' + error.message, 'error');

            if (error.message.includes('Insufficient')) {
                ui.showModal('buy-tokens-modal');
            }
        } finally {
            this.pendingChecks.delete(trimmedText);
        }
    }

    handleManualCheck() {
        const transcriptEl = ui.elements.transcriptContainer;

        // Get all text content
        const placeholder = transcriptEl.querySelector('.transcript-placeholder');
        let text = transcriptEl.textContent || transcriptEl.innerText || '';

        // Remove placeholder text if it exists
        if (placeholder && placeholder.textContent) {
            text = text.replace(placeholder.textContent, '').trim();
        }

        text = text.trim();

        if (!text || text.length === 0) {
            return;
        }

        // Check if entire text block was already checked
        if (this.checkedTexts.has(text)) {
            ui.showToast('This text has already been checked', 'info');
            return;
        }

        // Always use the Smart Agent pipeline: identify claims, then batch-verify
        this.handleSmartAgentCheck(text);
    }



    async handleSmartAgentCheck(text) {
        // Skip if already checked
        if (this.checkedTexts.has(text)) {
            ui.showToast('This text has already been checked', 'info');
            return;
        }

        this.isProcessing = true;
        this.abortRequested = false;
        ui.setCheckButtonMode('stop');

        try {
            let tempCardId = null;

            // Smart Agent: First identify claims, then check each one
            // Create a temporary card to show status
            tempCardId = ui.createCard("🧠 Smart Agent identifying claims...", true);

            const maxRetries = 2;
            let lastError = null;
            let identifiedClaims = null;

            // Step 1: Identification Phase (Not interruptible as it's one call)
            for (let attempt = 1; attempt <= maxRetries; attempt++) {
                try {
                    // Call backend to identify claims
                    const response = await api.identifyClaims(text);

                    if (response.claims) {
                        identifiedClaims = response.claims;
                        break; // Success
                    }
                } catch (error) {
                    lastError = error;
                    console.error(`Smart Agent attempt ${attempt} failed:`, error);
                    if (attempt < maxRetries) {
                        // Update temp card status
                        ui.updateCard(tempCardId, {
                            verdict: 'CHECKING...',
                            explanation: `Taking longer than usual... (Retrying ${attempt}/${maxRetries})`,
                            sources: []
                        });
                        await new Promise(resolve => setTimeout(resolve, 1000 * attempt));
                    }
                }
            }

            // Remove the temporary card - we will either replace it or show error
            if (tempCardId) {
                ui.removeCard(tempCardId);
            }

            if (!identifiedClaims) {
                // All retries exhausted - fall back to standard check
                console.error('Smart Agent failed identifiers:', lastError);
                ui.showToast('Smart Agent unavailable, using standard check', 'warning');
                await this.checkText(text, null);
                return;
            }

            // Step 2: Processing Phase (Interruptible)
            if (identifiedClaims.length > 0) {
                // Filter out already checked claims
                const uncheckedClaims = identifiedClaims.filter(claim => {
                    const trimmed = claim.trim();
                    return trimmed.length > 5 && !this.checkedTexts.has(trimmed);
                });

                if (uncheckedClaims.length === 0) {
                    ui.showToast('All identified claims already checked', 'info');
                    this.checkedTexts.add(text);
                    return;
                }

                ui.showToast(`🧠 Found ${uncheckedClaims.length} new claims to check`, 'success');

                // 2a. Create pending cards for ALL claims first (user sees what's coming)
                const cardMap = new Map(); // claim text -> cardId

                uncheckedClaims.forEach(claim => {
                    const cardId = ui.createCard(claim.trim(), true);
                    cardMap.set(claim.trim(), cardId);
                    this.checkedTexts.add(claim.trim()); // Optimistically mark as checked
                });

                // 2b. Pre-create context cache once (avoids redundant creation per micro-batch)
                let cacheName = null;
                if (text.length > 4000) {
                    try {
                        const cacheResponse = await api.request('/factcheck/create-context-cache', {
                            method: 'POST',
                            body: JSON.stringify({ text })
                        });
                        cacheName = cacheResponse.cache_name;
                        if (cacheName) {
                            console.info(`Pre-created context cache: ${cacheName}`);
                        }
                    } catch (cacheError) {
                        console.warn('Cache pre-creation failed, proceeding without:', cacheError);
                    }
                }

                // 2c. Progressive Micro-Batches: Process 3 claims at a time for faster feedback
                const MICRO_BATCH_SIZE = 3;
                let processedCount = 0;

                for (let i = 0; i < uncheckedClaims.length; i += MICRO_BATCH_SIZE) {
                    // Check for abort
                    if (this.abortRequested) {
                        ui.showToast('⏹️ Stopped. Completed checks are saved.', 'warning');
                        break;
                    }

                    const chunk = uncheckedClaims.slice(i, i + MICRO_BATCH_SIZE);
                    const batchNum = Math.floor(i / MICRO_BATCH_SIZE) + 1;
                    const totalBatches = Math.ceil(uncheckedClaims.length / MICRO_BATCH_SIZE);

                    try {
                        // Call batch API with pre-created cache_name
                        const batchBody = {
                            claims: chunk,
                            context: text  // Pass original text as context
                        };
                        if (cacheName) {
                            batchBody.cache_name = cacheName;
                        }

                        const response = await api.request('/factcheck/analyze-batch', {
                            method: 'POST',
                            body: JSON.stringify(batchBody)
                        });

                        if (response.success && response.results) {
                            // Update balance
                            if (response.new_balance !== undefined) {
                                ui.updateBalance(response.new_balance);
                            }

                            // Update cards IMMEDIATELY as results come in
                            response.results.forEach((res, idx) => {
                                const originalClaim = chunk[idx];
                                if (originalClaim) {
                                    const cardId = cardMap.get(originalClaim.trim());
                                    if (cardId) {
                                        ui.updateCard(cardId, {
                                            ...res,
                                            isSmartAgentMode: true
                                        });
                                    }
                                }
                                processedCount++;
                            });

                            // Show progress
                            ui.showToast(`✓ Batch ${batchNum}/${totalBatches} complete`, 'success');
                        }
                    } catch (error) {
                        console.error(`Micro-batch ${batchNum} failed:`, error);

                        // Mark this chunk's cards as error but continue with remaining
                        chunk.forEach(claim => {
                            const cardId = cardMap.get(claim.trim());
                            if (cardId) {
                                ui.updateCard(cardId, {
                                    verdict: 'ERROR',
                                    explanation: `Batch failed: ${error.message}`,
                                    sources: []
                                });
                            }
                        });

                        // Stop on auth/payment errors
                        if (error.status === 403 || error.status === 401) {
                            ui.showToast('Insufficient tokens. Purchase more to continue.', 'error');
                            break;
                        }
                    }
                }

                ui.showToast(`Verified ${processedCount} claims`, 'success');

                // Mark full text as processed only if we finished
                if (!this.abortRequested) {
                    this.checkedTexts.add(text);
                }
            } else {
                // No claims found, check as single block
                ui.showToast('No distinct claims found, checking as whole', 'info');
                await this.checkText(text, null);
            }

        } finally {
            this.isProcessing = false;
            ui.setCheckButtonMode('check');
        }

    }

    handleStop() {
        if (this.isProcessing) {
            this.abortRequested = true;
            ui.showToast('⏹️ Stopping after current check...', 'info');
            // Disable button immediately to give feedback
            const btn = document.getElementById('check-now-btn');
            if (btn) btn.disabled = true;
        }
    }

    handleAutoCheckFromTyping() {
        const transcriptEl = ui.elements.transcriptContainer;
        const placeholder = transcriptEl.querySelector('.transcript-placeholder');
        let text = transcriptEl.textContent || transcriptEl.innerText || '';

        if (placeholder && placeholder.textContent) {
            text = text.replace(placeholder.textContent, '').trim();
        }

        text = text.trim();
        if (!text) return;

        // AI detection mode: check full text (minimum 20 words required)
        const appMode = localStorage.getItem('analysisMode') || 'factcheck';
        if (appMode === 'aidetect') {
            const wordCount = text.split(/\s+/).length;
            if (wordCount >= 20) {
                this.handleAICheckWithText(text);
            }
            return;
        }

        // Fact-check mode: extract the last sentence typed and run smart agent on it
        const sentences = text.split(/(?<=[.!?])\s+/).filter(s => s.trim().length > 0);
        if (sentences.length === 0) return;

        const lastSentence = sentences[sentences.length - 1].trim();
        if (lastSentence.length < 10 || this.checkedTexts.has(lastSentence)) return;

        this.handleSmartAgentCheck(lastSentence);
    }

    async handleAutoCheck(chunkText) {
        // Called from audio input (both standard browser SpeechRecognition and Live Pro)
        if (!this.autoCheck) return;

        const transcriptEl = ui.elements.transcriptContainer;
        const placeholder = transcriptEl.querySelector('.transcript-placeholder');
        let fullText = transcriptEl.textContent || transcriptEl.innerText || '';

        if (placeholder && placeholder.textContent) {
            fullText = fullText.replace(placeholder.textContent, '').trim();
        }

        // AI detection mode: check full text
        const appMode = localStorage.getItem('analysisMode') || 'factcheck';
        if (appMode === 'aidetect') {
            const wordCount = fullText.split(/\s+/).length;
            if (wordCount >= 20) {
                this.handleAICheckWithText(fullText);
            }
            return;
        }

        // Fact-check mode: run smart agent on the last sentence
        const sentences = fullText.split(/(?<=[.!?])\s+/).filter(s => s.trim().length > 0);
        if (sentences.length === 0) return;

        const lastSentence = sentences[sentences.length - 1].trim();
        if (lastSentence.length < 10 || this.checkedTexts.has(lastSentence)) return;

        this.handleSmartAgentCheck(lastSentence);
    }

    /**
     * Handle AI detection check (when in AI Detect mode)
     */
    async handleAICheck() {
        const transcriptEl = ui.elements.transcriptContainer;

        // Get all text content
        const placeholder = transcriptEl.querySelector('.transcript-placeholder');
        let text = transcriptEl.textContent || transcriptEl.innerText || '';

        // Remove placeholder text if it exists
        if (placeholder && placeholder.textContent) {
            text = text.replace(placeholder.textContent, '').trim();
        }

        text = text.trim();

        if (!text || text.length === 0) {
            ui.showToast('Enter or paste some text to analyze', 'warning');
            return;
        }

        await this.handleAICheckWithText(text);
    }

    /**
     * Handle AI detection with provided text (for selection tooltip)
     */
    async handleAICheckWithText(text) {

        // Check word count (API requires minimum 20 words)
        const wordCount = text.split(/\s+/).length;
        if (wordCount < 20) {
            ui.showToast('Need at least 20 words for accurate AI detection', 'warning');
            return;
        }

        // Create pending card
        const cardId = ui.card.createAICard(text, true);

        try {
            const result = await api.analyzeAI(text);

            if (result.success) {
                ui.card.updateAICard(cardId, result);

                // Update token balance if changed
                if (result.new_balance !== undefined) {
                    ui.updateBalance(result.new_balance);
                }

                ui.showToast(`AI Detection: ${result.ai_probability}% AI-generated`, 'info');
            } else {
                throw new Error(result.error || 'AI detection failed');
            }
        } catch (error) {
            console.error('🤖 AI detection error:', error);
            // Show error in card
            ui.card.updateAICard(cardId, {
                ai_probability: 50,
                confidence: 'LOW',
                ai_indicators: [],
                human_indicators: [],
                explanation: error.message || 'Analysis failed. Please try again.'
            });
            ui.showToast('AI detection failed: ' + error.message, 'error');
        }
    }
}

export default new FactCheckManager();
