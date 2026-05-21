from typing import Union, Optional, List, Dict, Any
"""
WebSocket Proxy for Deepgram Live Pro.
Routes audio from browser → backend → Deepgram using the official SDK.
"""
import logging
import threading
import json
import time
from flask import Blueprint
from flask_sock import Sock

from deepgram import DeepgramClient, DeepgramClientOptions, LiveOptions, LiveTranscriptionEvents

from datetime import datetime
import os
import ssl
import certifi
from http.cookies import SimpleCookie
from backend.config import Config
from backend.database import db_session
from backend.jwt_utils import verify_token
from backend.models import LiveProSession

logger = logging.getLogger(__name__)

# JWT cookie name — must match auth.py
JWT_COOKIE_NAME = 'satori_token'


# Create blueprint for WebSocket routes
ws_bp = Blueprint('ws', __name__)
sock = Sock()

def init_websocket_proxy(app) -> None:
    """Initialize WebSocket support on Flask app."""
    sock.init_app(app)
    logger.info("✓ WebSocket proxy initialized (SDK Mode)")


def _authenticate_ws_user(environ: dict) -> Optional[int]:
    """Extract and verify JWT from WebSocket handshake cookies.

    Returns the user_id if valid, None otherwise.
    """
    raw_cookie = environ.get('HTTP_COOKIE', '')
    if not raw_cookie:
        return None

    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except Exception:
        return None

    morsel = cookie.get(JWT_COOKIE_NAME)
    if not morsel:
        return None

    payload = verify_token(morsel.value)
    if not payload:
        return None

    return payload.get('user_id')


@sock.route('/api/livepro/ws/<int:session_id>')
def ws_proxy(ws, session_id: int) -> None:
    """
    WebSocket proxy for Live Pro transcription using Deepgram SDK.
    """
    logger.info(f"WebSocket proxy connection for session {session_id}")

    # 0. Authenticate the WebSocket user via JWT cookie
    ws_user_id = _authenticate_ws_user(ws.environ)
    if ws_user_id is None:
        ws.send(json.dumps({'error': 'Authentication required'}))
        ws.close()
        return

    # 1. Validate Session from DB
    try:
        # Use a fresh session check
        db_session_obj = db_session.query(LiveProSession).get(session_id)
        if not db_session_obj or db_session_obj.status != 'active':
            ws.send(json.dumps({'error': 'Session not active'}))
            ws.close()
            return

        # Verify the authenticated user owns this session
        if db_session_obj.user_id != ws_user_id:
            logger.warning(
                f"WebSocket auth mismatch: JWT user {ws_user_id} tried to access session {session_id} owned by {db_session_obj.user_id}"
            )
            ws.send(json.dumps({'error': 'Session not found'}))
            ws.close()
            return

        user_id = db_session_obj.user_id
        language = db_session_obj.language or 'en'
    except Exception as e:
        logger.error(f"Database check failed for session {session_id}: {e}")
        ws.close()
        return

    # 2. Setup Deepgram Client
    try:
        # Robust SSL Fix: Set environment variable for the entire process/thread
        # This ensures underlying C-extensions (like those used by aiohttp/websockets) use the correct CA
        os.environ['SSL_CERT_FILE'] = certifi.where()
        
        # Configure Deepgram options
        config = DeepgramClientOptions(options={"keepalive": "true"})
        
        deepgram = DeepgramClient(Config.DEEPGRAM_API_KEY, config)
        dg_connection = deepgram.listen.live.v("1")

        # 3. Define Event Handlers
        def on_open(self, open, **kwargs):
            logger.info(f"Deepgram connection OPEN for session {session_id}")

        def on_message(self, result, **kwargs):
            try:
                # The SDK returns an object, we need to extract the raw JSON or construct it
                # result is a LiveResultResponse
                # We need to send back the generic JSON structure the frontend expects
                
                # Verify if we have a transcript
                transcript = result.channel.alternatives[0].transcript
                if transcript:
                    # Construct minimal response payload for frontend
                    payload = {
                        "channel": {
                            "alternatives": [
                                {
                                    "transcript": transcript
                                }
                            ]
                        },
                        "is_final": result.is_final
                    }
                    ws.send(json.dumps(payload))
            except Exception as e:
                logger.error(f"Error relaying transcript: {e}")

        def on_metadata(self, metadata, **kwargs):
            pass # We don't need to forward metadata for now



        def on_close(self, close, **kwargs):
            logger.info(f"Deepgram connection CLOSED for session {session_id}. Code: {close.code}, Reason: {close.reason}")

        def on_error(self, error, **kwargs):
            logger.error(f"Deepgram connection ERROR for session {session_id}: {error}")
            # We can't easily close the socket from here, but logging helps debug

        # Register handlers
        dg_connection.on(LiveTranscriptionEvents.Open, on_open)
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.on(LiveTranscriptionEvents.Metadata, on_metadata)

        dg_connection.on(LiveTranscriptionEvents.Close, on_close)
        dg_connection.on(LiveTranscriptionEvents.Error, on_error)

        # 4. Connect to Deepgram
        # Single, clean configuration relying on Deepgram auto-detection for audio format
        options = LiveOptions(
            model="nova-2",
            language=language or "en",
            smart_format=True,
            interim_results=True,
            utterance_end_ms="1000",
            vad_events=True,
            endpointing=True
        )

        if dg_connection.start(options) is False:
            logger.error("Failed to start Deepgram connection")
            ws.close()
            return
            
        logger.info(f"Deepgram Live connection started for session {session_id}")

        # 5. Main Loop: Relay Audio from Browser -> Deepgram
        try:
            while True:
                data = ws.receive()
                if data is None:
                    break # Connection closed by browser
                
                # print(f"DEBUG: Received {len(data)} bytes from browser", flush=True)
                if isinstance(data, bytes):
                    dg_connection.send(data)
                else:
                    # Handle control messages if any
                    pass
                    
        except Exception as e:
            logger.error(f"Browser WebSocket error in proxy: {e}")
        finally:
            # Cleanup
            dg_connection.finish()
            logger.info(f"Proxy cleanup for session {session_id}")

    except Exception as e:
        logger.error(f"Deepgram Setup Error: {e}")
        ws.send(json.dumps({'error': 'Transcription service error'}))
        ws.close()
