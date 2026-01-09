"""
Live Pro routes.
Handles Live Pro session management and time-based billing with heartbeat monitoring.
"""
from flask import Blueprint, request, jsonify
import logging
import time
from datetime import datetime, timedelta

from backend.database import db_session
from backend.models import TokenBalance, Transaction, LiveProSession
from backend.routes.auth import login_required
from backend.config import Config
from backend.error_handlers import APIError
from backend.services.deepgram_service import get_deepgram_service

logger = logging.getLogger(__name__)

live_pro_bp = Blueprint('live_pro', __name__, url_prefix='/api/live-pro')

# In-memory session tracking for heartbeat monitoring
# Key: session_id, Value: {user_id, last_heartbeat_time, last_billing_time}
active_sessions = {}


@live_pro_bp.route('/config', methods=['GET'])
@login_required
def get_live_pro_config():
    """Get Live Pro configuration and availability."""
    user = request.current_user
    deepgram = get_deepgram_service()
    
    # Get user's token balance
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    balance = token_balance.balance if token_balance else 0
    
    return jsonify({
        'success': True,
        'available': deepgram.is_available(),
        'cp_per_minute': Config.LIVE_PRO_CP_PER_MINUTE,
        'balance': balance,
        'websocket_url': deepgram.get_websocket_url() if deepgram.is_available() else None
        # SECURITY: auth_header removed - implement WebSocket proxy instead
    })


@live_pro_bp.route('/start', methods=['POST'])
@login_required
def start_session():
    """Start a Live Pro transcription session."""
    user = request.current_user
    deepgram = get_deepgram_service()
    
    if not deepgram.is_available():
        raise APIError('Live Pro is not available', status_code=503)
    
    # ABUSE PREVENTION: Check for existing active session (1 per user limit)
    existing_session = db_session.query(LiveProSession).filter_by(
        user_id=user.id,
        status='active'
    ).first()
    
    if existing_session:
        # Check if it's actually stale (no heartbeat in 60s) - if so, auto-close it
        if existing_session.id in active_sessions:
            last_heartbeat = active_sessions[existing_session.id].get('last_heartbeat', 0)
            if time.time() - last_heartbeat > 60:
                # Stale session, close it
                existing_session.status = 'abandoned'
                existing_session.ended_at = datetime.utcnow()
                del active_sessions[existing_session.id]
                db_session.commit()
                logger.info(f"Auto-closed stale session {existing_session.id} for user {user.email}")
            else:
                raise APIError('You already have an active Live Pro session. Please close it first.', status_code=409)
        else:
            # Session in DB but not in memory - mark as abandoned
            existing_session.status = 'abandoned'
            existing_session.ended_at = datetime.utcnow()
            db_session.commit()
            logger.info(f"Cleaned up orphaned session {existing_session.id} for user {user.email}")
    
    # Check balance
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    if not token_balance or token_balance.balance < Config.LIVE_PRO_CP_PER_MINUTE:
        raise APIError(
            f'Insufficient balance for Live Pro. Need at least {Config.LIVE_PRO_CP_PER_MINUTE} CP',
            status_code=403
        )
    
    # Get optional parameters
    data = request.get_json() or {}
    language = data.get('language', 'en')
    device_id = data.get('device_id')
    
    # Create session in database
    session = LiveProSession(
        user_id=user.id,
        started_at=datetime.utcnow(),
        last_heartbeat=datetime.utcnow(),
        status='active',
        language=language,
        device_id=device_id
    )
    db_session.add(session)
    db_session.commit()
    
    # Track in memory for heartbeat monitoring
    active_sessions[session.id] = {
        'user_id': user.id,
        'last_heartbeat': time.time(),
        'last_billing': time.time(),
        'started_at': time.time()  # Track session start for max duration
    }
    
    logger.info(f"Live Pro session {session.id} started for user {user.email}, balance: {token_balance.balance} CP")
    
    # Build the proxy WebSocket URL (browser connects here, we proxy to Deepgram)
    # Use wss:// in production, ws:// in development
    ws_protocol = 'wss' if Config.ENV == 'production' else 'ws'
    proxy_url = f"{ws_protocol}://{request.host}/api/livepro/ws/{session.id}"
    
    return jsonify({
        'success': True,
        'session_id': session.id,
        'websocket_url': proxy_url,  # Points to OUR proxy, not Deepgram directly
        'cp_per_minute': Config.LIVE_PRO_CP_PER_MINUTE,
        'balance': token_balance.balance,
        'max_duration_seconds': 7200  # Inform client of 2-hour limit
    })


@live_pro_bp.route('/deduct', methods=['POST'])
@login_required
def deduct_time():
    """
    Deduct CP based on time used.
    Called periodically by frontend during a Live Pro session.
    
    Expected payload:
    {
        "seconds": 30  // Time elapsed since last deduction
    }
    """
    try:
        data = request.get_json()
        if not data:
            raise APIError('No data provided')
        
        seconds = data.get('seconds', 0)
        if seconds <= 0:
            raise APIError('Invalid seconds value')
        
        user = request.current_user
        
        # Calculate CP to deduct (1 CP per 60 seconds, round up)
        # Standardized: same rounding as heartbeat billing
        minutes_used = seconds / 60.0
        cp_to_deduct = max(1, int(minutes_used + 0.5))  # Round up, minimum 1 CP
        
        # Get token balance
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance:
            raise APIError('No token balance found', status_code=403)
        
        # Check if enough balance
        if token_balance.balance < cp_to_deduct:
            # Deduct what's left
            cp_to_deduct = token_balance.balance
            out_of_credits = True
        else:
            out_of_credits = False
        
        # Deduct
        token_balance.balance -= cp_to_deduct
        token_balance.last_updated = datetime.utcnow()
        
        # Record transaction (aggregate multiple deductions into one per session later)
        transaction = Transaction(
            user_id=user.id,
            type='deduction',
            amount=-cp_to_deduct,
            description=f'Live Pro ({seconds}s)',
            timestamp=datetime.utcnow()
        )
        db_session.add(transaction)
        db_session.commit()
        
        logger.info(f"Live Pro deduction: {cp_to_deduct} CP for {seconds}s, user {user.email}, remaining: {token_balance.balance}")
        
        return jsonify({
            'success': True,
            'cp_deducted': cp_to_deduct,
            'new_balance': token_balance.balance,
            'out_of_credits': out_of_credits
        })
        
    except APIError:
        db_session.rollback()
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Live Pro deduction error: {e}", exc_info=True)
        raise APIError('Failed to process Live Pro billing')


@live_pro_bp.route('/heartbeat', methods=['POST'])
@login_required
def heartbeat():
    """
    Client sends heartbeat every 10 seconds.
    Server checks if 30 seconds elapsed since last billing and deducts CP if needed.
    """
    try:
        data = request.get_json()
        if not data:
            raise APIError('No data provided')
        
        session_id = data.get('session_id')
        if not session_id or session_id not in active_sessions:
            raise APIError('Invalid or expired session', status_code=404)
        
        user = request.current_user
        
        # Update heartbeat timestamp
        active_sessions[session_id]['last_heartbeat'] = time.time()
        
        # Update database
        session = db_session.query(LiveProSession).get(session_id)
        if session:
            session.last_heartbeat = datetime.utcnow()
        
        # Check if 30 seconds elapsed since last billing
        now = time.time()
        last_billing = active_sessions[session_id]['last_billing']
        
        if now - last_billing >= 30:
            # Calculate elapsed time and deduct CP
            elapsed_seconds = int(now - last_billing)
            minutes_used = elapsed_seconds / 60.0
            cp_to_deduct = max(1, int(minutes_used + 0.5))  # Round up, minimum 1 CP
            
            # Get token balance
            token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
            if not token_balance:
                raise APIError('No token balance found', status_code=403)
            
            # Check if enough balance
            if token_balance.balance >= cp_to_deduct:
                # Deduct CP
                token_balance.balance -= cp_to_deduct
                token_balance.last_updated = datetime.utcnow()
                
                # Update session
                if session:
                    session.cp_consumed += cp_to_deduct
                    session.duration_seconds = int((datetime.utcnow() - session.started_at).total_seconds())
                
                # Record transaction
                transaction = Transaction(
                    user_id=user.id,
                    type='deduction',
                    amount=-cp_to_deduct,
                    description=f'Live Pro session {session_id} ({elapsed_seconds}s)',
                    timestamp=datetime.utcnow()
                )
                db_session.add(transaction)
                db_session.commit()
                
                # Update last billing time
                active_sessions[session_id]['last_billing'] = now
                
                logger.info(f"Live Pro heartbeat billing: {cp_to_deduct} CP for session {session_id}")
                
                return jsonify({
                    'status': 'ok',
                    'cp_deducted': cp_to_deduct,
                    'new_balance': token_balance.balance
                })
            else:
                # Out of tokens - stop session
                return jsonify({'status': 'insufficient_balance'})
        
        db_session.commit()
        return jsonify({'status': 'ok'})
        
    except APIError:
        db_session.rollback()
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Live Pro heartbeat error: {e}", exc_info=True)
        raise APIError('Failed to process heartbeat')


@live_pro_bp.route('/end', methods=['POST'])
@login_required
def end_session():
    """
    End a Live Pro session.
    Final billing and cleanup.
    """
    try:
        data = request.get_json() or {}
        session_id = data.get('session_id')
        
        if not session_id:
            raise APIError('No session_id provided')
        
        user = request.current_user
        
        # Get session from database
        session = db_session.query(LiveProSession).get(session_id)
        if not session:
            raise APIError('Session not found', status_code=404)
        
        if session.user_id != user.id:
            raise APIError('Unauthorized', status_code=403)
        
        # Calculate final billing
        elapsed_seconds = int((datetime.utcnow() - session.started_at).total_seconds())
        
        # Check if there's time since last billing
        if session_id in active_sessions:
            last_billing_time = active_sessions[session_id]['last_billing']
            remaining_seconds = int(time.time() - last_billing_time)
            
            if remaining_seconds > 0:
                # Final deduction
                minutes_used = remaining_seconds / 60.0
                cp_to_deduct = max(1, int(minutes_used + 0.5))
                
                token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
                if token_balance and token_balance.balance >= cp_to_deduct:
                    token_balance.balance -= cp_to_deduct
                    token_balance.last_updated = datetime.utcnow()
                    session.cp_consumed += cp_to_deduct
                    
                    # Record final transaction
                    transaction = Transaction(
                        user_id=user.id,
                        type='deduction',
                        amount=-cp_to_deduct,
                        description=f'Live Pro session {session_id} (final {remaining_seconds}s)',
                        timestamp=datetime.utcnow()
                    )
                    db_session.add(transaction)
            
            # Remove from active tracking
            del active_sessions[session_id]
        
        # Update session status
        session.ended_at = datetime.utcnow()
        session.duration_seconds = elapsed_seconds
        session.status = 'completed'
        
        db_session.commit()
        
        logger.info(f"Live Pro session {session_id} ended for user {user.email}, total: {elapsed_seconds}s, CP consumed: {session.cp_consumed}")
        
        return jsonify({
            'success': True,
            'session_id': session_id,
            'total_seconds': elapsed_seconds,
            'cp_consumed': session.cp_consumed
        })
        
    except APIError:
        db_session.rollback()
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Live Pro end session error: {e}", exc_info=True)
        raise APIError('Failed to end Live Pro session')


def cleanup_abandoned_sessions():
    """
    Background task to clean up abandoned sessions.
    Should be called periodically (every 60 seconds).
    
    Handles:
    1. Sessions with no heartbeat for 60+ seconds (abandoned)
    2. Sessions exceeding 2-hour hard limit (abuse prevention)
    """
    MAX_SESSION_DURATION = 7200  # 2 hours in seconds
    
    try:
        now = time.time()
        abandoned_ids = []
        timeout_ids = []
        
        for session_id, data in list(active_sessions.items()):
            # Check for abandoned (no heartbeat for 60s)
            if now - data['last_heartbeat'] > 60:
                abandoned_ids.append(session_id)
            # Check for hard timeout (session running > 2 hours)
            elif 'started_at' in data and now - data['started_at'] > MAX_SESSION_DURATION:
                timeout_ids.append(session_id)
        
        # Clean up abandoned sessions
        for session_id in abandoned_ids:
            logger.warning(f"Abandoning session {session_id} - no heartbeat for 60s")
            _close_session(session_id, 'abandoned')
        
        # Clean up sessions exceeding hard limit
        for session_id in timeout_ids:
            logger.warning(f"Terminating session {session_id} - exceeded 2-hour limit")
            _close_session(session_id, 'timeout')
        
        total_cleaned = len(abandoned_ids) + len(timeout_ids)
        if total_cleaned:
            logger.info(f"Cleaned up {len(abandoned_ids)} abandoned + {len(timeout_ids)} timeout sessions")
            
    except Exception as e:
        logger.error(f"Error in cleanup_abandoned_sessions: {e}", exc_info=True)
        db_session.rollback()


def _close_session(session_id, status):
    """Helper to close a session with given status."""
    try:
        session = db_session.query(LiveProSession).get(session_id)
        if session and session.status == 'active':
            elapsed = (datetime.utcnow() - session.started_at).total_seconds()
            session.duration_seconds = int(elapsed)
            session.status = status
            session.ended_at = datetime.utcnow()
            db_session.commit()
        
        # Remove from active tracking
        if session_id in active_sessions:
            del active_sessions[session_id]
    except Exception as e:
        logger.error(f"Error closing session {session_id}: {e}")
        db_session.rollback()
