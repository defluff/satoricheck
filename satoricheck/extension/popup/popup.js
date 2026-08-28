/**
 * Popup script — Quick launcher and Google OAuth controller for Authenix.
 *
 * @module popup
 */

import {
    getToken,
    clearAuth,
    getModePreference,
    saveModePreference
} from '../lib/storage.js';
import {
    getCurrentUser,
    syncWebAuthSession,
    initiateGoogleLogin,
    createCheckout
} from '../lib/api.js';

// --- DOM Elements ---
const stateDisconnected = document.getElementById('state-disconnected');
const stateConnected = document.getElementById('state-connected');
const googleLoginBtn = document.getElementById('google-login-btn');
const authStatus = document.getElementById('auth-status');

const accountEmail = document.getElementById('account-email');
const balanceBadge = document.getElementById('balance-badge');
const streakBadge = document.getElementById('streak-badge');
const modeSelect = document.getElementById('popup-mode-select');
const openSidepanelBtn = document.getElementById('open-sidepanel-btn');
const disconnectBtn = document.getElementById('disconnect-btn');

// --- Initialization ---
document.addEventListener('DOMContentLoaded', async () => {
    setupEventListeners();

    // Check if already authenticated
    const token = await getToken();
    if (token) {
        await showConnectedState();
        return;
    }

    // Auto-detect web session cookies
    if (authStatus) authStatus.textContent = 'Checking session…';
    const user = await syncWebAuthSession();
    if (user) {
        await showConnectedState();
    } else {
        showDisconnectedState();
    }
});

// --- Event Listeners ---
function setupEventListeners() {
    // 1-Click Google OAuth
    googleLoginBtn?.addEventListener('click', async () => {
        googleLoginBtn.disabled = true;
        googleLoginBtn.innerHTML = '<span class="spinner"></span> Connecting Google…';
        try {
            const user = await initiateGoogleLogin();
            if (user) {
                await showConnectedState();
            } else {
                if (authStatus) authStatus.textContent = 'Sign-in canceled or timed out.';
            }
        } catch (err) {
            if (authStatus) authStatus.textContent = `Sign-in error: ${err.message}`;
        } finally {
            googleLoginBtn.disabled = false;
            googleLoginBtn.innerHTML = `
                <svg class="google-icon" viewBox="0 0 24 24" width="18" height="18">
                    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/>
                    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/>
                </svg>
                Sign in with Google
            `;
        }
    });

    // Mode preference select
    modeSelect?.addEventListener('change', async () => {
        await saveModePreference(modeSelect.value);
    });

    // Open side panel
    openSidepanelBtn?.addEventListener('click', async () => {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) {
            await chrome.sidePanel.open({ windowId: tab.windowId });
        }
        window.close();
    });

    // Sign out / Disconnect
    disconnectBtn?.addEventListener('click', async (e) => {
        e.preventDefault();
        await clearAuth();
        showDisconnectedState();
    });

    // CP badge top-up trigger (opens Sidepanel top-up modal)
    balanceBadge?.addEventListener('click', async () => {
        try {
            await chrome.storage.local.set({ authenix_open_topup: true });
            const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
            if (tab) {
                await chrome.sidePanel.open({ windowId: tab.windowId });
            }
            window.close();
        } catch {
            chrome.tabs.create({ url: 'https://satoricheck-829698588154.europe-west6.run.app' });
        }
    });
}

// --- State Transitions ---

async function showConnectedState() {
    try {
        const response = await getCurrentUser();
        const user = response.user;

        accountEmail.textContent = user.email || 'Connected';
        balanceBadge.textContent = `${user.balance ?? 0} CP`;
        streakBadge.textContent = `🔥 ${user.streak ?? 0}`;

        const mode = await getModePreference();
        if (modeSelect) modeSelect.value = mode;

        stateDisconnected.classList.add('hidden');
        stateConnected.classList.remove('hidden');
    } catch {
        await clearAuth();
        showDisconnectedState();
    }
}

function showDisconnectedState() {
    if (authStatus) authStatus.textContent = '';
    stateConnected.classList.add('hidden');
    stateDisconnected.classList.remove('hidden');
}
