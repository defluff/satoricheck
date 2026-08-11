import logging
import json
from datetime import datetime, UTC

from flask import Blueprint, request, jsonify

from backend.database import db_session
from backend.models import TokenBalance, Transaction, Streak
from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.services.streak import get_streak_info
from backend.extensions import limiter

logger = logging.getLogger(__name__)

tokens_bp = Blueprint('tokens', __name__, url_prefix='/api/tokens')


@tokens_bp.route('/balance', methods=['GET'])
@login_required
@limiter.limit("60 per minute")
def get_balance():
    """Get current token balance and streak."""
    user = request.current_user
    
    # Get token balance
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    if not token_balance:
        # Create if doesn't exist
        token_balance = TokenBalance(user_id=user.id, balance=0)
        db_session.add(token_balance)
        db_session.commit()
    
    # Get streak
    streak = db_session.query(Streak).filter_by(user_id=user.id).first()
    current_streak = streak.current_streak if streak else 0
    
    # Get streak info
    streak_info = get_streak_info(current_streak)
    
    # Check if a reward was granted today (for frontend toast)
    today_reward = None
    start_of_day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_bonus = db_session.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.type == 'bonus',
        Transaction.timestamp >= start_of_day
    ).first()
    
    if recent_bonus:
        today_reward = {
            'amount': recent_bonus.amount,
            'message': recent_bonus.description
        }
    
    return jsonify({
        'success': True,
        'balance': token_balance.balance,
        'is_wizard': token_balance.is_wizard,
        'streak': streak_info,
        'today_reward': today_reward
    })


@tokens_bp.route('/deduct', methods=['POST'])
@login_required
@limiter.limit("20 per minute")
def deduct_tokens():
    """Deduct tokens from user balance."""
    user = request.current_user
    logger.info(f"Token deduction request from user {user.email}")
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        amount = data.get('amount')
        description = data.get('description', 'Fact check')
        
        if not amount or amount <= 0:
            raise APIError('Invalid amount')
        
        user = request.current_user
        
        # Get token balance
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance or token_balance.balance < amount:
            raise APIError('Insufficient token balance', status_code=403)
        
        # Deduct tokens
        token_balance.balance -= amount
        token_balance.last_updated = datetime.now(UTC)
        
        # Record transaction
        transaction = Transaction(
            user_id=user.id,
            type='deduction',
            amount=-amount,
            description=description,
            timestamp=datetime.now(UTC)
        )
        db_session.add(transaction)
        
        db_session.commit()
        
        logger.info(f"Deducted {amount} CP from user {user.email}. New balance: {token_balance.balance}")
        
        return jsonify({
            'success': True,
            'new_balance': token_balance.balance,
            'amount_deducted': amount
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Token deduction error: {e}", exc_info=True)
        raise APIError('Failed to deduct tokens')


@tokens_bp.route('/history', methods=['GET'])
@login_required
def get_transaction_history():
    """Get user's transaction history."""
    user = request.current_user
    
    # Get transactions
    transactions = db_session.query(Transaction).filter_by(user_id=user.id).order_by(Transaction.timestamp.desc()).limit(50).all()
    
    return jsonify({
        'success': True,
        'transactions': [t.to_dict() for t in transactions]
    })
