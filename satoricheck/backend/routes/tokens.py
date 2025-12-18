"""
Token management routes.
Handles token balance, deductions, and transaction history.
"""
from flask import Blueprint, request, jsonify
import logging
from datetime import datetime

from backend.database import db_session
from backend.models import TokenBalance, Transaction, Streak
from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.services.streak import get_streak_info

logger = logging.getLogger(__name__)

tokens_bp = Blueprint('tokens', __name__, url_prefix='/api/tokens')


@tokens_bp.route('/balance', methods=['GET'])
@login_required
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
    
    return jsonify({
        'success': True,
        'balance': token_balance.balance,
        'is_wizard': token_balance.is_wizard,
        'streak': streak_info
    })


@tokens_bp.route('/deduct', methods=['POST'])
@login_required
def deduct_tokens():
    """Deduct tokens from user balance."""
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
        token_balance.last_updated = datetime.utcnow()
        
        # Record transaction
        transaction = Transaction(
            user_id=user.id,
            type='deduction',
            amount=-amount,
            description=description,
            timestamp=datetime.utcnow()
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
        'transactions': [
            {
                'id': t.id,
                'type': t.type,
                'amount': t.amount,
                'description': t.description,
                'timestamp': t.timestamp.isoformat()
            }
            for t in transactions
        ]
    })
