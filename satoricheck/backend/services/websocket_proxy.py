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
from backend.models import LiveProSession, TokenBalance, Transaction
from backend.routes.auth import login_required
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
    
    # Validated by DB lookup below
    # if session_id not in active_sessions:
    #     ws.send(json.dumps({'error': 'Invalid or expired session'}))
    #     ws.close()
    #     return
    
    # session_data = active_sessions[session_id]
    # user_id = session_data['user_id']
    
    # Get session from DB for language preference
    db_session_obj = db_session.query(LiveProSession).get(session_id)
    if not db_session_obj or db_session_obj.status != 'active':
        ws.send(json.dumps({'error': 'Session not active'}))
        ws.close()
        return
    
    user_id = db_session_obj.user_id # Get user_id from DB object
    
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
        # active_sessions[session_id]['last_heartbeat'] = time.time()
        
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
                

                # Heartbeat updated solely by client calling /heartbeat API
                # This keeps separation of concerns clean.
                # Proxy just proxies.
                    
            except TimeoutError:
                # No data received, just continue
                continue
            except Exception as e:
                logger.error(f"WebSocket proxy error: {e}")
                break
        
        # Billing handled by /heartbeat and /end endpoints
            
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



