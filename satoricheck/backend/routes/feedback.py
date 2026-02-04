"""
Feedback routes for user engagement and voting.
"""
from flask import Blueprint, request, jsonify
import logging
from sqlalchemy.exc import IntegrityError

from backend.database import db_session
from backend.models import FeatureVote
from backend.routes.auth import login_required
from backend.error_handlers import APIError

logger = logging.getLogger(__name__)

feedback_bp = Blueprint('feedback', __name__, url_prefix='/api/feedback')


# Lazy limiter import to avoid circular dependency
def get_limiter():
    from backend.server import limiter
    return limiter


@feedback_bp.before_request
def apply_rate_limit():
    """Apply rate limit: 10 per minute for feedback endpoints."""
    limiter = get_limiter()
    limiter.limit("10 per minute")(lambda: None)()


@feedback_bp.route('/feature-vote', methods=['POST'])
@login_required
def feature_vote():
    """Record a user vote for a feature."""
    try:
        data = request.get_json()
        if not data or 'feature' not in data:
            raise APIError('Feature name is required')
            
        feature = data['feature']
        user_id = request.current_user.id
        
        # Create vote record
        vote = FeatureVote(
            user_id=user_id,
            feature=feature
        )
        
        db_session.add(vote)
        db_session.commit()
        
        logger.info(f"Feature vote recorded: user={user_id} feature={feature}")
        
        return jsonify({
            'success': True,
            'message': 'Vote recorded'
        })
        
    except IntegrityError:
        # User already voted for this feature
        db_session.rollback()
        logger.info(f"Duplicate vote ignored: user={request.current_user.id} feature={data.get('feature')}")
        return jsonify({
            'success': True,
            'message': 'Vote already recorded'
        })
        
    except APIError:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Feature vote error: {e}", exc_info=True)
        # Return success to frontend even on error to not disrupt UX
        return jsonify({'success': True})
