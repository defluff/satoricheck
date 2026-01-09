"""
WebSocket Proxy for Deepgram Live Pro.
Routes audio from browser → backend → Deepgram, keeping API key server-side.
"""
import asyncio
import logging
import time
from flask import Blueprint, request
from flask_sock import Sock
import websocket
import threading
import json

from backend.config import Config
from backend.database import db_session
from backend.models import LiveProSession, TokenBalance, Transaction
from backend.routes.auth import login_required
from backend.routes.live_pro import active_sessions
from datetime import datetime

logger = logging.getLogger(__name__)

# Create blueprint for WebSocket routes
ws_bp = Blueprint('ws', __name__)
sock = Sock()

# Deepgram WebSocket URL
DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


def init_websocket_proxy(app):
    """Initialize WebSocket support on Flask app."""
    sock.init_app(app)
    logger.info("✓ WebSocket proxy initialized")


@sock.route('/api/livepro/ws/<int:session_id>')
def ws_proxy(ws, session_id):
    """
    WebSocket proxy for Live Pro transcription.
    
    Flow:
    1. Browser connects to this endpoint
    2. We validate the session belongs to the user
    3. We open a WebSocket to Deepgram (with OUR API key)
    4. We relay audio from browser → Deepgram
    5. We relay transcripts from Deepgram → browser
    6. We handle billing server-side
    """
    logger.info(f"WebSocket proxy connection for session {session_id}")
    
    # Validate session exists and is active
    if session_id not in active_sessions:
        ws.send(json.dumps({'error': 'Invalid or expired session'}))
        ws.close()
        return
    
    session_data = active_sessions[session_id]
    user_id = session_data['user_id']
    
    # Get session from DB for language preference
    db_session_obj = db_session.query(LiveProSession).get(session_id)
    if not db_session_obj or db_session_obj.status != 'active':
        ws.send(json.dumps({'error': 'Session not active'}))
        ws.close()
        return
    
    language = db_session_obj.language or 'en'
    
    # Build Deepgram URL with parameters
    params = {
        "model": "nova-2",
        "language": language,
        "punctuate": "true",
        "interim_results": "true",
        "utterance_end_ms": "1000",
        "vad_events": "true"
    }
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    deepgram_url = f"{DEEPGRAM_WS_URL}?{query_string}"
    
    # Connect to Deepgram with our API key
    deepgram_ws = None
    try:
        deepgram_ws = websocket.create_connection(
            deepgram_url,
            header=[f"Authorization: Token {Config.DEEPGRAM_API_KEY}"]
        )
        logger.info(f"Connected to Deepgram for session {session_id}")
        
        # Update session heartbeat
        active_sessions[session_id]['last_heartbeat'] = time.time()
        
        # Start thread to receive from Deepgram and send to browser
        stop_event = threading.Event()
        
        def relay_from_deepgram():
            """Receive transcripts from Deepgram and send to browser."""
            try:
                while not stop_event.is_set():
                    try:
                        result = deepgram_ws.recv()
                        if result:
                            ws.send(result)
                    except websocket.WebSocketConnectionClosedException:
                        break
                    except Exception as e:
                        logger.error(f"Deepgram receive error: {e}")
                        break
            finally:
                stop_event.set()
        
        # Start relay thread
        relay_thread = threading.Thread(target=relay_from_deepgram, daemon=True)
        relay_thread.start()
        
        # Main loop: receive audio from browser and send to Deepgram
        last_billing_time = time.time()
        
        while not stop_event.is_set():
            try:
                # Receive audio data from browser
                audio_data = ws.receive(timeout=1)
                
                if audio_data is None:
                    # Connection closed
                    break
                
                # Forward to Deepgram
                if isinstance(audio_data, bytes):
                    deepgram_ws.send_binary(audio_data)
                else:
                    # Could be a control message
                    deepgram_ws.send(audio_data)
                
                # Update heartbeat
                active_sessions[session_id]['last_heartbeat'] = time.time()
                
                # Server-side billing every 30 seconds
                now = time.time()
                if now - last_billing_time >= 30:
                    elapsed = now - last_billing_time
                    bill_user(user_id, session_id, elapsed)
                    last_billing_time = now
                    
            except TimeoutError:
                # No data received, just continue
                continue
            except Exception as e:
                logger.error(f"WebSocket proxy error: {e}")
                break
        
        # Final billing for remaining time
        final_elapsed = time.time() - last_billing_time
        if final_elapsed > 5:  # Only bill if > 5 seconds
            bill_user(user_id, session_id, final_elapsed)
            
    except Exception as e:
        logger.error(f"Failed to connect to Deepgram: {e}")
        ws.send(json.dumps({'error': 'Failed to connect to transcription service'}))
    finally:
        # Cleanup
        stop_event.set()
        if deepgram_ws:
            try:
                deepgram_ws.close()
            except:
                pass
        logger.info(f"WebSocket proxy closed for session {session_id}")


def bill_user(user_id, session_id, elapsed_seconds):
    """Deduct CP based on elapsed time (server-controlled)."""
    try:
        minutes_used = elapsed_seconds / 60.0
        cp_to_deduct = max(1, int(minutes_used + 0.5))  # Round up, minimum 1 CP
        
        # Get token balance
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user_id).first()
        if not token_balance:
            return
        
        if token_balance.balance >= cp_to_deduct:
            token_balance.balance -= cp_to_deduct
            token_balance.last_updated = datetime.utcnow()
            
            # Update session CP consumed
            session_obj = db_session.query(LiveProSession).get(session_id)
            if session_obj:
                session_obj.cp_consumed = (session_obj.cp_consumed or 0) + cp_to_deduct
            
            # Record transaction
            transaction = Transaction(
                user_id=user_id,
                type='deduction',
                amount=-cp_to_deduct,
                description=f'Live Pro proxy session {session_id}',
                timestamp=datetime.utcnow()
            )
            db_session.add(transaction)
            db_session.commit()
            
            logger.info(f"Billed {cp_to_deduct} CP for session {session_id}")
        else:
            logger.warning(f"Insufficient balance for session {session_id}")
            
    except Exception as e:
        logger.error(f"Billing error: {e}")
        db_session.rollback()
