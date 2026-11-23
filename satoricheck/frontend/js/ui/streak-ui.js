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
        const currentStreak = Math.max(0, Number(streakData.current_streak) || 0);
        const claimedUpTo = Math.max(0, Number(streakData.cycle_reward_high_water) || 0);

        // Calculate Cycle (Prestige Level)
        const cycle = currentStreak > 0 ? Math.floor((currentStreak - 1) / 30) : 0;
        const displayStreak = currentStreak > 0 ? ((currentStreak - 1) % 30) + 1 : 0;

        const isLegendary = cycle > 0;
        const themeClass = isLegendary ? 'legendary' : '';

        // Reward schedule
        const rewards = {
            3: { icon: '🔋', amount: 10 },
            6: { icon: '🔋', amount: 25 },
            10: { icon: '☕', amount: 40 },
            14: { icon: '🔋', amount: 50 },
            21: { icon: '⚡', amount: 100 },
            30: { icon: '💎', amount: 250 }
        };

        const infoContainer = document.createElement('div');
        infoContainer.className = isLegendary ? 'streak-info-container legendary' : 'streak-info-container';

        const countLarge = document.createElement('span');
        countLarge.className = isLegendary ? 'streak-count-large legendary' : 'streak-count-large';
        countLarge.appendChild(document.createTextNode(`${currentStreak} Days `));
        if (isLegendary) {
            const badge = document.createElement('span');
            badge.className = 'legendary-badge';
            badge.textContent = '👑';
            countLarge.appendChild(badge);
        }

        const subtitle = document.createElement('p');
        subtitle.className = 'modal-subtitle';
        subtitle.textContent = isLegendary ? 'Legendary Streak Active! Blue Flame Ignited.' : 'Keep the energy flowing!';

        infoContainer.append(countLarge, subtitle);

        const grid = document.createElement('div');
        grid.className = isLegendary ? `streak-grid ${themeClass}` : 'streak-grid';

        for (let i = 1; i <= 30; i++) {
            const dayEl = document.createElement('div');
            dayEl.className = 'streak-day';

            if (i <= displayStreak) {
                dayEl.classList.add('completed');
                if (i === displayStreak) dayEl.classList.add('today');
            }

            if (isLegendary) {
                dayEl.classList.add('legendary');
            }

            const dayNumber = document.createElement('span');
            dayNumber.className = 'day-number';
            dayNumber.textContent = String(i);
            dayEl.appendChild(dayNumber);

            if (rewards[i]) {
                const isClaimedInCycle = i <= claimedUpTo;
                dayEl.classList.add('reward-day');
                if (isClaimedInCycle) {
                    dayEl.classList.add('claimed');
                    dayEl.setAttribute('data-tooltip', `Claimed this cycle (${rewards[i].amount} CP)`);
                } else {
                    dayEl.setAttribute('data-tooltip', `Reward: ${rewards[i].amount} CP`);
                }
                const rewardIcon = document.createElement('span');
                rewardIcon.className = 'streak-reward-icon large';
                rewardIcon.textContent = rewards[i].icon;
                dayEl.appendChild(rewardIcon);
            }

            grid.appendChild(dayEl);
        }

        this.streakRoadmap.replaceChildren(infoContainer, grid);

        // Update header flame color if legendary
        if (isLegendary) {
            const streakIcon = document.querySelector('.streak-icon');
            if (streakIcon) streakIcon.textContent = '🔵';
        }
    }
}

export default new StreakUI();
