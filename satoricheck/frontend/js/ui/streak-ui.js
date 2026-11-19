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
        const claimedUpTo = streakData.cycle_reward_high_water || 0;

        // Calculate Cycle (Prestige Level)
        const cycle = Math.floor((currentStreak - 1) / 30);
        const displayStreak = (currentStreak - 1) % 30 + 1;

        const isLegendary = cycle > 0;
        const themeClass = isLegendary ? 'legendary' : '';
        const badgeHtml = isLegendary ? '<span class="legendary-badge">👑</span>' : '';

        // Reward schedule
        const rewards = {
            3: { icon: '🔋', amount: 10 },
            6: { icon: '🔋', amount: 25 },
            10: { icon: '☕', amount: 40 },
            14: { icon: '🔋', amount: 50 },
            21: { icon: '⚡', amount: 100 },
            30: { icon: '💎', amount: 250 }
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
                const isClaimedInCycle = i <= claimedUpTo;
                classes += isClaimedInCycle ? ' reward-day claimed' : ' reward-day';
                iconHtml = `<span class="streak-reward-icon large">${rewards[i].icon}</span>`;
                tooltipAttr = isClaimedInCycle
                    ? `data-tooltip="Claimed this cycle (${rewards[i].amount} CP)"`
                    : `data-tooltip="Reward: ${rewards[i].amount} CP"`;
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
