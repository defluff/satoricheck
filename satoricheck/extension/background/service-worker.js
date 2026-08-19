/**
 * Background service worker — Authenix extension entry point.
 *
 * Responsibilities:
 *   - Register context menu item ("Verify with Authenix")
 *   - Route context menu selections to the side panel
 *   - Handle messages from popup, side panel, and content script
 *   - Restrict storage access to trusted contexts
 */

// --- Security: restrict storage to extension pages only ---
chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' })
    .catch(() => {
        // Fallback for older Chromium versions where this API may not exist
    });

// --- Side panel behavior ---
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: false })
    .catch(() => {
        // Popup is the primary action icon click
    });

// --- Context menu registration ---
chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.create({
        id: 'authenix-factcheck',
        title: 'Verify with Authenix (Fact & AI)',
        contexts: ['selection'],
    });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    if (info.menuItemId !== 'authenix-factcheck' || !tab?.id) return;

    const selectedText = (info.selectionText || '').trim().slice(0, 10000);
    if (!selectedText) return;

    // Persist for READY handshake — storage.session survives SW termination
    await chrome.storage.session.set({ pendingSelection: selectedText });

    try {
        await chrome.sidePanel.open({ tabId: tab.id });

        // Fast path: if panel is already open its listener receives this directly
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
    }
});

// --- Message handler ---
const ALLOWED_TYPES = ['FACTCHECK_SELECTION', 'OPEN_SIDE_PANEL', 'SIDE_PANEL_READY', 'AUTH_UPDATED'];

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id) return false;
    if (!message.type || !ALLOWED_TYPES.includes(message.type)) return false;

    if (message.type === 'SIDE_PANEL_READY') {
        chrome.storage.session.get('pendingSelection').then((stored) => {
            sendResponse({ text: stored.pendingSelection || null });
            chrome.storage.session.remove('pendingSelection');
        });
        return true;
    }

    if (message.type === 'OPEN_SIDE_PANEL') {
        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            if (tabs[0]) {
                chrome.sidePanel.open({ windowId: tabs[0].windowId });
            }
        });
        sendResponse({ success: true });
        return false;
    }

    return false;
});
