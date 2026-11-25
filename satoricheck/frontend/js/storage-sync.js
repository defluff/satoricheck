/**
 * Cloud Storage Sync UI Module (Google Drive, Dropbox, OneDrive)
 * Strictly CSP-compliant: builds all DOM nodes via createElement / textContent / replaceChildren.
 */

import api from './api.js';
import ui from './ui.js';

const PROVIDERS = [
    {
        id: 'google_drive',
        name: 'Google Drive',
        badge: 'Native Google Docs + Sheets',
        outputPlaceholder: 'Leave blank to auto-create "Authenix Deal Briefs"'
    },
    {
        id: 'dropbox',
        name: 'Dropbox',
        badge: 'Markdown (.md) Deal Memos',
        inputPlaceholder: '/PitchDecks or https://www.dropbox.com/home/...',
        outputPlaceholder: '/DealMemos (must differ from Input)'
    },
    {
        id: 'onedrive',
        name: 'OneDrive',
        badge: 'Markdown (.md) Deal Memos',
        inputPlaceholder: 'OneDrive Input Folder Item ID',
        outputPlaceholder: 'OneDrive Output Folder Item ID'
    }
];

class StorageSyncManager {
    constructor() {
        this.isEnabled = false;
        this.connections = [];
        this.lastSyncByProvider = new Map();
        this.stagedDriveFiles = [];
        this.driveFolderLabels = new Map();
        this.onBatchCheckRequested = null;
        this.focusProvider = null;
        this.elements = {};
    }

    init() {
        this.elements = {
            modal: document.getElementById('cloud-storage-modal'),
            closeModalBtn: document.getElementById('close-cloud-storage-modal'),
            providersContainer: document.getElementById('cloud-storage-providers'),
            settingsSummary: document.getElementById('settings-storage-summary'),
            settingsManageBtn: document.getElementById('settings-manage-storage-btn'),
            pdCloudSyncBtn: document.getElementById('pd-cloud-sync-btn'),
            pdChooseFolderBtn: document.getElementById('pd-choose-folder-btn'),
            pdFolderSyncCard: document.getElementById('pd-folder-sync-card'),
            pdFolderSyncDisabled: document.getElementById('pd-folder-sync-disabled'),
            pdFolderSyncUnconfigured: document.getElementById('pd-folder-sync-unconfigured'),
            pdFolderSyncConnected: document.getElementById('pd-folder-sync-connected'),
            pdFolderUnconfiguredSubtitle: document.getElementById('pd-folder-unconfigured-subtitle'),
            pdSyncProviderName: document.getElementById('pd-sync-provider-name'),
            pdSyncFolderName: document.getElementById('pd-sync-folder-name'),
            pdDealSyncGroup: document.getElementById('pd-deal-sync-group'),
            pdDealSyncSettingsBtn: document.getElementById('pd-deal-sync-settings-btn')
        };

        this.elements.pdCloudSyncBtn?.addEventListener('click', () => {
            this.openModal();
        });

        this.elements.pdChooseFolderBtn?.addEventListener('click', () => {
            this.openModal();
        });

        this.elements.pdDealSyncSettingsBtn?.addEventListener('click', () => {
            this.openModal();
        });

        this.elements.settingsManageBtn?.addEventListener('click', () => {
            ui.hideModal('settings-modal');
            this.openModal();
        });

        this.elements.closeModalBtn?.addEventListener('click', () => {
            ui.hideModal('cloud-storage-modal');
        });
    }

    async getPrimaryConnectedConnection() {
        if (!this.isEnabled) return null;
        if (!Array.isArray(this.connections) || this.connections.length === 0) {
            await this.loadConnections({ silent: true });
        }
        if (!this.isEnabled) return null;
        return (this.connections || []).find(c => Boolean(c?.connected)) || null;
    }

    async openModal(focusProvider = null) {
        if (!this.isEnabled) return;
        this.focusProvider = focusProvider;
        ui.showModal('cloud-storage-modal');
        await this.loadConnections({ silent: false, focusProvider });
    }

    applyDisabledComingSoonUI() {
        const {
            pdFolderSyncCard,
            pdFolderSyncDisabled,
            pdFolderSyncUnconfigured,
            pdFolderSyncConnected,
            pdDealSyncGroup,
            settingsSummary,
            settingsManageBtn
        } = this.elements;

        pdFolderSyncCard?.classList.add('pd-folder-sync-card--disabled');
        pdFolderSyncCard?.classList.remove('pd-folder-sync-card--connected');
        pdFolderSyncCard?.setAttribute('aria-disabled', 'true');
        pdFolderSyncDisabled?.classList.remove('hidden');
        pdFolderSyncUnconfigured?.classList.add('hidden');
        pdFolderSyncConnected?.classList.add('hidden');
        pdDealSyncGroup?.classList.add('hidden');
        ui.hideModal('cloud-storage-modal');

        if (settingsSummary) {
            const pill = document.createElement('span');
            pill.className = 'storage-summary-pill';
            pill.textContent = 'Coming Soon';
            settingsSummary.replaceChildren(pill);
        }
        if (settingsManageBtn) {
            settingsManageBtn.disabled = true;
            settingsManageBtn.textContent = 'Coming Soon';
        }
    }

    async loadConnections({ silent = false, focusProvider = null } = {}) {
        if (focusProvider) {
            this.focusProvider = focusProvider;
        }
        try {
            const data = await api.getStorageConnections();
            this.isEnabled = data?.enabled !== false;
            if (!this.isEnabled) {
                this.connections = [];
                this.applyDisabledComingSoonUI();
                return [];
            }

            this.elements.pdFolderSyncCard?.classList.remove('pd-folder-sync-card--disabled');
            this.elements.pdFolderSyncCard?.removeAttribute('aria-disabled');
            this.elements.pdFolderSyncDisabled?.classList.add('hidden');
            this.elements.pdDealSyncGroup?.classList.remove('hidden');
            if (this.elements.settingsManageBtn) {
                this.elements.settingsManageBtn.disabled = false;
                this.elements.settingsManageBtn.textContent = '☁️ Manage Cloud Storage';
            }

            const list = Array.isArray(data?.connections) ? data.connections : [];
            this.connections = list;
            this.renderSettingsSummary(list);
            this.renderConnections(list);
            this.renderPitchdeckFolderCard(list);
            return list;
        } catch (error) {
            if (!silent) {
                ui.showToast('Failed to load cloud storage connections: ' + error.message, 'error');
            }
            return [];
        }
    }

    renderPitchdeckFolderCard(connections) {
        if (!this.isEnabled) {
            this.applyDisabledComingSoonUI();
            return;
        }
        const card = this.elements.pdFolderSyncCard;
        const unconfigured = this.elements.pdFolderSyncUnconfigured;
        const connectedEl = this.elements.pdFolderSyncConnected;
        if (!card || !unconfigured || !connectedEl) return;
        this.elements.pdFolderSyncDisabled?.classList.add('hidden');

        const list = Array.isArray(connections) ? connections : [];
        const configuredConn = list.find(
            c => Boolean(c?.connected) && (
                c.provider === 'google_drive'
                || (Boolean(c?.input_folder_ref) && Boolean(c?.output_folder_ref))
            )
        );
        const connectedOnly = !configuredConn ? list.find(c => Boolean(c?.connected)) : null;

        if (configuredConn) {
            const meta = PROVIDERS.find(p => p.id === configuredConn.provider);
            const providerLabel = meta ? meta.name : configuredConn.provider;
            const isGoogle = configuredConn.provider === 'google_drive';
            const folderRef = String(configuredConn.input_folder_ref || '').trim();
            const displayFolder = isGoogle
                ? (this.stagedDriveFiles.length > 0 ? this._formatStagedFilesSummary() : 'Select PDFs to Check')
                : this._formatFolderDisplay(folderRef);

            if (this.elements.pdSyncProviderName) {
                this.elements.pdSyncProviderName.textContent = providerLabel;
            }
            if (this.elements.pdSyncFolderName) {
                this.elements.pdSyncFolderName.textContent = displayFolder;
                this.elements.pdSyncFolderName.title = isGoogle
                    ? (this.stagedDriveFiles.map(f => f.name).join(', ') || 'Select PDFs from Google Drive to batch check')
                    : folderRef;
            }
            if (this.elements.pdCloudSyncBtn) {
                this.elements.pdCloudSyncBtn.textContent = isGoogle ? 'Batch settings' : 'Change folder';
            }

            card.classList.add('pd-folder-sync-card--connected');
            unconfigured.classList.add('hidden');
            connectedEl.classList.remove('hidden');
            return;
        }

        card.classList.remove('pd-folder-sync-card--connected');
        connectedEl.classList.add('hidden');
        unconfigured.classList.remove('hidden');

        if (this.elements.pdFolderUnconfiguredSubtitle) {
            if (connectedOnly) {
                const meta = PROVIDERS.find(p => p.id === connectedOnly.provider);
                const providerLabel = meta ? meta.name : connectedOnly.provider;
                this.elements.pdFolderUnconfiguredSubtitle.textContent =
                    `${providerLabel} connected — configure folders in Batch settings.`;
            } else {
                this.elements.pdFolderUnconfiguredSubtitle.textContent =
                    'Check multiple PDFs from a local folder or Google Drive.';
            }
        }
    }

    setBatchStatusText(statusText, tooltip = '') {
        if (this.elements.pdSyncFolderName) {
            this.elements.pdSyncFolderName.textContent = statusText;
            if (tooltip) {
                this.elements.pdSyncFolderName.title = tooltip;
            }
        }
    }

    _formatFolderDisplay(folderRef) {
        if (!folderRef) return 'Folder';
        // Strip URL prefix if user pasted a full Drive/Dropbox URL
        try {
            if (folderRef.startsWith('http://') || folderRef.startsWith('https://')) {
                const url = new URL(folderRef);
                const segments = url.pathname.split('/').filter(Boolean);
                const last = segments[segments.length - 1];
                if (last) return decodeURIComponent(last);
            }
        } catch (_) {
            // Fall back to raw string handling
        }
        return folderRef;
    }

    renderSettingsSummary(connections) {
        const container = this.elements.settingsSummary;
        if (!container) return;

        const connMap = new Map(connections.map(c => [c.provider, c]));
        const nodes = PROVIDERS.map(meta => {
            const conn = connMap.get(meta.id);
            const connected = Boolean(conn?.connected);
            const pill = document.createElement('span');
            pill.className = connected
                ? 'storage-summary-pill storage-summary-pill--connected'
                : 'storage-summary-pill';
            pill.textContent = connected ? `${meta.name}: Connected` : `${meta.name}: Not connected`;
            return pill;
        });
        container.replaceChildren(...nodes);
    }

    renderConnections(connections) {
        const container = this.elements.providersContainer;
        if (!container) return;

        const connMap = new Map(connections.map(c => [c.provider, c]));
        const cards = PROVIDERS.map(meta => {
            const conn = connMap.get(meta.id) || { provider: meta.id, connected: false };
            return this._buildProviderCard(meta, conn);
        });
        container.replaceChildren(...cards);
    }

    _buildProviderCard(meta, conn) {
        const card = document.createElement('section');
        card.className = 'storage-provider-card';
        card.dataset.provider = meta.id;
        if (this.focusProvider === meta.id) {
            card.classList.add('storage-provider-card--focused');
        }

        const header = document.createElement('div');
        header.className = 'storage-provider-header';

        const titleWrap = document.createElement('div');
        titleWrap.className = 'storage-provider-title-wrap';

        const title = document.createElement('h3');
        title.className = 'storage-provider-title';
        title.textContent = meta.name;
        titleWrap.appendChild(title);

        const formatBadge = document.createElement('span');
        formatBadge.className = 'badge badge-neutral';
        formatBadge.textContent = meta.badge;
        titleWrap.appendChild(formatBadge);

        const statusPill = document.createElement('span');
        statusPill.className = conn.connected
            ? 'storage-status-pill storage-status-pill--connected'
            : 'storage-status-pill';
        statusPill.textContent = conn.connected ? 'Connected' : 'Not connected';
        titleWrap.appendChild(statusPill);

        header.appendChild(titleWrap);

        const authActions = document.createElement('div');
        authActions.className = 'storage-provider-auth-actions';

        if (conn.connected) {
            const disconnectBtn = document.createElement('button');
            disconnectBtn.type = 'button';
            disconnectBtn.className = 'btn-secondary btn-sm';
            disconnectBtn.textContent = 'Disconnect';
            disconnectBtn.dataset.action = 'disconnect';
            disconnectBtn.addEventListener('click', () => this._handleDisconnect(meta, disconnectBtn));
            authActions.appendChild(disconnectBtn);
        } else {
            const connectBtn = document.createElement('button');
            connectBtn.type = 'button';
            connectBtn.className = 'btn-primary btn-sm';
            connectBtn.textContent = `Connect ${meta.name}`;
            connectBtn.dataset.action = 'connect';
            connectBtn.addEventListener('click', () => {
                window.location.assign(`/api/storage/${encodeURIComponent(meta.id)}/connect`);
            });
            authActions.appendChild(connectBtn);
        }

        header.appendChild(authActions);
        card.appendChild(header);

        if (!conn.connected) {
            const hint = document.createElement('p');
            hint.className = 'storage-disconnected-hint';
            hint.textContent = meta.id === 'google_drive'
                ? 'Connect your Google Drive account to pick pitch deck PDFs from any folder, choose where Deal Memos land, and sync.'
                : `Connect your ${meta.name} account to designate an input folder for PDF pitch decks and a separate output folder for Deal Memos.`;
            card.appendChild(hint);
            return card;
        }

        // Config Form
        const form = document.createElement('form');
        form.className = 'storage-config-form';
        form.noValidate = true;

        const grid = document.createElement('div');
        grid.className = 'storage-config-grid';

        const inputId = `storage-${meta.id}-input-folder`;
        const outputId = `storage-${meta.id}-output-folder`;
        const presetId = `storage-${meta.id}-preset`;
        const dailyId = `storage-${meta.id}-daily`;
        const autoSheetsId = `storage-${meta.id}-auto-sheets`;
        const sheetIdInputId = `storage-${meta.id}-sheet-id`;
        const sheetNameInputId = `storage-${meta.id}-sheet-name`;

        const inlineError = document.createElement('div');
        inlineError.className = 'storage-inline-error hidden';

        const handleAuthError = (error, fallbackMessage) => {
            if (error.status === 401 && error.data?.needs_reauth) {
                statusPill.textContent = 'Reconnect required';
                statusPill.className = 'storage-status-pill storage-status-pill--error';
                ui.showToast(`${meta.name} authorization expired. Please reconnect.`, 'error');
            }
            inlineError.textContent = error.message || fallbackMessage;
            inlineError.classList.remove('hidden');
        };

        const isGoogleDrive = meta.id === 'google_drive';
        let inputField = null;
        let outputField = null;
        let selectedOutputFolderId = conn.output_folder_ref || '';

        if (isGoogleDrive) {
            const driveInputPicker = this._createPickerField({
                id: inputId,
                labelText: '1. Input Pitch Decks (PDFs)',
                placeholder: 'Browse Drive folder & select PDFs…',
                onBrowse: async (btn) => {
                    inlineError.classList.add('hidden');
                    inlineError.textContent = '';
                    btn.disabled = true;
                    btn.textContent = 'Opening…';
                    try {
                        const picked = await this._openGoogleDrivePdfPicker();
                        if (picked) {
                            this.stagedDriveFiles = picked;
                            syncDriveInputDisplay();
                            this.renderPitchdeckFolderCard(this.connections);
                        }
                    } catch (error) {
                        handleAuthError(error, 'Unable to open Google Drive PDF picker.');
                    } finally {
                        btn.disabled = false;
                        btn.textContent = 'Browse…';
                    }
                }
            });

            const syncDriveInputDisplay = () => {
                if (this.stagedDriveFiles.length > 0) {
                    driveInputPicker.input.value = `✓ ${this._formatStagedFilesSummary()}`;
                    driveInputPicker.input.title = this.stagedDriveFiles.map(f => f.name).join(', ');
                } else {
                    driveInputPicker.input.value = '';
                    driveInputPicker.input.title = 'Browse your Google Drive folders and select up to 25 PDFs';
                }
            };
            syncDriveInputDisplay();

            const driveOutputPicker = this._createPickerField({
                id: outputId,
                labelText: '2. Output Folder (Where Memos Land)',
                placeholder: meta.outputPlaceholder,
                onBrowse: async (btn) => {
                    inlineError.classList.add('hidden');
                    inlineError.textContent = '';
                    btn.disabled = true;
                    btn.textContent = 'Opening…';
                    try {
                        const folder = await this._openGoogleDriveFolderPicker();
                        if (folder) {
                            selectedOutputFolderId = folder.id;
                            this.driveFolderLabels.set(folder.id, folder.name || folder.id);
                            syncDriveOutputDisplay();
                        }
                    } catch (error) {
                        handleAuthError(error, 'Unable to open Google Drive folder picker.');
                    } finally {
                        btn.disabled = false;
                        btn.textContent = 'Browse…';
                    }
                },
                onReset: () => {
                    selectedOutputFolderId = '';
                    syncDriveOutputDisplay();
                }
            });

            const syncDriveOutputDisplay = () => {
                if (selectedOutputFolderId) {
                    const label = this._formatDriveFolderLabel(selectedOutputFolderId);
                    driveOutputPicker.input.value = `📁 ${label}`;
                    driveOutputPicker.input.title = selectedOutputFolderId;
                    driveOutputPicker.resetBtn?.classList.remove('hidden');
                } else {
                    driveOutputPicker.input.value = '';
                    driveOutputPicker.input.title = meta.outputPlaceholder;
                    driveOutputPicker.resetBtn?.classList.add('hidden');
                }
            };
            syncDriveOutputDisplay();

            grid.appendChild(driveInputPicker.wrapper);
            grid.appendChild(driveOutputPicker.wrapper);
        } else {
            inputField = this._createInputField(
                inputId,
                'Input Folder (PDF Pitch Decks)',
                conn.input_folder_ref || '',
                meta.inputPlaceholder
            );
            outputField = this._createInputField(
                outputId,
                'Output Folder (Deal Memos — must differ)',
                conn.output_folder_ref || '',
                meta.outputPlaceholder
            );
            grid.appendChild(inputField.wrapper);
            grid.appendChild(outputField.wrapper);
        }

        const presetWrapper = document.createElement('div');
        presetWrapper.className = 'storage-config-field';
        const presetLabel = document.createElement('label');
        presetLabel.htmlFor = presetId;
        presetLabel.textContent = 'Deal Memo Preset';
        const presetSelect = document.createElement('select');
        presetSelect.id = presetId;
        presetSelect.className = 'storage-input';
        const optBrief = document.createElement('option');
        optBrief.value = 'brief';
        optBrief.textContent = 'Brief (Summary & Red Flags)';
        const optExtended = document.createElement('option');
        optExtended.value = 'extended';
        optExtended.textContent = 'Extended (Full Scorecard & Thesis)';
        presetSelect.appendChild(optBrief);
        presetSelect.appendChild(optExtended);
        presetSelect.value = conn.memo_preset === 'extended' ? 'extended' : 'brief';
        presetWrapper.appendChild(presetLabel);
        presetWrapper.appendChild(presetSelect);
        grid.appendChild(presetWrapper);

        const togglesWrapper = document.createElement('div');
        togglesWrapper.className = 'storage-config-field storage-toggles-box';

        const dailyToggle = isGoogleDrive
            ? null
            : this._createToggleSwitch(
                dailyId,
                'Automated Daily Sync',
                Boolean(conn.daily_sync_enabled)
            );
        const sheetsToggle = this._createToggleSwitch(
            autoSheetsId,
            'Auto-Export to Google Sheets',
            Boolean(conn.auto_export_sheets)
        );
        if (dailyToggle) {
            togglesWrapper.appendChild(dailyToggle.label);
        }
        togglesWrapper.appendChild(sheetsToggle.label);
        grid.appendChild(togglesWrapper);

        const sheetIdField = this._createInputField(
            sheetIdInputId,
            'Google Sheet ID or URL',
            conn.sheets_spreadsheet_id || '',
            'Spreadsheet ID or https://docs.google.com/spreadsheets/d/...'
        );
        const sheetNameField = this._createInputField(
            sheetNameInputId,
            'Sheet Tab Name (optional)',
            conn.sheets_sheet_name || '',
            'Sheet1'
        );
        grid.appendChild(sheetIdField.wrapper);
        grid.appendChild(sheetNameField.wrapper);

        const syncSheetsFieldState = () => {
            const enabled = sheetsToggle.checkbox.checked;
            sheetIdField.wrapper.classList.toggle('storage-field-muted', !enabled);
            sheetNameField.wrapper.classList.toggle('storage-field-muted', !enabled);
        };
        syncSheetsFieldState();
        sheetsToggle.checkbox.addEventListener('change', syncSheetsFieldState);

        form.appendChild(grid);
        form.appendChild(inlineError);

        const actionsRow = document.createElement('div');
        actionsRow.className = 'storage-card-actions';

        const saveBtn = document.createElement('button');
        saveBtn.type = 'submit';
        saveBtn.className = 'btn-secondary btn-sm';
        saveBtn.textContent = 'Save Config';

        const syncBtn = document.createElement('button');
        syncBtn.type = 'button';
        syncBtn.className = 'btn-primary btn-sm';
        syncBtn.textContent = 'Check Decks';
        syncBtn.dataset.action = 'sync-now';

        actionsRow.appendChild(saveBtn);
        actionsRow.appendChild(syncBtn);
        form.appendChild(actionsRow);

        const metaRow = document.createElement('div');
        metaRow.className = 'storage-meta-row';
        const lastSyncedText = conn.last_synced_at
            ? `Last checked: ${new Date(conn.last_synced_at).toLocaleString()}`
            : 'No batch checks run yet';
        const lastSyncedSpan = document.createElement('span');
        lastSyncedSpan.textContent = lastSyncedText;
        metaRow.appendChild(lastSyncedSpan);

        if (conn.last_error) {
            const errSpan = document.createElement('span');
            errSpan.className = 'storage-last-error';
            errSpan.textContent = `Last error: ${conn.last_error}`;
            metaRow.appendChild(errSpan);
        }
        form.appendChild(metaRow);

        const resultsContainer = document.createElement('div');
        resultsContainer.className = 'storage-sync-results hidden';
        const cachedSyncResult = this.lastSyncByProvider.get(meta.id);
        if (cachedSyncResult) {
            this._renderSyncResultsInto(resultsContainer, cachedSyncResult);
        }
        form.appendChild(resultsContainer);

        const buildValidatedConfigPayload = () => {
            const inputRef = inputField ? inputField.input.value.trim() : '';
            const outputRef = isGoogleDrive ? selectedOutputFolderId : outputField.input.value.trim();
            const autoSheets = sheetsToggle.checkbox.checked;
            const sheetIdVal = sheetIdField.input.value.trim();
            const sheetNameVal = sheetNameField.input.value.trim();

            if (!isGoogleDrive && (!inputRef || !outputRef)) {
                inlineError.textContent = 'Both Input Folder and Output Folder are required.';
                inlineError.classList.remove('hidden');
                return null;
            }
            if (inputRef && outputRef && inputRef === outputRef) {
                inlineError.textContent = 'Input Folder and Output Folder must be distinct.';
                inlineError.classList.remove('hidden');
                return null;
            }
            if (autoSheets && !sheetIdVal) {
                inlineError.textContent = 'Enter a Google Sheet ID or URL to enable auto-export.';
                inlineError.classList.remove('hidden');
                return null;
            }

            const payload = {
                output_folder_ref: outputRef || null,
                memo_preset: presetSelect.value,
                auto_export_sheets: autoSheets,
                sheets_spreadsheet_id: sheetIdVal || null,
                sheets_sheet_name: sheetNameVal || null
            };
            if (!isGoogleDrive) {
                payload.input_folder_ref = inputRef;
                payload.daily_sync_enabled = dailyToggle ? dailyToggle.checkbox.checked : false;
            }
            return payload;
        };

        const hasUnsavedConfig = () => {
            const currentOutput = isGoogleDrive ? (selectedOutputFolderId || null) : (outputField.input.value.trim() || null);
            const savedPreset = conn.memo_preset === 'extended' ? 'extended' : 'brief';
            if (currentOutput !== (conn.output_folder_ref || null)) return true;
            if (presetSelect.value !== savedPreset) return true;
            if (sheetsToggle.checkbox.checked !== Boolean(conn.auto_export_sheets)) return true;
            if ((sheetIdField.input.value.trim() || null) !== (conn.sheets_spreadsheet_id || null)) return true;
            if ((sheetNameField.input.value.trim() || null) !== (conn.sheets_sheet_name || null)) return true;
            if (!isGoogleDrive) {
                if ((inputField.input.value.trim() || null) !== (conn.input_folder_ref || null)) return true;
                if (Boolean(dailyToggle?.checkbox.checked) !== Boolean(conn.daily_sync_enabled)) return true;
            }
            return false;
        };

        const persistConfig = async () => {
            const payload = buildValidatedConfigPayload();
            if (!payload) return false;
            const requestedBlankDriveOutput = isGoogleDrive && !payload.output_folder_ref;
            const saved = await api.updateStorageConfig(meta.id, payload);
            if (requestedBlankDriveOutput && saved?.output_folder_ref) {
                this.driveFolderLabels.set(saved.output_folder_ref, 'Authenix Deal Briefs');
                selectedOutputFolderId = saved.output_folder_ref;
            }
            return true;
        };

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            inlineError.classList.add('hidden');
            inlineError.textContent = '';

            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving...';
            try {
                const ok = await persistConfig();
                if (!ok) return;
                ui.showToast(`${meta.name} sync configuration saved.`, 'success');
                await this.loadConnections({ silent: true, focusProvider: meta.id });
            } catch (error) {
                handleAuthError(error, 'Failed to save configuration.');
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save Config';
            }
        });

        syncBtn.addEventListener('click', async () => {
            inlineError.classList.add('hidden');
            inlineError.textContent = '';
            syncBtn.disabled = true;
            try {
                if (hasUnsavedConfig() || (!isGoogleDrive && (!conn.input_folder_ref || !conn.output_folder_ref))) {
                    syncBtn.textContent = 'Saving...';
                    const ok = await persistConfig();
                    if (!ok) return;
                }

                if (isGoogleDrive && !this.stagedDriveFiles.length) {
                    syncBtn.textContent = 'Opening Picker...';
                    const decks = await this.getStagedOrPickGoogleDriveDecks();
                    if (!decks.length) {
                        return;
                    }
                }

                if (typeof this.onBatchCheckRequested === 'function') {
                    ui.hideModal('cloud-storage-modal');
                    await this.onBatchCheckRequested(meta.id);
                    return;
                }

                syncBtn.textContent = 'Checking...';
                const syncPayload = isGoogleDrive
                    ? { file_ids: this.stagedDriveFiles.map((d) => d.id) }
                    : null;
                const res = await api.triggerStorageSync(meta.id, syncPayload);
                if (isGoogleDrive) {
                    this.clearStagedDriveFiles();
                }
                this.lastSyncByProvider.set(meta.id, res);
                if (typeof res.new_balance === 'number') {
                    ui.updateBalance(res.new_balance);
                }
                this._renderSyncResultsInto(resultsContainer, res);
                await this.loadConnections({ silent: true, focusProvider: meta.id });
            } catch (error) {
                handleAuthError(error, 'Batch check failed.');
            } finally {
                syncBtn.disabled = false;
                syncBtn.textContent = 'Check Decks';
            }
        });

        card.appendChild(form);
        return card;
    }

    async _handleDisconnect(meta, btn) {
        btn.disabled = true;
        btn.textContent = 'Disconnecting...';
        try {
            await api.disconnectStorageProvider(meta.id);
            this.lastSyncByProvider.delete(meta.id);
            if (meta.id === 'google_drive') {
                this.clearStagedDriveFiles();
            }
            ui.showToast(`${meta.name} disconnected.`, 'info');
            await this.loadConnections({ silent: true });
        } catch (error) {
            ui.showToast(`Failed to disconnect ${meta.name}: ${error.message}`, 'error');
            btn.disabled = false;
            btn.textContent = 'Disconnect';
        }
    }

    clearStagedDriveFiles() {
        this.stagedDriveFiles = [];
        if (Array.isArray(this.connections) && this.connections.length > 0) {
            this.renderPitchdeckFolderCard(this.connections);
        }
    }

    _formatStagedFilesSummary() {
        const count = this.stagedDriveFiles.length;
        if (count === 0) return '';
        if (count === 1) return this.stagedDriveFiles[0].name;
        return `${count} PDFs (${this.stagedDriveFiles[0].name} +${count - 1})`;
    }

    _formatDriveFolderLabel(folderId) {
        const cached = this.driveFolderLabels.get(folderId);
        if (cached) return cached;
        return folderId.length > 18 ? `Drive folder (${folderId.slice(0, 10)}…)` : folderId;
    }

    _loadGooglePickerApi() {
        if (window.google?.picker) {
            return Promise.resolve();
        }
        if (this._pickerLoadPromise) {
            return this._pickerLoadPromise;
        }
        this._pickerLoadPromise = new Promise((resolve, reject) => {
            const onGapiReady = () => {
                if (!window.gapi?.load) {
                    reject(new Error('Google API loader unavailable'));
                    return;
                }
                window.gapi.load('picker', {
                    callback: () => resolve(),
                    onerror: () => reject(new Error('Failed to load Google Picker')),
                });
            };

            if (window.gapi?.load) {
                onGapiReady();
                return;
            }

            const script = document.createElement('script');
            script.src = 'https://apis.google.com/js/api.js';
            script.async = true;
            script.onload = onGapiReady;
            script.onerror = () => reject(new Error('Failed to load Google API script'));
            document.head.appendChild(script);
        }).catch((err) => {
            this._pickerLoadPromise = null;
            throw err;
        });
        return this._pickerLoadPromise;
    }

    async _openGoogleDrivePdfPicker() {
        const [cfg] = await Promise.all([
            api.getStoragePickerConfig('google_drive'),
            this._loadGooglePickerApi(),
        ]);
        const pickerApi = window.google?.picker;
        if (!pickerApi) {
            throw new Error('Google Picker failed to initialize');
        }
        return new Promise((resolve) => {
            const view = new pickerApi.DocsView(pickerApi.ViewId.PDFS)
                .setIncludeFolders(true);
            const picker = new pickerApi.PickerBuilder()
                .addView(view)
                .enableFeature(pickerApi.Feature.MULTISELECT_ENABLED)
                .setMaxItems(25)
                .setOAuthToken(cfg.access_token)
                .setDeveloperKey(cfg.api_key)
                .setAppId(cfg.app_id)
                .setTitle('Select PDF Pitch Decks from Folder (up to 25)')
                .setCallback((data) => {
                    if (data.action === pickerApi.Action.PICKED) {
                        const docs = (Array.isArray(data.docs) ? data.docs : [])
                            .map((d) => ({
                                id: String(d?.id || '').trim(),
                                name: String(d?.name || d?.id || 'Deck.pdf').trim()
                            }))
                            .filter((d) => Boolean(d.id))
                            .slice(0, 25);
                        resolve(docs);
                    } else if (data.action === pickerApi.Action.CANCEL) {
                        resolve(null);
                    }
                })
                .build();
            picker.setVisible(true);
        });
    }

    async _openGoogleDriveFolderPicker() {
        const [cfg] = await Promise.all([
            api.getStoragePickerConfig('google_drive'),
            this._loadGooglePickerApi(),
        ]);
        const pickerApi = window.google?.picker;
        if (!pickerApi) {
            throw new Error('Google Picker failed to initialize');
        }
        return new Promise((resolve) => {
            const view = new pickerApi.DocsView(pickerApi.ViewId.FOLDERS)
                .setIncludeFolders(true)
                .setSelectFolderEnabled(true)
                .setMimeTypes('application/vnd.google-apps.folder');
            const picker = new pickerApi.PickerBuilder()
                .addView(view)
                .setOAuthToken(cfg.access_token)
                .setDeveloperKey(cfg.api_key)
                .setAppId(cfg.app_id)
                .setTitle('Select Output Folder for Deal Memos')
                .setCallback((data) => {
                    if (data.action === pickerApi.Action.PICKED && Array.isArray(data.docs) && data.docs[0]?.id) {
                        resolve({
                            id: String(data.docs[0].id).trim(),
                            name: String(data.docs[0].name || data.docs[0].id).trim()
                        });
                    } else if (data.action === pickerApi.Action.CANCEL) {
                        resolve(null);
                    }
                })
                .build();
            picker.setVisible(true);
        });
    }

    async getStagedOrPickGoogleDriveDecks() {
        if (this.stagedDriveFiles.length > 0) {
            return [...this.stagedDriveFiles];
        }
        const picked = await this._openGoogleDrivePdfPicker();
        if (!picked || !picked.length) {
            return [];
        }
        this.stagedDriveFiles = picked;
        this.renderPitchdeckFolderCard(this.connections);
        return [...picked];
    }

    async getOrPickGoogleDrivePdfs() {
        const decks = await this.getStagedOrPickGoogleDriveDecks();
        return decks.map((f) => f.id);
    }

    _createPickerField({ id, labelText, placeholder, onBrowse, onReset = null }) {
        const wrapper = document.createElement('div');
        wrapper.className = 'storage-config-field';

        const label = document.createElement('label');
        label.htmlFor = id;
        label.textContent = labelText;

        const row = document.createElement('div');
        row.className = 'storage-picker-row';

        const input = document.createElement('input');
        input.type = 'text';
        input.id = id;
        input.className = 'storage-input storage-input--picker';
        input.readOnly = true;
        input.placeholder = placeholder;

        const browseBtn = document.createElement('button');
        browseBtn.type = 'button';
        browseBtn.className = 'btn-secondary btn-sm';
        browseBtn.textContent = 'Browse…';
        browseBtn.addEventListener('click', () => onBrowse(browseBtn));
        input.addEventListener('click', () => {
            if (!browseBtn.disabled) onBrowse(browseBtn);
        });

        row.appendChild(input);
        row.appendChild(browseBtn);

        let resetBtn = null;
        if (typeof onReset === 'function') {
            resetBtn = document.createElement('button');
            resetBtn.type = 'button';
            resetBtn.className = 'btn-secondary btn-sm hidden';
            resetBtn.textContent = 'Auto';
            resetBtn.title = 'Reset to auto-created "Authenix Deal Briefs"';
            resetBtn.setAttribute('aria-label', 'Reset output folder to auto-create Authenix Deal Briefs');
            resetBtn.addEventListener('click', onReset);
            row.appendChild(resetBtn);
        }

        wrapper.appendChild(label);
        wrapper.appendChild(row);
        return { wrapper, input, browseBtn, resetBtn };
    }

    _createInputField(id, labelText, initialValue, placeholder) {
        const wrapper = document.createElement('div');
        wrapper.className = 'storage-config-field';

        const label = document.createElement('label');
        label.htmlFor = id;
        label.textContent = labelText;

        const input = document.createElement('input');
        input.type = 'text';
        input.id = id;
        input.className = 'storage-input';
        input.value = initialValue;
        input.placeholder = placeholder;

        wrapper.appendChild(label);
        wrapper.appendChild(input);
        return { wrapper, input };
    }

    _createToggleSwitch(id, labelText, checked) {
        const label = document.createElement('label');
        label.className = 'toggle-switch';
        label.htmlFor = id;

        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.id = id;
        checkbox.checked = checked;

        const slider = document.createElement('span');
        slider.className = 'toggle-slider';

        const textSpan = document.createElement('span');
        textSpan.className = 'toggle-label';
        textSpan.textContent = labelText;

        label.appendChild(checkbox);
        label.appendChild(slider);
        label.appendChild(textSpan);
        return { label, checkbox };
    }

    _renderSyncResultsInto(container, res) {
        const summary = document.createElement('div');
        summary.className = 'storage-sync-summary-line';
        const invalidOrErrors = (res.skipped_invalid ?? 0) + (res.errors ?? 0);
        summary.textContent =
            `Processed: ${res.processed ?? 0} · ` +
            `Invalid / Errors: ${invalidOrErrors}`;

        const children = [summary];
        const records = Array.isArray(res.records) ? res.records : [];
        if (records.length > 0) {
            const list = document.createElement('ul');
            list.className = 'storage-sync-records-list';
            records.forEach(rec => {
                const li = document.createElement('li');
                li.className = 'storage-record-item';

                const nameSpan = document.createElement('span');
                nameSpan.className = 'storage-record-name';
                nameSpan.textContent = rec.file_name || rec.external_file_id || 'Deck';
                li.appendChild(nameSpan);

                const statusSpan = document.createElement('span');
                statusSpan.className = `storage-record-status storage-record-status--${rec.status || 'error'}`;
                statusSpan.textContent = rec.status || 'unknown';
                li.appendChild(statusSpan);

                if (typeof rec.exported_doc_url === 'string' && rec.exported_doc_url.startsWith('https://')) {
                    const link = document.createElement('a');
                    link.href = rec.exported_doc_url;
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                    link.className = 'storage-record-link';
                    link.textContent = 'Memo ↗';
                    li.appendChild(link);
                }

                if (rec.exported_sheet_range) {
                    const sheetSpan = document.createElement('span');
                    sheetSpan.className = 'storage-record-sheet';
                    sheetSpan.textContent = `Sheet: ${rec.exported_sheet_range}`;
                    li.appendChild(sheetSpan);
                }

                if (rec.error_message) {
                    const msgSpan = document.createElement('span');
                    msgSpan.className = 'storage-record-error';
                    msgSpan.textContent = rec.error_message;
                    li.appendChild(msgSpan);
                }

                list.appendChild(li);
            });
            children.push(list);
        }

        container.replaceChildren(...children);
        container.classList.remove('hidden');
    }
}

export default new StorageSyncManager();
