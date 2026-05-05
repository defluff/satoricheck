/**
 * Share Module
 * Client-side image generation and social sharing for verdict cards
 * 
 * Privacy-first: All image generation happens in browser.
 * No verdict content is stored on server.
 */

// Verdict emoji mapping
const VERDICT_EMOJI = {
    'TRUE': '✅',
    'FALSE': '❌',
    'MISLEADING': '⚠️',
    'COULD_NOT_VERIFY': '❓',
    'NOT_A_CLAIM': 'ℹ️'
};

// Platform share intents
const SHARE_INTENTS = {
    'X': 'https://twitter.com/intent/tweet?text=',
    'LinkedIn': 'https://www.linkedin.com/sharing/share-offsite/?url='
};

/**
 * Prepare the hidden share card with verdict data
 * @param {Object} verdictData - The verdict result object
 */
function prepareShareCard(verdictData) {
    const container = document.getElementById('share-card-container');
    if (!container) {
        console.error('[Share] Share card container not found');
        return false;
    }

    // Sanitize all text content
    const claim = DOMPurify.sanitize(verdictData.claim || verdictData.claim_text || '', { ALLOWED_TAGS: [] });
    const verdict = verdictData.verdict || 'COULD_NOT_VERIFY';
    const explanation = DOMPurify.sanitize(verdictData.explanation || '', { ALLOWED_TAGS: [] });

    // Truncate claim if too long (max ~120 chars for 2 lines)
    const truncatedClaim = claim.length > 120 ? claim.substring(0, 117) + '...' : claim;

    // Truncate explanation to ~3 lines
    const truncatedExplanation = explanation.length > 250 ? explanation.substring(0, 247) + '...' : explanation;

    // Update the share card DOM
    const claimEl = document.getElementById('share-card-claim');
    const verdictEl = document.getElementById('share-card-verdict');
    const emojiEl = document.getElementById('share-card-emoji');
    const verdictTextEl = document.getElementById('share-card-verdict-text');
    const explanationEl = document.getElementById('share-card-explanation');

    if (claimEl) claimEl.textContent = truncatedClaim;
    if (verdictEl) verdictEl.className = `share-card-verdict ${verdict}`;
    if (emojiEl) emojiEl.textContent = VERDICT_EMOJI[verdict] || '❓';
    if (verdictTextEl) verdictTextEl.textContent = verdict.replace(/_/g, ' ');
    if (explanationEl) explanationEl.textContent = truncatedExplanation;

    return true;
}

/**
 * Handle share button click - main entry point
 * @param {Object} verdictData - The verdict result object  
 * @param {string} platform - 'X', 'LinkedIn', or 'Download'
 */
async function handleShare(verdictData, platform = 'Download') {
    console.log('[Share] Starting share:', platform);

    // Generate user-friendly filename: Authenix-VERDICT-YYYYMMDD.png
    const verdict = verdictData.verdict || 'Verdict';
    const date = new Date().toISOString().split('T')[0]; // YYYY-MM-DD
    const filename = `Authenix-${verdict}-${date}.png`;

    // 1. Prepare the share card with verdict data
    if (!prepareShareCard(verdictData)) {
        window.ui?.showToast('Unable to prepare share card', 'error');
        return { success: false, error: 'prepare_failed' };
    }

    const cardElement = document.getElementById('share-card');
    if (!cardElement) {
        console.error('[Share] Share card element not found');
        return { success: false, error: 'element_not_found' };
    }

    // 2. Wait for fonts with timeout (prevent garbled text)
    try {
        await Promise.race([
            document.fonts.ready,
            new Promise((_, reject) => setTimeout(() => reject('font_timeout'), 3000))
        ]);
    } catch (fontError) {
        console.warn('[Share] Font loading timed out, proceeding anyway');
    }

    // 3. Generate image with html-to-image
    let blob;
    try {
        // Check if html-to-image is available (loaded via CDN)
        if (typeof htmlToImage === 'undefined') {
            throw new Error('html-to-image library not loaded');
        }

        blob = await htmlToImage.toBlob(cardElement, {
            quality: 0.95,
            pixelRatio: 2,
            backgroundColor: '#0f0f23'
        });

        if (!blob) {
            throw new Error('Blob generation returned null');
        }

        // Check blob size (some platforms reject >5MB)
        if (blob.size > 5 * 1024 * 1024) {
            console.warn('[Share] Image too large, using lower quality');
            blob = await htmlToImage.toBlob(cardElement, {
                quality: 0.7,
                pixelRatio: 1,
                backgroundColor: '#0f0f23'
            });
        }

        console.log('[Share] Image generated:', blob.size, 'bytes');

    } catch (imageError) {
        console.error('[Share] Image generation failed:', imageError);
        window.ui?.showToast('Unable to generate image. Please try again.', 'error');
        return { success: false, error: 'image_generation_failed' };
    }

    // 4. Platform-specific sharing
    try {
        if (platform === 'X' || platform === 'LinkedIn') {
            // For social platforms: download image + open web intent
            await downloadBlob(blob, filename);
            openShareIntent(platform);
            window.ui?.showToast('Image downloaded. Attach it to your post!', 'info');
        } else if (platform === 'Download') {
            // Direct download only
            await downloadBlob(blob, filename);
            window.ui?.showToast('Image downloaded!', 'success');
        } else if (navigator.canShare && navigator.canShare({ files: [new File([blob], 'verdict.png', { type: 'image/png' })] })) {
            // Mobile Web Share API (for generic share)
            const file = new File([blob], filename, { type: 'image/png' });
            await navigator.share({
                files: [file],
                title: 'Authenix Verdict',
                text: 'Fact-checked with Authenix'
            });
            window.ui?.showToast('Shared!', 'success');
        } else {
            // Fallback: just download
            await downloadBlob(blob, filename);
            window.ui?.showToast('Image downloaded!', 'success');
        }
    } catch (shareError) {
        if (shareError.name === 'AbortError') {
            // User cancelled share sheet - not an error
            console.log('[Share] User cancelled');
            return { success: false, error: 'user_cancelled' };
        }
        console.error('[Share] Share failed:', shareError);
        // Fallback to download
        await downloadBlob(blob, 'authenix-verdict.png');
        window.ui?.showToast('Share failed. Image downloaded instead.', 'warning');
    }

    // 5. Fire-and-forget analytics (never blocks share)
    trackShare(platform).catch(() => { });

    return { success: true };
}

/**
 * Download blob as file
 */
async function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

/**
 * Open platform share intent (Twitter/LinkedIn web)
 */
function openShareIntent(platform) {
    const baseUrl = SHARE_INTENTS[platform];
    if (!baseUrl) {
        console.warn('[Share] Unknown platform:', platform);
        return;
    }

    const shareText = encodeURIComponent('Fact-checked with Authenix 🔍 https://authenix.ai');
    const url = baseUrl + shareText;

    const popup = window.open(url, '_blank', 'width=600,height=600');
    if (!popup) {
        window.ui?.showToast('Please enable popups to share directly', 'warning');
    }
}

/**
 * Fire-and-forget analytics tracking
 */
async function trackShare(platform) {
    await fetch('/api/analytics/share', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ platform })
    });
}

/**
 * Show share menu for a verdict card
 * @param {HTMLElement} button - The share button that was clicked
 * @param {Object} verdictData - The verdict data
 */
function showShareMenu(button, verdictData) {
    // Remove any existing menus
    document.querySelectorAll('.share-menu.active').forEach(m => m.classList.remove('active'));

    // Find or create menu
    let menu = button.parentElement.querySelector('.share-menu');
    if (!menu) {
        menu = document.createElement('div');
        menu.className = 'share-menu';
        menu.innerHTML = `
            <div class="share-menu-item" data-platform="X">
                <span class="share-menu-item-icon">𝕏</span>
                <span>Share to X</span>
            </div>
            <div class="share-menu-item" data-platform="LinkedIn">
                <span class="share-menu-item-icon">in</span>
                <span>Share to LinkedIn</span>
            </div>
            <div class="share-menu-item" data-platform="Download">
                <span class="share-menu-item-icon">⬇️</span>
                <span>Download Image</span>
            </div>
        `;
        button.parentElement.appendChild(menu);

        // Add click handlers
        menu.querySelectorAll('.share-menu-item').forEach(item => {
            item.addEventListener('click', async (e) => {
                e.stopPropagation();
                const platform = item.dataset.platform;

                // Show loading state on button
                const originalText = button.innerHTML;
                button.innerHTML = `<span class="loading-spinner"></span> Generating...`;
                button.disabled = true;

                // Close menu immediately
                menu.classList.remove('active');

                try {
                    await handleShare(verdictData, platform);
                } finally {
                    // Restore button
                    button.innerHTML = originalText;
                    button.disabled = false;
                }
            });
        });
    }

    // Toggle menu
    menu.classList.toggle('active');

    // Close on outside click
    const closeMenu = (e) => {
        if (!menu.contains(e.target) && e.target !== button) {
            menu.classList.remove('active');
            document.removeEventListener('click', closeMenu);
        }
    };
    setTimeout(() => document.addEventListener('click', closeMenu), 10);
}

// Export for use by other modules
window.shareModule = {
    handleShare,
    showShareMenu,
    prepareShareCard
};
