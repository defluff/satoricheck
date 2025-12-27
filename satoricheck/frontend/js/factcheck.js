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
        this.smartAgent = false;
        this.smartAgentNotified = false; // Track if user was notified this session
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

        // Smart Agent toggle - simple on/off with tooltip for explanation
        const smartAgentToggle = document.getElementById('smart-agent-toggle');
        if (smartAgentToggle) {
            smartAgentToggle.addEventListener('change', (e) => {
                this.smartAgent = e.target.checked;
                ui.showToast(
                    this.smartAgent ? 'Smart Agent enabled 🧠 (2x tokens)' : 'Smart Agent disabled',
                    this.smartAgent ? 'success' : 'info'
                );
            });
        }

        // Manual check button (serves as Check or Stop)
        ui.elements.checkNowBtn.addEventListener('click', () => {
            // Priority 1: Stop if processing
            if (this.isProcessing) {
                console.log('⏹ Stop requested');
                this.handleStop();
                return;
            }

            // Priority 2: Check based on mode
            const appMode = localStorage.getItem('analysisMode') || 'factcheck';
            console.log('🔘 Check button clicked, mode:', appMode);

            if (appMode === 'aidetect') {
                console.log('🤖 Routing to AI detection...');
                this.handleAICheck();
            } else {
                console.log('✓ Routing to fact check...');
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
            // Prepare request with optional context and smart agent flag
            const requestData = {
                text: trimmedText,
                context: context,
                smart_agent: this.smartAgent
            };

            // Call API
            const response = await api.analyzeText(trimmedText, context, this.smartAgent);

            // Update card with results
            ui.updateCard(cardId, response.result);
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

        // If Smart Agent is enabled, use the backend to identify claims first
        if (this.smartAgent) {
            this.handleSmartAgentCheck(text);
            return;
        }

        // STANDARD MODE: Find unchecked sentences and batch them
        const sentences = text.split(/(?<=[.!?])\s+/).filter(s => s.trim().length > 0);

        const uncheckedSentences = [];
        for (const sentence of sentences) {
            const trimmed = sentence.trim();
            if (trimmed.length >= 10 && !this.checkedTexts.has(trimmed)) {
                uncheckedSentences.push(trimmed);
            }
        }

        if (uncheckedSentences.length === 0) {
            ui.showToast('All text already checked', 'info');
            return;
        }

        // Batch into chunks of max 10 sentences each
        const MAX_SENTENCES_PER_BATCH = 10;
        const batches = [];

        for (let i = 0; i < uncheckedSentences.length; i += MAX_SENTENCES_PER_BATCH) {
            const batch = uncheckedSentences.slice(i, i + MAX_SENTENCES_PER_BATCH);
            batches.push(batch);
        }

        // Show info about what's being checked
        const batchInfo = batches.length > 1
            ? `Checking ${uncheckedSentences.length} sentences in ${batches.length} batches`
            : `Checking ${uncheckedSentences.length} new sentence(s)`;
        ui.showToast(batchInfo, 'info');

        // Process batches sequentially
        this.processBatches(batches);
    }

    async processBatches(batches) {
        this.isProcessing = true;
        this.abortRequested = false;
        ui.setCheckButtonMode('stop');

        try {
            for (let i = 0; i < batches.length; i++) {
                // Check if stop requested
                if (this.abortRequested) {
                    ui.showToast('⏹️ Stopped. Tokens used for completed checks are non-refundable.', 'warning');
                    break;
                }

                const batch = batches[i];
                const batchedText = batch.join(' ');

                try {
                    await this.checkText(batchedText, null);

                    // Mark sentences as checked after successful check
                    batch.forEach(s => this.checkedTexts.add(s));

                } catch (error) {
                    ui.showToast(`Batch ${i + 1} failed: ${error.message}`, 'error');
                    // Stop processing remaining batches if one fails (e.g., insufficient CP)
                    break;
                }
            }
        } finally {
            this.isProcessing = false;
            ui.setCheckButtonMode('check');
        }
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
            // Smart Agent: First identify claims, then check each one
            ui.showToast('🧠 Smart Agent analyzing...', 'info');

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
                        ui.showToast(`🧠 Retrying... (${attempt}/${maxRetries})`, 'warning');
                        await new Promise(resolve => setTimeout(resolve, 1000 * attempt));
                    }
                }
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

                // Check each claim individually
                for (const claim of uncheckedClaims) {
                    // Check stop flag
                    if (this.abortRequested) {
                        ui.showToast('⏹️ Stopped. Tokens used for completed checks are non-refundable.', 'warning');
                        break;
                    }

                    await this.checkText(claim.trim(), null);
                }

                // Mark full text as processed only if we finished
                if (!this.abortRequested) {
                    this.checkedTexts.add(text);
                }
            } else {
                // No claims found, check as single block
                ui.showToast('🧠 No distinct claims found, checking as whole', 'info');
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
        // OPTION 2: Context Window
        // When auto-checking from typing, use sentence-based with context

        const transcriptEl = ui.elements.transcriptContainer;
        const placeholder = transcriptEl.querySelector('.transcript-placeholder');
        let text = transcriptEl.textContent || transcriptEl.innerText || '';

        if (placeholder && placeholder.textContent) {
            text = text.replace(placeholder.textContent, '').trim();
        }

        text = text.trim();
        if (!text) return;

        // Check analysis mode
        const appMode = localStorage.getItem('analysisMode') || 'factcheck';

        // If in AI detection mode, just run AI check on the full text
        if (appMode === 'aidetect') {
            // For AI detection, check the entire text (minimum 20 words required)
            const wordCount = text.split(/\s+/).length;
            if (wordCount >= 20) {
                this.handleAICheckWithText(text);
            }
            return;
        }

        // Standard fact-check mode: extract sentences
        const sentences = text.split(/(?<=[.!?])\s+/).filter(s => s.trim().length > 0);

        if (sentences.length === 0) return;

        // Get the last sentence (most recent)
        const lastSentence = sentences[sentences.length - 1].trim();

        // Skip if too short or already checked
        if (lastSentence.length < 10 || this.checkedTexts.has(lastSentence)) {
            return;
        }

        // Build context from previous 2 sentences
        let context = null;
        if (sentences.length > 1) {
            const contextSentences = sentences.slice(Math.max(0, sentences.length - 3), sentences.length - 1);
            context = contextSentences.join(' ');
        }

        // Check with context
        this.checkText(lastSentence, context);
    }

    async handleAutoCheck(chunkText) {
        // Called from audio input
        if (!this.autoCheck) return;

        // For audio, use the same context window approach
        const transcriptEl = ui.elements.transcriptContainer;
        const placeholder = transcriptEl.querySelector('.transcript-placeholder');
        let fullText = transcriptEl.textContent || transcriptEl.innerText || '';

        if (placeholder && placeholder.textContent) {
            fullText = fullText.replace(placeholder.textContent, '').trim();
        }

        // Check analysis mode
        const appMode = localStorage.getItem('analysisMode') || 'factcheck';

        // If in AI detection mode, check the entire text
        if (appMode === 'aidetect') {
            const wordCount = fullText.split(/\s+/).length;
            if (wordCount >= 20) {
                this.handleAICheckWithText(fullText);
            }
            return;
        }

        // Standard fact-check mode: get sentences for context
        const sentences = fullText.split(/(?<=[.!?])\s+/).filter(s => s.trim().length > 0);

        if (sentences.length === 0) return;

        // Get last sentence
        const lastSentence = sentences[sentences.length - 1].trim();

        if (lastSentence.length < 10 || this.checkedTexts.has(lastSentence)) {
            return;
        }

        // Build context
        let context = null;
        if (sentences.length > 1) {
            const contextSentences = sentences.slice(Math.max(0, sentences.length - 3), sentences.length - 1);
            context = contextSentences.join(' ');
        }

        this.checkText(lastSentence, context);
    }

    /**
     * Handle AI detection check (when in AI Detect mode)
     */
    async handleAICheck() {
        console.log('🤖 handleAICheck called');
        const transcriptEl = ui.elements.transcriptContainer;

        // Get all text content
        const placeholder = transcriptEl.querySelector('.transcript-placeholder');
        let text = transcriptEl.textContent || transcriptEl.innerText || '';

        // Remove placeholder text if it exists
        if (placeholder && placeholder.textContent) {
            text = text.replace(placeholder.textContent, '').trim();
        }

        text = text.trim();
        console.log('🤖 Text to analyze:', text.substring(0, 50) + '...');

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
        console.log('🤖 handleAICheckWithText called with:', text.substring(0, 50) + '...');

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
