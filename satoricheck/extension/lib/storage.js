/**
 * Storage module — encrypted token storage via AES-GCM-256 and preferences.
 *
 * The API/JWT token is encrypted at rest using a key generated once and
 * stored in chrome.storage.local. Both the key and the ciphertext
 * live inside the TRUSTED_CONTEXTS sandbox — content scripts and
 * other extensions cannot access them.
 *
 * @module storage
 */

const STORAGE_KEYS = {
    API_TOKEN: 'authenix_api_token',   // Legacy plaintext (migration only)
    ENCRYPTED_TOKEN: '_t',             // { iv: number[], data: number[] }
    ENCRYPTION_KEY: '_ek',             // Exported AES-GCM key bytes
    TOKEN_TIMESTAMP: '_ts',            // Unix ms — when token was stored
    USER_EMAIL: 'authenix_user_email',
    MODE_PREF: 'authenix_mode_pref',   // 'both' | 'claims' | 'ai'
};

/** Token expires after 30 days (client-side enforcement). */
const TOKEN_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;

/** Valid modular verification modes. */
export const VALID_MODES = ['both', 'claims', 'ai'];
export const DEFAULT_MODE = 'both';

/**
 * Get or create the AES-GCM-256 encryption key.
 * Generated once and persisted. Never leaves the extension sandbox.
 * @returns {Promise<CryptoKey>}
 */
async function getOrCreateKey() {
    const stored = await chrome.storage.local.get(STORAGE_KEYS.ENCRYPTION_KEY);
    const raw = stored[STORAGE_KEYS.ENCRYPTION_KEY];

    if (raw) {
        return crypto.subtle.importKey(
            'raw',
            new Uint8Array(raw),
            { name: 'AES-GCM' },
            false,
            ['encrypt', 'decrypt']
        );
    }

    const key = await crypto.subtle.generateKey(
        { name: 'AES-GCM', length: 256 },
        true, // extractable so we can persist the raw bytes
        ['encrypt', 'decrypt']
    );
    const exported = await crypto.subtle.exportKey('raw', key);
    await chrome.storage.local.set({
        [STORAGE_KEYS.ENCRYPTION_KEY]: Array.from(new Uint8Array(exported)),
    });
    return key;
}

/**
 * Encrypt and store the authentication token (API token or JWT).
 * @param {string} token - Token string
 * @returns {Promise<void>}
 */
export async function saveToken(token) {
    if (!token || typeof token !== 'string') return;
    const cleanToken = token.trim();
    const key = await getOrCreateKey();
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ciphertext = await crypto.subtle.encrypt(
        { name: 'AES-GCM', iv },
        key,
        new TextEncoder().encode(cleanToken)
    );

    await chrome.storage.local.set({
        [STORAGE_KEYS.ENCRYPTED_TOKEN]: {
            iv: Array.from(iv),
            data: Array.from(new Uint8Array(ciphertext)),
        },
        [STORAGE_KEYS.TOKEN_TIMESTAMP]: Date.now(),
    });

    // Remove any legacy plaintext token left over from pre-encryption builds
    await chrome.storage.local.remove(STORAGE_KEYS.API_TOKEN);
}

/**
 * Retrieve and decrypt the stored API/JWT token.
 * Returns null if no token, expired, or decryption fails.
 * @returns {Promise<string|null>}
 */
export async function getToken() {
    const stored = await chrome.storage.local.get([
        STORAGE_KEYS.ENCRYPTED_TOKEN,
        STORAGE_KEYS.TOKEN_TIMESTAMP,
        STORAGE_KEYS.API_TOKEN, // Legacy fallback
    ]);

    // --- Encrypted token (primary path) ---
    const encrypted = stored[STORAGE_KEYS.ENCRYPTED_TOKEN];
    const timestamp = stored[STORAGE_KEYS.TOKEN_TIMESTAMP];

    if (encrypted) {
        // Client-side expiry check
        if (timestamp && (Date.now() - timestamp > TOKEN_MAX_AGE_MS)) {
            await clearAuth();
            return null;
        }

        try {
            const key = await getOrCreateKey();
            const plaintext = await crypto.subtle.decrypt(
                { name: 'AES-GCM', iv: new Uint8Array(encrypted.iv) },
                key,
                new Uint8Array(encrypted.data)
            );
            return new TextDecoder().decode(plaintext);
        } catch {
            // Decryption failed (key rotated / data corrupt) — force re-auth
            await clearAuth();
            return null;
        }
    }

    // --- Legacy plaintext migration ---
    const plaintext = stored[STORAGE_KEYS.API_TOKEN];
    if (plaintext) {
        await saveToken(plaintext); // Re-saves encrypted + removes plaintext
        return plaintext;
    }

    return null;
}

/**
 * Save the user's email for display in the popup/panel.
 * Not sensitive — stored as plaintext.
 * @param {string} email
 * @returns {Promise<void>}
 */
export async function saveUserEmail(email) {
    await chrome.storage.local.set({ [STORAGE_KEYS.USER_EMAIL]: email });
}

/**
 * Retrieve the stored user email.
 * @returns {Promise<string|null>}
 */
export async function getUserEmail() {
    const result = await chrome.storage.local.get(STORAGE_KEYS.USER_EMAIL);
    return result[STORAGE_KEYS.USER_EMAIL] || null;
}

/**
 * Save the user's analysis mode preference ('both' | 'claims' | 'ai').
 * @param {'both'|'claims'|'ai'} mode
 * @returns {Promise<void>}
 */
export async function saveModePreference(mode) {
    const validMode = VALID_MODES.includes(mode) ? mode : DEFAULT_MODE;
    await chrome.storage.local.set({ [STORAGE_KEYS.MODE_PREF]: validMode });
}

/**
 * Retrieve the user's analysis mode preference. Defaults to 'both'.
 * @returns {Promise<'both'|'claims'|'ai'>}
 */
export async function getModePreference() {
    const result = await chrome.storage.local.get(STORAGE_KEYS.MODE_PREF);
    const mode = result[STORAGE_KEYS.MODE_PREF];
    return VALID_MODES.includes(mode) ? mode : DEFAULT_MODE;
}

/**
 * Clear all auth data (disconnect). Also wipes the encryption key
 * so a fresh one is generated on next connect.
 * @returns {Promise<void>}
 */
export async function clearAuth() {
    await chrome.storage.local.remove([
        STORAGE_KEYS.API_TOKEN,
        STORAGE_KEYS.ENCRYPTED_TOKEN,
        STORAGE_KEYS.ENCRYPTION_KEY,
        STORAGE_KEYS.TOKEN_TIMESTAMP,
        STORAGE_KEYS.USER_EMAIL,
    ]);
}
