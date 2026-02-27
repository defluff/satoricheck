/**
 * Selection Handler Module
 * Manages text selection and verification tooltip
 */

import ui from './ui.js';
import factcheck from './factcheck.js';

class SelectionHandler {
    constructor() {
        this.selectedText = '';
    }

    init() {
        // Listen for text selection globally (to work with contenteditable)
        document.addEventListener('mouseup', (e) => {
            // Only handle selections within the transcript container
            if (ui.elements.transcriptContainer.contains(e.target)) {
                setTimeout(() => this.handleSelection(), 10);
            }
        });

        // Also handle keyboard selection
        document.addEventListener('keyup', (e) => {
            if (ui.elements.transcriptContainer.contains(e.target)) {
                setTimeout(() => this.handleSelection(), 10);
            }
        });

        // Listen for selection tooltip click
        const tooltipBtn = ui.elements.selectionTooltip.querySelector('.btn-tooltip');
        if (tooltipBtn) {
            tooltipBtn.addEventListener('click', () => {
                this.verifySelection();
            });
        }

        // Hide tooltip when clicking elsewhere
        document.addEventListener('mousedown', (e) => {
            if (!ui.elements.selectionTooltip.contains(e.target) &&
                !ui.elements.transcriptContainer.contains(e.target)) {
                ui.hideSelectionTooltip();
            }
        });
    }

    handleSelection() {
        const selection = window.getSelection();
        const text = selection.toString().trim();

        if (!text || text.length < 5) {
            ui.hideSelectionTooltip();
            return;
        }

        // Check if selection is within transcript container
        const range = selection.getRangeAt(0);
        if (!ui.elements.transcriptContainer.contains(range.commonAncestorContainer)) {
            return;
        }

        this.selectedText = text;

        // Position tooltip near selection
        const rect = range.getBoundingClientRect();

        const tooltipX = rect.left + (rect.width / 2) - 60; // Center tooltip
        const tooltipY = rect.top - 10; // Above selection

        ui.showSelectionTooltip(tooltipX, tooltipY);
    }

    verifySelection() {
        if (this.selectedText) {
            // Check analysis mode
            const appMode = localStorage.getItem('analysisMode') || 'factcheck';

            if (appMode === 'aidetect') {
                factcheck.handleAICheckWithText(this.selectedText);
            } else {
                // Always use Smart Agent for selection verification
                factcheck.handleSmartAgentCheck(this.selectedText);
            }

            ui.hideSelectionTooltip();

            // Clear selection
            window.getSelection().removeAllRanges();
            this.selectedText = '';
        }
    }
}

export default new SelectionHandler();
