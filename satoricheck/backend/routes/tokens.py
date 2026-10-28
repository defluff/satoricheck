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


def reserve_word_tokens(user_id: int, word_count: int, min_cost: int = 0, error_status: int = 403) -> tuple[TokenBalance, int, int]:
    """Atomically reserve tokens for word-accumulated operations and commit immediately.

    Committing before external LLM calls prevents TOCTOU race conditions (parallel
    requests bypassing balance checks) and avoids holding open DB transactions across
    slow network I/O.

    Returns:
        Tuple of (token_balance, token_cost, previous_unbilled_words)
    """
    from backend.config import Config

    token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).with_for_update().first()
    if not token_balance:
        raise APIError('No token balance found', status_code=403)

    previous_unbilled = token_balance.unbilled_words or 0
    total_unbilled = previous_unbilled + word_count

    token_cost = (total_unbilled // Config.WORDS_PER_CP) * Config.TOKENS_PER_CP_UNIT
    remainder_words = total_unbilled % Config.WORDS_PER_CP

    if min_cost > 0 and token_cost > 0:
        token_cost = max(min_cost, token_cost)

    min_required = max(1, token_cost)
    if token_balance.balance < min_required:
        verb = "have" if error_status == 402 else "you have"
        raise APIError(
            f'Insufficient tokens. Need {min_required} CP, {verb} {token_balance.balance} CP',
            status_code=error_status
        )

    token_balance.balance -= token_cost
    token_balance.unbilled_words = remainder_words
    token_balance.last_updated = datetime.now(UTC)
    db_session.commit()

    return token_balance, token_cost, previous_unbilled


def refund_word_tokens(user_id: int, token_cost: int, previous_unbilled: int) -> None:
    """Refund a previously committed word-token reservation after an upstream failure."""
    try:
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).with_for_update().first()
        if token_balance:
            token_balance.balance += token_cost
            token_balance.unbilled_words = previous_unbilled
            token_balance.last_updated = datetime.now(UTC)
            db_session.commit()
    except Exception as refund_err:
        db_session.rollback()
        logger.error(f"Failed to refund word tokens for user {user_id}: {refund_err}")


def reserve_flat_tokens(user_id: int, cost: int, error_message: str | None = None, auto_create: bool = False) -> TokenBalance:
    """Atomically reserve a flat token cost and commit immediately before external I/O."""
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).with_for_update().first()
    if not token_balance:
        if auto_create:
            token_balance = TokenBalance(user_id=user_id, balance=0)
            db_session.add(token_balance)
            db_session.flush()
        else:
            raise APIError(error_message or 'No token balance found', status_code=403)

    if token_balance.balance < cost:
        raise APIError(
            error_message or f'Insufficient tokens. Analysis requires {cost} CP.',
            status_code=403
        )

    token_balance.balance -= cost
    token_balance.last_updated = datetime.now(UTC)
    db_session.commit()
    return token_balance


def refund_flat_tokens(user_id: int, cost: int) -> None:
    """Refund a previously committed flat token reservation after an upstream failure."""
    try:
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).with_for_update().first()
        if token_balance:
            token_balance.balance += cost
            token_balance.last_updated = datetime.now(UTC)
            db_session.commit()
    except Exception as refund_err:
        db_session.rollback()
        logger.error(f"Failed to refund {cost} CP for user {user_id}: {refund_err}")


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
