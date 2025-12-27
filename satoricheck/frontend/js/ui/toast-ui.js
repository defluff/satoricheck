/**
 * Toast Notifications Module
 * Handles temporary notification messages
 */

class ToastUI {
    constructor() {
        this.toastContainer = document.getElementById('toast-container');
    }

    show(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;

        this.toastContainer.appendChild(toast);

        // Auto-remove after 5 seconds
        setTimeout(() => {
            toast.style.animation = 'fadeOut 0.3s ease';
            setTimeout(() => toast.remove(), 300);
        }, 5000);
    }
}

export default new ToastUI();
