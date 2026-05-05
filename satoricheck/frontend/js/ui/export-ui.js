/**
 * Export UI Module
 * Handles CSV export functionality
 */

import api from '../api.js';

class ExportUI {
    async handleExport() {
        try {
            const blob = await api.exportFactChecks('csv');

            // Create download link
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `authenix_export_${new Date().toISOString().split('T')[0]}.csv`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            window.URL.revokeObjectURL(url);

            return { success: true };
        } catch (error) {
            return { success: false, error: error.message };
        }
    }
}

export default new ExportUI();
