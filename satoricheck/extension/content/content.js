/**
 * Content script — injected into all web pages.
 *
 * Minimal footprint: this script only listens for messages from
 * the background service worker. It does NOT access storage or
 * read the auth token (blocked by TRUSTED_CONTEXTS policy).
 *
 * Future: could add text selection highlighting or inline tooltips.
 */

// Listen for messages from the service worker
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.type === 'GET_SELECTION') {
        const selection = window.getSelection()?.toString().trim() || '';
        sendResponse({ text: selection });
    }
    return false;
});
