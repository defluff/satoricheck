"""
Streak & Cycle-Locked Reward Tests.
Verifies milestone rewards, per-cycle anti-farming lock, Day 30 reset, and API exposure.
"""
from datetime import datetime, timedelta, UTC
import pytest
from backend.models import Streak, TokenBalance, Transaction
from backend.services.streak import (
    STREAK_REWARDS,
    check_and_grant_streak_reward,
    get_streak_info,
    handle_login_streak,
)


class TestStreakCycleLock:
    """Test per-30-day-cycle reward lock and Day 30 cycle reset."""

    @pytest.mark.parametrize(
        "day,expected_amount,expected_phrase",
        [
            (3, 10, "You're on a roll!"),
            (6, 25, "Mojo Rising!"),
            (10, 40, "You got yourself a coffee on the house!"),
            (14, 50, "Two Weeks Strong!"),
            (21, 100, "Habit Master!"),
            (30, 250, "LEGENDARY!"),
        ],
    )
    def test_milestone_rewards_and_messages(
        self, test_user, db_session_fixture, day, expected_amount, expected_phrase
    ):
        """Each scheduled milestone day grants the configured CP amount and message."""
        streak = db_session_fixture.query(Streak).filter_by(user_id=test_user.id).first()
        streak.current_streak = day
        streak.longest_streak = day
        streak.cycle_reward_high_water = 0
        db_session_fixture.commit()

        initial_balance = (
            db_session_fixture.query(TokenBalance)
            .filter_by(user_id=test_user.id)
            .first()
            .balance
        )

        amount, message = check_and_grant_streak_reward(
            test_user.id, day, db_session_fixture, streak_obj=streak
        )
        db_session_fixture.commit()

        assert amount == expected_amount
        assert STREAK_REWARDS[day] == expected_amount
        assert expected_phrase in message

        updated_balance = (
            db_session_fixture.query(TokenBalance)
            .filter_by(user_id=test_user.id)
            .first()
            .balance
        )
        assert updated_balance == initial_balance + expected_amount

        expected_high_water = 0 if day == 30 else day
        assert streak.cycle_reward_high_water == expected_high_water

        tx = (
            db_session_fixture.query(Transaction)
            .filter_by(user_id=test_user.id, type="bonus")
            .order_by(Transaction.id.desc())
            .first()
        )
        assert tx is not None
        assert tx.amount == expected_amount
        assert tx.description == message

    @pytest.mark.parametrize(
        "break_milestone,next_milestone,expected_next_reward",
        [
            (3, 6, 25),
            (6, 10, 40),
            (10, 14, 50),
            (14, 21, 100),
            (21, 30, 250),
        ],
    )
    def test_broken_streak_cannot_refarm_claimed_milestones_before_day_30(
        self,
        test_user,
        db_session_fixture,
        break_milestone,
        next_milestone,
        expected_next_reward,
    ):
        """
        Breaking a streak at ANY claimed milestone resets current_streak to 1,
        preserves cycle_reward_high_water, blocks re-claiming all previously
        passed milestones in the same 30-day cycle, and unlocks the next milestone
        once reached.
        """
        streak = db_session_fixture.query(Streak).filter_by(user_id=test_user.id).first()
        streak.current_streak = break_milestone - 1
        streak.longest_streak = break_milestone - 1
        streak.cycle_reward_high_water = 0
        streak.last_active_date = datetime.now(UTC) - timedelta(days=1)
        db_session_fixture.commit()

        # Step 1: Reach break_milestone naturally and claim its reward
        res_break = handle_login_streak(test_user.id, db_session_fixture)
        db_session_fixture.commit()
        assert res_break["streak_count"] == break_milestone
        assert res_break["reward_granted"] == STREAK_REWARDS[break_milestone]
        assert streak.cycle_reward_high_water == break_milestone

        balance_after_milestone = (
            db_session_fixture.query(TokenBalance)
            .filter_by(user_id=test_user.id)
            .first()
            .balance
        )

        # Step 2: Break streak by missing a day (last active 2 days ago)
        streak.last_active_date = datetime.now(UTC) - timedelta(days=2)
        db_session_fixture.commit()

        login_res = handle_login_streak(test_user.id, db_session_fixture)
        db_session_fixture.commit()
        assert login_res["streak_count"] == 1
        assert login_res["reward_granted"] == 0
        # High water mark must be preserved even after streak breaks
        assert streak.cycle_reward_high_water == break_milestone

        # Step 3: Climb through all milestones <= break_milestone again -> all locked
        claimed_milestones = [
            m for m in sorted(STREAK_REWARDS.keys()) if m <= break_milestone
        ]
        for locked_day in claimed_milestones:
            # Shift bonus timestamp so daily rate limit does not mask the milestone lock check
            db_session_fixture.query(Transaction).filter_by(
                user_id=test_user.id, type="bonus"
            ).update({"timestamp": datetime.now(UTC) - timedelta(days=2)})

            streak.current_streak = locked_day - 1
            streak.last_active_date = datetime.now(UTC) - timedelta(days=1)
            db_session_fixture.commit()

            res = handle_login_streak(test_user.id, db_session_fixture)
            db_session_fixture.commit()
            assert res["streak_count"] == locked_day
            assert res["reward_granted"] == 0
            assert res["reward_message"] is None
            assert streak.cycle_reward_high_water == break_milestone

        balance_mid = (
            db_session_fixture.query(TokenBalance)
            .filter_by(user_id=test_user.id)
            .first()
            .balance
        )
        assert balance_mid == balance_after_milestone

        # Step 4: Reaching next_milestone (> break_milestone) unlocks its reward
        db_session_fixture.query(Transaction).filter_by(
            user_id=test_user.id, type="bonus"
        ).update({"timestamp": datetime.now(UTC) - timedelta(days=2)})

        streak.current_streak = next_milestone - 1
        streak.last_active_date = datetime.now(UTC) - timedelta(days=1)
        db_session_fixture.commit()

        res_next = handle_login_streak(test_user.id, db_session_fixture)
        db_session_fixture.commit()
        assert res_next["streak_count"] == next_milestone
        assert res_next["reward_granted"] == expected_next_reward
        expected_high_water = 0 if next_milestone == 30 else next_milestone
        assert streak.cycle_reward_high_water == expected_high_water

        balance_after = (
            db_session_fixture.query(TokenBalance)
            .filter_by(user_id=test_user.id)
            .first()
            .balance
        )
        assert balance_after == balance_after_milestone + expected_next_reward

    def test_completing_day_30_resets_cycle_and_unlocks_next_cycle_day_3(
        self, test_user, db_session_fixture
    ):
        """Claiming Day 30 resets cycle_reward_high_water=0 so Cycle 2 Day 3 pays out."""
        streak = db_session_fixture.query(Streak).filter_by(user_id=test_user.id).first()
        streak.current_streak = 29
        streak.longest_streak = 29
        streak.cycle_reward_high_water = 21
        streak.last_active_date = datetime.now(UTC) - timedelta(days=1)
        db_session_fixture.commit()

        initial_balance = (
            db_session_fixture.query(TokenBalance)
            .filter_by(user_id=test_user.id)
            .first()
            .balance
        )

        res_30 = handle_login_streak(test_user.id, db_session_fixture)
        db_session_fixture.commit()
        assert res_30["streak_count"] == 30
        assert res_30["reward_granted"] == 250
        assert streak.cycle_reward_high_water == 0

        # Clear today's bonus transaction timestamp back 1 day to simulate Cycle 2 Day 3
        tx = (
            db_session_fixture.query(Transaction)
            .filter_by(user_id=test_user.id, type="bonus")
            .order_by(Transaction.id.desc())
            .first()
        )
        tx.timestamp = datetime.now(UTC) - timedelta(days=3)

        streak.current_streak = 32
        streak.longest_streak = 32
        streak.last_active_date = datetime.now(UTC) - timedelta(days=1)
        db_session_fixture.commit()

        res_33 = handle_login_streak(test_user.id, db_session_fixture)
        db_session_fixture.commit()
        assert res_33["streak_count"] == 33
        assert res_33["reward_granted"] == 10
        assert streak.cycle_reward_high_water == 3

        final_balance = (
            db_session_fixture.query(TokenBalance)
            .filter_by(user_id=test_user.id)
            .first()
            .balance
        )
        assert final_balance == initial_balance + 260

    def test_zero_streak_does_not_wrap_to_day_30(self, test_user, db_session_fixture):
        """Guard against Python modulo wraparound (-1 % 30 + 1 == 30) on zero streak."""
        amount, message = check_and_grant_streak_reward(
            test_user.id, 0, db_session_fixture
        )
        assert amount == 0
        assert message is None

    def test_tokens_balance_exposes_cycle_reward_high_water(
        self, auth_client, test_user, db_session_fixture
    ):
        """GET /api/tokens/balance returns streak.cycle_reward_high_water for UI state."""
        streak = db_session_fixture.query(Streak).filter_by(user_id=test_user.id).first()
        streak.current_streak = 2
        streak.cycle_reward_high_water = 10
        db_session_fixture.commit()

        response = auth_client.get("/api/tokens/balance")
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["streak"]["current_streak"] == 2
        assert data["streak"]["cycle_reward_high_water"] == 10

        info = get_streak_info(2, 10)
        assert info["cycle_reward_high_water"] == 10
