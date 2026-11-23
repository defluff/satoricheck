import logging
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


def reserve_flat_tokens(
    user_id: int,
    cost: int,
    error_message: str | None = None,
    auto_create: bool = False,
    error_status: int = 403,
    transaction_description: str | None = None,
    session=None,
) -> TokenBalance:
    """Atomically reserve a flat token cost and commit immediately before external I/O."""
    active_session = session if session is not None else db_session
    token_balance = active_session.query(TokenBalance).filter_by(user_id=user_id).with_for_update().first()
    if not token_balance:
        if auto_create:
            token_balance = TokenBalance(user_id=user_id, balance=0)
            active_session.add(token_balance)
            active_session.flush()
        else:
            raise APIError(error_message or 'No token balance found', status_code=403)

    if token_balance.balance < cost:
        if token_balance.auto_refill_enabled:
            from backend.routes.billing import maybe_trigger_auto_refill
            maybe_trigger_auto_refill(user_id, active_session, required_cost=cost)
            token_balance = active_session.query(TokenBalance).filter_by(user_id=user_id).with_for_update().first()

        if not token_balance or token_balance.balance < cost:
            default_message = (
                f'Insufficient tokens. Need {cost} CP, have {token_balance.balance if token_balance else 0} CP'
                if error_status == 402
                else f'Insufficient tokens. Analysis requires {cost} CP.'
            )
            payload = {'auto_refill_failed': True} if (token_balance and token_balance.auto_refill_failed) else None
            raise APIError(
                error_message or default_message,
                status_code=error_status,
                payload=payload,
            )

    token_balance.balance -= cost
    token_balance.last_updated = datetime.now(UTC)
    if transaction_description is not None:
        active_session.add(
            Transaction(
                user_id=user_id,
                type='deduction',
                amount=-cost,
                description=transaction_description,
                timestamp=datetime.now(UTC),
            )
        )
    try:
        active_session.commit()
    except Exception:
        active_session.rollback()
        raise
    return token_balance


def ensure_sufficient_tokens(
    user_id: int,
    required_cp: int,
    error_message: str,
    error_status: int = 403,
) -> TokenBalance:
    """Pre-check balance guard with off-session auto-refill when balance is insufficient.

    Never creates a missing TokenBalance row so callers preserve missing-row 403 guards.
    """
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
    if not token_balance:
        raise APIError(error_message, status_code=error_status)

    if token_balance.balance < required_cp:
        if token_balance.auto_refill_enabled:
            from backend.routes.billing import maybe_trigger_auto_refill
            maybe_trigger_auto_refill(user_id, db_session, required_cost=required_cp)
            token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()

        if not token_balance or token_balance.balance < required_cp:
            payload = {'auto_refill_failed': True} if (token_balance and token_balance.auto_refill_failed) else None
            raise APIError(error_message, status_code=error_status, payload=payload)

    return token_balance


def trigger_post_action_refill_if_empty(user_id: int, session=None) -> tuple[int, bool]:
    """Trigger off-session auto-refill if a successful action drained balance to 0 CP.

    Returns (current_balance, auto_refill_failed).
    """
    active_session = session if session is not None else db_session
    token_balance = active_session.query(TokenBalance).filter_by(user_id=user_id).first()
    if not token_balance:
        return 0, False

    if (token_balance.balance or 0) <= 0 and token_balance.auto_refill_enabled:
        from backend.routes.billing import maybe_trigger_auto_refill
        maybe_trigger_auto_refill(user_id, active_session, required_cost=0)
        token_balance = active_session.query(TokenBalance).filter_by(user_id=user_id).first()

    if not token_balance:
        return 0, False
    return token_balance.balance, bool(token_balance.auto_refill_failed)


def refund_flat_tokens(
    user_id: int,
    cost: int,
    *,
    transaction_description: str | None = None,
    session=None,
) -> bool:
    """Refund a previously committed flat token reservation after an upstream failure."""
    active_session = session if session is not None else db_session
    try:
        token_balance = active_session.query(TokenBalance).filter_by(user_id=user_id).with_for_update().first()
        if token_balance:
            token_balance.balance += cost
            token_balance.last_updated = datetime.now(UTC)
            if transaction_description is not None:
                active_session.add(
                    Transaction(
                        user_id=user_id,
                        type='refund',
                        amount=cost,
                        description=transaction_description,
                        timestamp=datetime.now(UTC),
                    )
                )
            active_session.commit()
        return True
    except Exception as refund_err:
        active_session.rollback()
        logger.error(f"Failed to refund {cost} CP for user {user_id}: {refund_err}")
        return False


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
    high_water = (streak.cycle_reward_high_water or 0) if streak else 0
    
    # Get streak info
    streak_info = get_streak_info(current_streak, high_water)
    
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
        'auto_refill_failed': bool(token_balance.auto_refill_failed),
        'streak': streak_info,
        'today_reward': today_reward
    })


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
