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

from backend.config import Config
from backend.database import db_session
from backend.models import LiveProSession

logger = logging.getLogger(__name__)

# Create blueprint for WebSocket routes
ws_bp = Blueprint('ws', __name__)
sock = Sock()

def init_websocket_proxy(app) -> None:
    """Initialize WebSocket support on Flask app."""
    sock.init_app(app)
    logger.info("✓ WebSocket proxy initialized (SDK Mode)")

@sock.route('/api/livepro/ws/<int:session_id>')
def ws_proxy(ws, session_id: int) -> None:
    """
    WebSocket proxy for Live Pro transcription using Deepgram SDK.
    """
    logger.info(f"WebSocket proxy connection for session {session_id}")

    # 1. Validate Session from DB
    try:
        # Use a fresh session check
        db_session_obj = db_session.query(LiveProSession).get(session_id)
        if not db_session_obj or db_session_obj.status != 'active':
            ws.send(json.dumps({'error': 'Session not active'}))
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
        # Configuration for the Deepgram Client
        # SDK v3+ often accepts simple config or defaults.
        # We'll rely on the client to handle keepalives default or pass via config dict if needed
        # but for now, simple init to fix the ImportError
        
        deepgram = DeepgramClient(Config.DEEPGRAM_API_KEY)

        # Create a websocket connection to Deepgram
        options = LiveOptions(
            model="nova-2", 
            language=language or "en", 
            smart_format=True, 
            interim_results=True, 
            utterance_end_ms="1000", 
            vad_events=True,
            endpointing=True, # Enable endpointing
            keepalive=True # Pass keepalive here in LiveOptions if supported, or rely on client default
        )
        
        dg_connection = deepgram.listen.websocket.v("1")

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

        def on_speech_started(self, speech_started, **kwargs):
            pass

        def on_speech_ended(self, speech_ended, **kwargs):
            pass

        def on_close(self, close, **kwargs):
            logger.info(f"Deepgram connection CLOSED for session {session_id}. Code: {close.code}, Reason: {close.reason}")

        def on_error(self, error, **kwargs):
            logger.error(f"Deepgram connection ERROR for session {session_id}: {error}")

        # Register handlers
        dg_connection.on(LiveTranscriptionEvents.Open, on_open)
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_connection.on(LiveTranscriptionEvents.Metadata, on_metadata)
        dg_connection.on(LiveTranscriptionEvents.SpeechStarted, on_speech_started)
        dg_connection.on(LiveTranscriptionEvents.SpeechEnded, on_speech_ended)
        dg_connection.on(LiveTranscriptionEvents.Close, on_close)
        dg_connection.on(LiveTranscriptionEvents.Error, on_error)

        # 4. Connect to Deepgram
        options = LiveOptions(
            model="nova-2",
            language=language or "en",
            smart_format=True,
            encoding="linear16", # We will force raw bytes if possible, or let browser send opus
            channels=1,
            sample_rate=16000,
            interim_results=True,
            utterance_end_ms="1000",
            vad_events=True,
        )

        # NOTE: The browser sends OPUS/WebM usually. Deepgram supports this auto-detect mostly,
        # but specifying "linear16" might break if we send WebM.
        # Let's REMOVE encoding/sample_rate to let Deepgram auto-detect container format from the stream.
        options = LiveOptions(
            model="nova-2",
            language=language or "en",
            smart_format=True,
            interim_results=True,
            utterance_end_ms="1000",
            vad_events=True,
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
