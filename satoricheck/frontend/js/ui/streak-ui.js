/**
 * Streak UI Module
 * Handles gamification streak display
 */

class StreakUI {
    constructor() {
        this.streakCount = document.getElementById('streak-count');
        this.streakRoadmap = document.getElementById('streak-roadmap');
    }

    updateStreak(streakData) {
        if (streakData.current_streak !== undefined) {
            this.streakCount.textContent = streakData.current_streak;
        }

        // Update roadmap in settings
        if (streakData.current_milestone && this.streakRoadmap) {
            this.renderStreakRoadmap(streakData);
        }
    }

    renderStreakRoadmap(streakData) {
        const currentStreak = streakData.current_streak || 0;

        // Calculate Cycle (Prestige Level)
        const cycle = Math.floor((currentStreak - 1) / 30);
        const displayStreak = (currentStreak - 1) % 30 + 1;

        const isLegendary = cycle > 0;
        const themeClass = isLegendary ? 'legendary' : '';
        const badgeHtml = isLegendary ? '<span class="legendary-badge">👑</span>' : '';

        // Reward schedule
        const rewards = {
            6: { icon: '🔋', amount: 100 },
            14: { icon: '🔋', amount: 200 },
            21: { icon: '⚡', amount: 400 },
            30: { icon: '💎', amount: 1000 }
        };

        let html = `
            <div class="streak-info-container ${themeClass}">
                <span class="streak-count-large ${themeClass}">
                    ${currentStreak} Days ${badgeHtml}
                </span>
                <p class="modal-subtitle">
                    ${isLegendary ? 'Legendary Streak Active! Blue Flame Ignited.' : 'Keep the energy flowing!'}
                </p>
            </div>
            <div class="streak-grid ${themeClass}">
        `;

        for (let i = 1; i <= 30; i++) {
            let classes = 'streak-day';
            let iconHtml = '';

            if (i <= displayStreak) {
                classes += ' completed';
                if (i === displayStreak) classes += ' today';
            }

            if (isLegendary) {
                classes += ' legendary';
            }

            let tooltipAttr = '';
            if (rewards[i]) {
                classes += ' reward-day';
                iconHtml = `<span class="streak-reward-icon large">${rewards[i].icon}</span>`;
                tooltipAttr = `data-tooltip="Reward: ${rewards[i].amount} CP"`;
            }

            html += `
                <div class="${classes}" ${tooltipAttr}>
                    <span class="day-number">${i}</span>
                    ${iconHtml}
                </div>
            `;
        }

        html += '</div>';
        this.streakRoadmap.innerHTML = html;

        // Update header flame color if legendary
        if (isLegendary) {
            const streakIcon = document.querySelector('.streak-icon');
            if (streakIcon) streakIcon.textContent = '🔵';
        }
    }
}

export default new StreakUI();
