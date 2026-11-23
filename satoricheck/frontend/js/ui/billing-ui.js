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

            const nodes = [];
            if (id === 'wizard') {
                const badge = document.createElement('div');
                badge.className = 'package-badge';
                badge.textContent = 'Best Value';
                nodes.push(badge);
            }

            const iconDiv = document.createElement('div');
            iconDiv.className = 'package-icon';
            iconDiv.textContent = icon;
            nodes.push(iconDiv);

            const titleEl = document.createElement('h3');
            titleEl.textContent = pkg.name;
            nodes.push(titleEl);

            const tokensDiv = document.createElement('div');
            tokensDiv.className = 'package-tokens';
            tokensDiv.textContent = id === 'wizard'
                ? `${pkg.tokens.toLocaleString()} CP/month`
                : `${pkg.tokens.toLocaleString()} CP`;
            nodes.push(tokensDiv);

            if (id === 'wizard') {
                const durationDiv = document.createElement('div');
                durationDiv.className = 'package-duration';
                durationDiv.textContent = `${Math.floor(pkg.duration / 12)} Years`;
                nodes.push(durationDiv);
            }

            const priceDiv = document.createElement('div');
            priceDiv.className = 'package-price';
            priceDiv.textContent = `${priceFormatted} ${currency}`;
            nodes.push(priceDiv);

            const btn = document.createElement('button');
            btn.className = 'btn-primary btn-full';
            btn.textContent = id === 'wizard' ? 'Subscribe' : 'Purchase';
            nodes.push(btn);

            card.replaceChildren(...nodes);

            container.appendChild(card);
        });
    }
}

export default new BillingUI();
