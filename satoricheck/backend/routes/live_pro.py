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

# In-memory session tracking REMOVED in favor of Database State
# active_sessions = {}


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
        if (datetime.utcnow() - existing_session.last_heartbeat).total_seconds() > 60:
            # Stale session, close it
            existing_session.status = 'abandoned'
            existing_session.ended_at = datetime.utcnow()
            db_session.commit()
            logger.info(f"Auto-closed stale session {existing_session.id} for user {user.email}")
        else:
            raise APIError('You already have an active Live Pro session. Please close it first.', status_code=409)
    
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
        last_billed_at=datetime.utcnow(),
        status='active',
        language=language,
        device_id=device_id
    )
    db_session.add(session)
    db_session.commit()
    
    # In-memory tracking removed
    
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
        
        session_id = data.get('session_id')
        if not session_id:
            raise APIError('Invalid session ID', status_code=404)
        
        user = request.current_user
        
        # Get session from DB
        session = db_session.query(LiveProSession).get(session_id)
        if not session or session.status != 'active':
            # Client might be out of sync, tell them to stop
            return jsonify({'status': 'invalid_session'})
            
        if session.user_id != user.id:
            raise APIError('Unauthorized', status_code=403)
        
        # Update heartbeat timestamp
        session.last_heartbeat = datetime.utcnow()
        
        # Check if 30 seconds elapsed since last billing
        # Use DB timestamps (UTC)
        now = datetime.utcnow()
        last_billing = session.last_billed_at
        
        elapsed_since_billing = (now - last_billing).total_seconds()
        
        if elapsed_since_billing >= 30:
            # Calculate elapsed time and deduct CP
            minutes_used = elapsed_since_billing / 60.0
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
                session.cp_consumed += cp_to_deduct
                session.duration_seconds = int((datetime.utcnow() - session.started_at).total_seconds())
                session.last_billed_at = now  # Update billing timer
                
                # Record transaction
                transaction = Transaction(
                    user_id=user.id,
                    type='deduction',
                    amount=-cp_to_deduct,
                    description=f'Live Pro session {session_id} ({int(elapsed_since_billing)}s)',
                    timestamp=datetime.utcnow()
                )
                db_session.add(transaction)
                db_session.commit()
                
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
        # Check final billing segment
        last_billing_time = session.last_billed_at
        remaining_seconds = (datetime.utcnow() - last_billing_time).total_seconds()
        
        if remaining_seconds > 0:
            # Grace Period Check (4s)
            potential_total_duration = (datetime.utcnow() - session.started_at).total_seconds()
            
            if potential_total_duration < 4:
                logger.info(f"Session {session_id} ended within grace period ({potential_total_duration}s). No charge.")
                # Do NOT deduct.
            else:
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
                        description=f'Live Pro session {session_id} (final {int(remaining_seconds)}s)',
                        timestamp=datetime.utcnow()
                    )
                    db_session.add(transaction)
        
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
        
        # Query DB for active sessions
        active_sessions_list = db_session.query(LiveProSession).filter_by(status='active').all()
        
        for session in active_sessions_list:
            # Check for abandoned (no heartbeat for 60s)
            time_since_heartbeat = (datetime.utcnow() - session.last_heartbeat).total_seconds()
            if time_since_heartbeat > 60:
                abandoned_ids.append(session.id)
            # Check for hard timeout (session running > 2 hours)
            elif (datetime.utcnow() - session.started_at).total_seconds() > MAX_SESSION_DURATION:
                timeout_ids.append(session.id)
        
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
    except Exception as e:
        logger.error(f"Error closing session {session_id}: {e}")
        db_session.rollback()
