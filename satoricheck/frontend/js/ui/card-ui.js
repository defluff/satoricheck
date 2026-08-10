/**
 * Card UI Module
 * Handles fact-check cards in the feed
 */

class CardUI {
    constructor() {
        this.feedContainer = document.getElementById('feed-container');
        this.selectionTooltip = document.getElementById('selection-tooltip');
        this.cardCounter = 0;
    }

    createCard(claim, isPending = false) {
        const cardId = `card-${++this.cardCounter}`;

        // Remove feed placeholder
        const placeholder = this.feedContainer.querySelector('.feed-placeholder');
        if (placeholder) {
            placeholder.remove();
        }

        const card = document.createElement('div');
        card.className = `fact-check-card ${isPending ? 'pending' : ''}`;
        card.id = cardId;

        card.innerHTML = `
            <div class="card-header">
                <span class="verdict-badge PENDING">
                    ${isPending ? '<span class="loading-spinner"></span> Checking...' : 'Pending'}
                </span>
                <span class="card-meta">${new Date().toLocaleTimeString()}</span>
            </div>
            <p class="claim-text">"${this.escapeHtml(claim)}"</p>
            <div class="card-details"></div>
        `;

        // Add click handler for expanding (only when not pending)
        if (!isPending) {
            card.addEventListener('click', () => {
                if (!card.classList.contains('pending')) {
                    card.classList.toggle('expanded');
                }
            });
        }

        // Add to top of feed
        this.feedContainer.insertBefore(card, this.feedContainer.firstChild);

        return cardId;
    }

    updateCard(cardId, result) {
        const card = document.getElementById(cardId);
        if (!card) return;

        card.classList.remove('pending');

        const verdictBadge = card.querySelector('.verdict-badge');
        verdictBadge.className = `verdict-badge ${result.verdict}`;
        verdictBadge.textContent = result.verdict.replace(/_/g, ' ');

        // For NOT_A_CLAIM, don't add details
        if (result.verdict === 'NOT_A_CLAIM') {
            card.style.cursor = 'default';
            return;
        }

        // Create details container
        let detailsContainer = card.querySelector('.card-details');
        if (!detailsContainer) {
            detailsContainer = document.createElement('div');
            detailsContainer.className = 'card-details';
            card.appendChild(detailsContainer);
        }

        // Add Meta Analysis for Quote Claims (Strict Check)
        if (result.is_quote_claim === true && result.quote_attribution) {
            const metaTruthBox = document.createElement('div');
            metaTruthBox.className = 'meta-truth-box';

            const quoteStatus = result.quote_verified ? '✅ Verified' : (result.quote_verified === false ? '❌ Not Found' : '❓ Unverified');
            const metaVerdictClass = result.meta_truth_verdict ? result.meta_truth_verdict.toLowerCase() : 'unknown';

            metaTruthBox.innerHTML = `
                <div class="meta-truth-header">🔍 Meta Analysis</div>
                <div class="meta-truth-levels">
                    <div class="meta-level level-1">
                        <span class="level-label">Level 1: Quote</span>
                        <span class="level-result">${quoteStatus}</span>
                        ${result.quote_source ? `<span class="level-detail">${this.escapeHtml(result.quote_attribution)} — ${this.escapeHtml(result.quote_source)}</span>` : `<span class="level-detail">${this.escapeHtml(result.quote_attribution)}</span>`}
                    </div>
                    <div class="meta-arrow">→</div>
                    <div class="meta-level level-2">
                        <span class="level-label">Level 2: Statement</span>
                        <span class="level-result verdict-${metaVerdictClass}">${this.escapeHtml(result.meta_truth_verdict || result.verdict)}</span>
                        <span class="level-detail">Is the content of the quote true?</span>
                    </div>
                </div>
            `;
            detailsContainer.appendChild(metaTruthBox);
        }

        // Add Social Context (Grok)
        if (result.social && result.social.found) {
            const socialBox = document.createElement('div');
            socialBox.className = 'social-context-box';

            const sourceVerified = result.social.source_verified ? ' ✓' : '';
            const engagement = result.social.engagement
                ? `<span class="social-engagement">♥ ${result.social.engagement.likes || 0} 🔁 ${result.social.engagement.retweets || 0}</span>`
                : '';

            socialBox.innerHTML = DOMPurify.sanitize(`
                <div class="social-header">🐦 Social Context</div>
                <div class="social-content">
                    <div class="social-source">${result.social.source}${sourceVerified}</div>
                    <p class="social-text">"${result.social.text}"</p>
                    ${result.social.posted_at ? `<span class="social-date">${result.social.posted_at}</span>` : ''}
                    ${engagement}
                    ${result.social.url ? `<a href="${result.social.url}" target="_blank" rel="noopener noreferrer" class="social-link">View on X →</a>` : ''}
                </div>
                ${result.social.context ? `<p class="social-context-note">${result.social.context}</p>` : ''}
            `);
            detailsContainer.appendChild(socialBox);
        }

        // Add reliability badge
        if (result.source_reliability) {
            const reliability = document.createElement('span');
            reliability.className = `reliability-badge reliability-${result.source_reliability.toLowerCase()}`;
            reliability.innerHTML = DOMPurify.sanitize(`🛡️ Source Reliability: <strong>${result.source_reliability}</strong>`);
            detailsContainer.appendChild(reliability);
        }

        // Add explanation
        if (result.explanation) {
            const explanation = document.createElement('p');
            explanation.className = 'explanation-text';
            explanation.textContent = result.explanation;
            detailsContainer.appendChild(explanation);
        }

        // Add fallacy if detected
        if (result.fallacy) {
            const fallacy = document.createElement('div');
            fallacy.className = 'fallacy-tag';
            fallacy.textContent = `⚠️ ${result.fallacy}`;
            detailsContainer.appendChild(fallacy);
        }

        // Add sources - filter out invalid URLs
        if (result.sources && result.sources.length > 0) {
            const validSources = result.sources.filter(url => {
                if (!url || typeof url !== 'string') return false;
                return url.startsWith('http://') || url.startsWith('https://');
            });

            if (validSources.length > 0) {
                const sourcesDiv = document.createElement('div');
                const formatUrl = (url) => {
                    try {
                        const parsed = new URL(url);
                        const domain = parsed.hostname.replace('www.', '');
                        const path = parsed.pathname.substring(0, 10);
                        return domain.substring(0, 20) + (path.length > 1 ? path + '…' : '');
                    } catch {
                        return url.substring(0, 25) + '…';
                    }
                };
                sourcesDiv.innerHTML = DOMPurify.sanitize(`
                    <strong style="color: var(--color-text-secondary); font-size: 0.875rem;">Sources:</strong>
                    <ul class="sources-list">
                        ${validSources.map(url => `
                            <li><a href="${url}" target="_blank" rel="noopener noreferrer" title="${url}">${formatUrl(url)}</a></li>
                        `).join('')}
                    </ul>
                `);
                detailsContainer.appendChild(sourcesDiv);
            }
        }

        // Add share button if Smart Agent mode (single claim guaranteed)
        if (result.isSmartAgentMode) {
            const shareContainer = document.createElement('div');
            shareContainer.className = 'share-button-container';

            const shareButton = document.createElement('button');
            shareButton.className = 'share-button';
            shareButton.innerHTML = `<span class="share-button-icon">📤</span> Share Verdict`;

            // Store verdict data on button for share module
            shareButton.verdictData = {
                claim_text: card.querySelector('.claim-text')?.textContent?.replace(/^"|"$/g, '') || '',
                verdict: result.verdict,
                explanation: result.explanation
            };

            shareButton.addEventListener('click', (e) => {
                e.stopPropagation();
                if (window.shareModule) {
                    window.shareModule.showShareMenu(shareButton, shareButton.verdictData);
                }
            });

            shareContainer.appendChild(shareButton);
            detailsContainer.appendChild(shareContainer);
        }

        // Add click handler to toggle expansion
        card.style.cursor = 'pointer';
        card.addEventListener('click', () => {
            card.classList.toggle('expanded');
        });
    }

    showSelectionTooltip(x, y) {
        this.selectionTooltip.classList.remove('hidden');
        this.selectionTooltip.style.left = `${x}px`;
        this.selectionTooltip.style.top = `${y}px`;
    }

    hideSelectionTooltip() {
        this.selectionTooltip.classList.add('hidden');
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    removeCard(cardId) {
        const card = document.getElementById(cardId);
        if (card) {
            card.remove();
        }
    }

    // AI Detection Card Methods
    createAICard(text, isPending = false) {
        const cardId = `ai-card-${++this.cardCounter}`;

        // Remove feed placeholder
        const placeholder = this.feedContainer.querySelector('.feed-placeholder');
        if (placeholder) {
            placeholder.remove();
        }

        const card = document.createElement('div');
        card.className = `ai-detect-card ${isPending ? 'pending' : ''}`;
        card.id = cardId;

        // Truncate text for preview
        const previewText = text.length > 100 ? text.substring(0, 100) + '...' : text;

        card.innerHTML = `
            <div class="card-header">
                <span class="ai-badge">AI Detection</span>
                <span class="card-meta">${new Date().toLocaleTimeString()}</span>
            </div>
            <p class="claim-text">"${this.escapeHtml(previewText)}"</p>
            <div class="ai-result">
                ${isPending ? '<div class="loading-spinner"></div> Analyzing...' : ''}
            </div>
        `;

        // Add to top of feed
        this.feedContainer.insertBefore(card, this.feedContainer.firstChild);

        return cardId;
    }

    updateAICard(cardId, result) {
        const card = document.getElementById(cardId);
        if (!card) return;

        card.classList.remove('pending');

        const probability = result.ai_probability || 50;
        const confidence = result.confidence || 'LOW';

        // Determine color class based on probability
        let colorClass = 'ai-low';
        if (probability >= 70) colorClass = 'ai-high';
        else if (probability >= 40) colorClass = 'ai-medium';

        const resultContainer = card.querySelector('.ai-result');

        // Sanitize explanation - hide errors from users
        let explanation = result.explanation || '';
        const isError = explanation.toLowerCase().includes('failed') ||
            explanation.toLowerCase().includes('error') ||
            explanation.toLowerCase().includes('timeout');

        // For beta, always show a disclaimer
        const disclaimer = '<em style="color: var(--color-text-muted); font-size: 0.75rem;">AI detection can make errors.</em>';

        resultContainer.innerHTML = DOMPurify.sanitize(`
            <div class="ai-probability-container ${colorClass}">
                <div class="ai-probability-header">
                    <span class="ai-probability-value">${probability}%</span>
                    <span class="ai-probability-label">AI-Generated</span>
                    <span class="ai-confidence">${confidence}</span>
                </div>
                <div class="ai-probability-bar">
                    <div class="ai-probability-fill" style="width: ${probability}%"></div>
                </div>
            </div>
            
            <div class="ai-explanation">${isError ? disclaimer : explanation + '<br><br>' + disclaimer}</div>
            
            ${!isError && result.ai_indicators && result.ai_indicators.length > 0 ? `
                <div class="ai-indicators">
                    <strong>AI Indicators:</strong>
                    <ul>${result.ai_indicators.map(i => `<li>${this.escapeHtml(String(i))}</li>`).join('')}</ul>
                </div>
            ` : ''}
            
            ${!isError && result.human_indicators && result.human_indicators.length > 0 ? `
                <div class="human-indicators">
                    <strong>Human Indicators:</strong>
                    <ul>${result.human_indicators.map(i => `<li>${this.escapeHtml(String(i))}</li>`).join('')}</ul>
                </div>
            ` : ''}
        `);

        // Make expandable
        card.addEventListener('click', () => {
            card.classList.toggle('expanded');
        });
    }
}

export default new CardUI();

