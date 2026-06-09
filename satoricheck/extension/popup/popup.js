/**
 * Popup script — handles token paste login and account display.
 *
 * Two states:
 *   1. Disconnected: shows token input + connect button
 *   2. Connected: shows email, balance, side panel launcher
 */

import { saveToken, getToken, saveUserEmail, getUserEmail, clearAuth } from '../lib/storage.js';
import { getCurrentUser, createCheckout } from '../lib/api.js';

// --- DOM refs ---
const stateDisconnected = document.getElementById('state-disconnected');
const stateConnected = document.getElementById('state-connected');
const tokenInput = document.getElementById('token-input');
const connectBtn = document.getElementById('connect-btn');
const getTokenLink = document.getElementById('get-token-link');
const connectError = document.getElementById('connect-error');
const accountEmail = document.getElementById('account-email');
const balanceBadge = document.getElementById('balance-badge');
const openSidepanelBtn = document.getElementById('open-sidepanel-btn');
const disconnectBtn = document.getElementById('disconnect-btn');

// --- Init ---
document.addEventListener('DOMContentLoaded', async () => {
    const token = await getToken();
    if (token) {
        await showConnectedState();
    } else {
        showDisconnectedState();
    }
});

// --- Event listeners ---

// Enable connect button only when input has content
tokenInput.addEventListener('input', () => {
    connectBtn.disabled = tokenInput.value.trim().length === 0;
    connectError.classList.add('hidden');
});

// Connect: validate token against the API
connectBtn.addEventListener('click', async () => {
    const token = tokenInput.value.trim();
    if (!token) return;

    connectBtn.disabled = true;
    connectBtn.innerHTML = '<span class="spinner"></span> Verifying…';
    connectError.classList.add('hidden');

    try {
        // Temporarily store token so api.js can use it
        await saveToken(token);

        // Validate by calling /auth/me
        const response = await getCurrentUser();

        if (response.success && response.user) {
            await saveUserEmail(response.user.email);
            await showConnectedState();
        } else {
            throw new Error('Invalid response from server');
        }
    } catch (error) {
        // Token is invalid — clear it
        await clearAuth();

        connectError.textContent = error.status === 401
            ? 'Invalid token. Please copy a fresh one from authenix.ai'
            : `Connection failed: ${error.message}`;
        connectError.classList.remove('hidden');

        connectBtn.disabled = false;
        connectBtn.textContent = 'Connect';
    }
});

// Open the web app to get a token (Cloud Run URL — update if domain mapping is set up)
getTokenLink.addEventListener('click', (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: 'https://satoricheck-829698588154.europe-west6.run.app?ext=1' });
});

// Open side panel
openSidepanelBtn.addEventListener('click', async () => {
    // chrome.sidePanel.open requires a windowId
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab) {
        await chrome.sidePanel.open({ windowId: tab.windowId });
    }
    window.close(); // Close popup after opening side panel
});

// Disconnect
disconnectBtn.addEventListener('click', async (e) => {
    e.preventDefault();
    await clearAuth();
    showDisconnectedState();
});

// CP badge click → open Stripe checkout
balanceBadge.addEventListener('click', async () => {
    try {
        const result = await createCheckout();
        if (result.url) {
            chrome.tabs.create({ url: result.url });
        }
    } catch {
        chrome.tabs.create({ url: 'https://authenix.ai' });
    }
});

// --- State transitions ---

/**
 * Fetch user info and switch to the connected state.
 */
async function showConnectedState() {
    try {
        const response = await getCurrentUser();
        const user = response.user;

        accountEmail.textContent = user.email;
        balanceBadge.textContent = `${user.balance ?? '—'} CP`;

        stateDisconnected.classList.add('hidden');
        stateConnected.classList.remove('hidden');
    } catch {
        // Token expired or invalid — fall back to disconnected
        await clearAuth();
        showDisconnectedState();
    }
}

/**
 * Switch to the disconnected state.
 */
function showDisconnectedState() {
    tokenInput.value = '';
    connectBtn.disabled = true;
    connectBtn.textContent = 'Connect';
    connectError.classList.add('hidden');

    stateConnected.classList.add('hidden');
    stateDisconnected.classList.remove('hidden');
}
