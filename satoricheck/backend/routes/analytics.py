"""
Analytics routes for share tracking.
Privacy-first: No user content stored, only platform counts.
"""
from flask import Blueprint, request, jsonify
import logging

from backend.database import db_session
from backend.models import ShareStats
from backend.routes.auth import login_required
from backend.error_handlers import APIError

logger = logging.getLogger(__name__)

analytics_bp = Blueprint('analytics', __name__, url_prefix='/api/analytics')


# Lazy limiter import to avoid circular dependency
def get_limiter():
    from backend.server import limiter
    return limiter


# Apply rate limiting via before_request hook (avoids decorator circular import)
@analytics_bp.before_request
def apply_rate_limit():
    """Apply rate limit: 10 per minute for analytics endpoints."""
    limiter = get_limiter()
    limiter.limit("10 per minute")(lambda: None)()


@analytics_bp.route('/share', methods=['POST'])
@login_required
def track_share():
    """Track share event without storing content.
    
    Privacy-first: Only records platform and timestamp.
    No user_id, no claim text, no verdict stored.
    """
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        platform = data.get('platform')
        
        # Validate platform enum
        valid_platforms = ['X', 'LinkedIn', 'Download']
        if platform not in valid_platforms:
            raise APIError(f'Invalid platform. Must be one of: {", ".join(valid_platforms)}')
        
        # Insert anonymized metric
        share_stat = ShareStats(platform=platform)
        db_session.add(share_stat)
        db_session.commit()
        
        logger.info(f"Share tracked: platform={platform}")
        
        return jsonify({'success': True})
        
    except APIError:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Share tracking error: {e}", exc_info=True)
        # Don't fail the share flow - just log and return success
        # This is fire-and-forget analytics
        return jsonify({'success': True})
