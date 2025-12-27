"""
Streak calculation and management service.
"""
from datetime import datetime, timedelta
from backend.config import Config


# Streak reward schedule (day -> CP amount)
STREAK_REWARDS = {
    6: 100,
    14: 200,
    21: 400,
    30: 1000
}


def update_streak(streak_obj, last_active):
    """
    Update user's streak based on last active date.
    
    Args:
        streak_obj: Streak database object
        last_active: DateTime of last activity
        
    Returns:
        Updated streak count
    """
    if not last_active:
        # First time user
        streak_obj.current_streak = 1
        streak_obj.longest_streak = 1
        streak_obj.last_active_date = datetime.utcnow()
        return 1
    
    today = datetime.utcnow().date()
    last_date = last_active.date()
    
    # Check if already logged in today
    if last_date == today:
        return streak_obj.current_streak
    
    # Check if logged in yesterday (continue streak)
    yesterday = today - timedelta(days=1)
    if last_date == yesterday:
        streak_obj.current_streak += 1
        if streak_obj.current_streak > streak_obj.longest_streak:
            streak_obj.longest_streak = streak_obj.current_streak
    else:
        # Streak broken, reset to 1
        streak_obj.current_streak = 1
    
    streak_obj.last_active_date = datetime.utcnow()
    return streak_obj.current_streak


def check_and_grant_streak_reward(user_id, current_streak, db_session):
    """
    Check if user qualifies for a streak reward and grant it.
    
    Args:
        user_id: User ID
        current_streak: Current streak count
        db_session: Database session
        
    Returns:
        Tuple (reward_amount, reward_message) if granted, else (0, None)
    """
    from backend.models import TokenBalance, Transaction
    
    # Calculate cycle day (rewards repeat every 30 days)
    cycle_day = (current_streak - 1) % 30 + 1
    
    if cycle_day not in STREAK_REWARDS:
        return 0, None
    
    reward_amount = STREAK_REWARDS[cycle_day]
    
    # Check if we already gave a bonus today
    start_of_day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    existing_bonus = db_session.query(Transaction).filter(
        Transaction.user_id == user_id,
        Transaction.type == 'bonus',
        Transaction.timestamp >= start_of_day
    ).first()
    
    if existing_bonus:
        return 0, None  # Already granted today
    
    # Grant reward
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
    if not token_balance:
        return 0, None
    
    token_balance.balance += reward_amount
    
    # Build reward message
    messages = {
        6: "Mojo Rising! +100 CP Reward",
        14: "Two Weeks Strong! +200 CP Reward",
        21: "Habit Master! +400 CP Reward",
        30: "LEGENDARY! +1000 CP Reward"
    }
    reward_message = messages.get(cycle_day, f"Streak Day {cycle_day} Reward")
    
    # Record transaction
    transaction = Transaction(
        user_id=user_id,
        type='bonus',
        amount=reward_amount,
        description=reward_message
    )
    db_session.add(transaction)
    
    return reward_amount, reward_message


def get_streak_info(streak_count):
    """
    Get streak milestone information.
    
    Args:
        streak_count: Current streak count
        
    Returns:
        Dict with milestone name and next milestone info
    """
    current_milestone = Config.get_streak_milestone(streak_count)
    
    # Find next milestone
    next_milestone = None
    next_count = None
    for count, name in Config.STREAK_MILESTONES:
        if count > streak_count:
            next_milestone = name
            next_count = count
            break
    
    return {
        'current_milestone': current_milestone,
        'current_streak': streak_count,
        'next_milestone': next_milestone,
        'next_count': next_count,
        'days_until_next': next_count - streak_count if next_count else 0
    }

