/**
 * Billing UI Module
 * Handles token balance and package displays
 */

class BillingUI {
    constructor() {
        this.tokenCount = document.getElementById('token-count');
        this.batteryLevel = document.getElementById('battery-level');
    }

    updateBalance(balance, showToast = null) {
        this.tokenCount.textContent = balance;

        // Update battery level (max 5000 for visual)
        const percentage = Math.min((balance / 5000) * 100, 100);
        this.batteryLevel.style.width = `${percentage}%`;

        // Show warning if low (only if showToast callback provided)
        if (showToast) {
            if (balance < 10 && balance > 0) {
                showToast('Low balance! Recharge your batteries', 'warning');
            } else if (balance === 0) {
                showToast('No tokens remaining! Purchase more to continue', 'error');
            }
        }
    }

    renderPackages(packages) {
        const container = document.querySelector('.token-packages');
        if (!container) return;

        container.innerHTML = '';

        // Render in order: small -> medium -> large -> wizard
        const order = ['battery_small', 'battery_medium', 'battery_large', 'wizard'];

        order.forEach(id => {
            const pkg = packages[id];
            if (!pkg) return;

            const card = document.createElement('div');
            card.className = `package-card ${pkg.is_subscription ? 'package-premium' : ''}`;
            if (id === 'wizard') card.classList.add('package-premium');
            card.dataset.package = id;

            const icon = id === 'battery_small' ? '🔋' :
                id === 'battery_medium' ? '🔋🔋' :
                    id === 'battery_large' ? '⚡' : '🧙‍♂️';

            const priceFormatted = (pkg.price / 100).toFixed(2);
            const currency = pkg.currency.toUpperCase();

            card.innerHTML = `
                ${id === 'wizard' ? '<div class="package-badge">Best Value</div>' : ''}
                <div class="package-icon">${icon}</div>
                <h3>${pkg.name}</h3>
                <div class="package-tokens">
                    ${id === 'wizard' ? `${pkg.tokens.toLocaleString()} CP/month` : `${pkg.tokens.toLocaleString()} CP`}
                </div>
                ${id === 'wizard' ? `<div class="package-duration">${Math.floor(pkg.duration / 12)} Years</div>` : ''}
                <div class="package-price">${priceFormatted} ${currency}</div>
                <button class="btn-primary btn-full">${id === 'wizard' ? 'Subscribe' : 'Purchase'}</button>
            `;

            container.appendChild(card);
        });
    }
}

export default new BillingUI();
