"""
Streak calculation and management service.
"""
from datetime import datetime, timedelta
from backend.config import Config


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
