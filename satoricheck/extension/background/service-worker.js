/**
 * Background service worker — extension entry point.
 *
 * Responsibilities:
 *   - Register context menu item ("Fact-check with Authenix")
 *   - Route context menu clicks to the side panel
 *   - Handle messages from popup, side panel, and content script
 *   - Restrict storage access to trusted contexts
 */

// --- Security: restrict storage to extension pages only ---
// Content scripts cannot read the auth token.
chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' })
    .catch(() => {
        // Fallback for older Chromium versions where this API may not exist
    });

// --- Side panel config ---
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false })
    .catch(() => {
        // Not critical — popup is the primary action
    });

// --- Context menu ---
chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.create({
        id: 'satori-factcheck',
        title: 'Fact-check with Authenix',
        contexts: ['selection'],
    });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    if (info.menuItemId !== 'satori-factcheck' || !tab?.id) return;

    const selectedText = (info.selectionText || '').trim().slice(0, 10000);
    if (!selectedText) return;

    // Persist for READY handshake — storage.session survives SW termination
    await chrome.storage.session.set({ pendingSelection: selectedText });

    try {
        await chrome.sidePanel.open({ tabId: tab.id });

        // Fast path: if panel is already open its listener receives this
        // directly. On success, clear the session store to avoid duplicate
        // delivery via READY.
        chrome.runtime.sendMessage({
            type: 'FACTCHECK_SELECTION',
            text: selectedText,
        }).then(() => {
            chrome.storage.session.remove('pendingSelection');
        }).catch(() => {
            // Panel not ready yet — SIDE_PANEL_READY handshake will deliver
        });
    } catch (error) {
        console.error('[Authenix] Failed to open side panel:', error);
        pendingSelection = null;
    }
});

// --- Message handler ---
const ALLOWED_TYPES = ['FACTCHECK_SELECTION', 'OPEN_SIDE_PANEL', 'SIDE_PANEL_READY'];

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    // Only accept messages from our own extension
    if (sender.id !== chrome.runtime.id) return false;
    if (!message.type || !ALLOWED_TYPES.includes(message.type)) return false;

    if (message.type === 'SIDE_PANEL_READY') {
        // Deliver any queued selection — read from session storage so it
        // survives service worker termination between right-click and ready.
        chrome.storage.session.get('pendingSelection').then((stored) => {
            sendResponse({ text: stored.pendingSelection || null });
            chrome.storage.session.remove('pendingSelection');
        });
        return true; // Required: keeps the message channel open for async sendResponse
    }

    if (message.type === 'OPEN_SIDE_PANEL') {
        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            if (tabs[0]) {
                chrome.sidePanel.open({ windowId: tabs[0].windowId });
            }
        });
        sendResponse({ success: true });
    }

    // FACTCHECK_SELECTION is forwarded to the side panel via the same
    // runtime messaging channel — the side panel listens for it directly.

    return false;
});
