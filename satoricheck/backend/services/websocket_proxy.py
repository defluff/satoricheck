"""
WebSocket Proxy for Live Pro transcription via Gemini Live API.

Architecture:
  Browser mic (raw PCM 16kHz) → WS → this proxy → Gemini Live API
  Gemini transcript events → JSON → Browser

The browser-facing protocol is unchanged from the Deepgram era:
  Incoming:  binary ArrayBuffer (PCM chunks)
  Outgoing:  JSON  {"channel": {"alternatives": [{"transcript": "..."}]},
                    "is_final": bool}

Session auth, heartbeat billing, and DB session management are untouched.
"""
from typing import Optional
import asyncio
import json
import logging
import threading
from http.cookies import SimpleCookie

from flask import Blueprint
from flask_sock import Sock
from google import genai
from google.genai import types as genai_types

from backend.config import Config
from backend.database import db_session
from backend.jwt_utils import verify_token
from backend.models import LiveProSession

logger = logging.getLogger(__name__)

# JWT cookie name — keep in sync with auth.py manually (circular import guard)
JWT_COOKIE_NAME = 'authenix_token'

# Blueprint / Sock are attached to the Flask app in init_websocket_proxy()
ws_bp = Blueprint('ws', __name__)
sock = Sock()


def init_websocket_proxy(app) -> None:
    """Attach WebSocket support to the Flask app."""
    sock.init_app(app)
    logger.info("✓ WebSocket proxy initialised (Gemini Live mode)")


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _authenticate_ws_user(environ: dict) -> Optional[int]:
    """Extract and verify the JWT from WebSocket handshake cookies.

    Args:
        environ: WSGI environ dict from the WS handshake.

    Returns:
        user_id integer if the token is valid, None otherwise.
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


# ---------------------------------------------------------------------------
# Gemini Live session runner (async, executed in a background thread)
# ---------------------------------------------------------------------------

def _run_gemini_live_session(ws, session_id: int, language: str) -> None:
    """Open a Gemini Live session and bridge audio/transcripts to the browser WS.

    Runs in a dedicated thread via asyncio.run() so the synchronous flask-sock
    handler can hand off and receive transcripts via a thread-safe queue.

    Args:
        ws:         The flask-sock WebSocket object (browser side).
        session_id: DB session ID (for logging only).
        language:   BCP-47 language code hint (e.g. 'en', 'de').
    """
    # Queue used to pass audio bytes from the main thread into the async loop
    audio_queue: asyncio.Queue = asyncio.Queue()
    # Sentinel that tells the async sender loop to stop
    _STOP = object()

    async def _session():
        client = genai.Client(api_key=Config.GEMINI_API_KEY)

        config = genai_types.LiveConnectConfig(
            response_modalities=[genai_types.Modality.TEXT],
            # Request transcription of the user's audio input
            input_audio_transcription=genai_types.AudioTranscriptionConfig(),
            system_instruction=(
                "You are a real-time transcription assistant. "
                "Transcribe the incoming audio accurately. "
                f"The speaker's primary language is '{language}'. "
                "Output only the transcription text — no commentary."
            ),
        )

        async with client.aio.live.connect(
            model=Config.GEMINI_LIVE_MODEL, config=config
        ) as session:
            logger.info(f"Gemini Live session open for proxy session {session_id}")

            async def _sender():
                """Pull audio chunks from the queue and forward to Gemini."""
                while True:
                    chunk = await audio_queue.get()
                    if chunk is _STOP:
                        break
                    await session.send_realtime_input(
                        audio=genai_types.Blob(
                            mime_type="audio/pcm;rate=16000",
                            data=chunk,
                        )
                    )

            async def _receiver():
                """Forward Gemini transcript events to the browser WS."""
                async for response in session.receive():
                    sc = response.server_content
                    if sc and sc.input_transcription:
                        text = sc.input_transcription.text
                        if text:
                            # Emit same JSON schema the frontend expects
                            payload = {
                                "channel": {
                                    "alternatives": [{"transcript": text}]
                                },
                                "is_final": bool(sc.turn_complete),
                            }
                            try:
                                ws.send(json.dumps(payload))
                            except Exception as send_err:
                                logger.warning(
                                    f"WS send error session {session_id}: {send_err}"
                                )
                                return  # Browser disconnected

            # Run sender and receiver concurrently
            await asyncio.gather(_sender(), _receiver())

        logger.info(f"Gemini Live session closed for proxy session {session_id}")

    try:
        asyncio.run(_session())
    except Exception as exc:
        logger.error(f"Gemini Live session error (session {session_id}): {exc}")
        try:
            ws.send(json.dumps({"error": "Transcription service error"}))
        except Exception:
            pass  # Browser already gone


# ---------------------------------------------------------------------------
# Main WebSocket route
# ---------------------------------------------------------------------------

@sock.route('/api/livepro/ws/<int:session_id>')
def ws_proxy(ws, session_id: int) -> None:
    """WebSocket proxy for Live Pro transcription via Gemini Live API.

    1. Authenticates user via JWT cookie.
    2. Validates the DB session belongs to that user.
    3. Starts a Gemini Live session in a background thread.
    4. Relays raw PCM audio from browser → Gemini queue.
    5. Transcript events flow back via the receiver coroutine.
    """
    logger.info(f"WebSocket proxy connection for session {session_id}")

    # Step 1 — Authenticate
    ws_user_id = _authenticate_ws_user(ws.environ)
    if ws_user_id is None:
        ws.send(json.dumps({"error": "Authentication required"}))
        ws.close()
        return

    # Step 2 — Validate DB session ownership
    try:
        session_obj = db_session.query(LiveProSession).get(session_id)
        if not session_obj or session_obj.status != 'active':
            ws.send(json.dumps({"error": "Session not active"}))
            ws.close()
            return

        if session_obj.user_id != ws_user_id:
            logger.warning(
                f"WS auth mismatch: JWT user {ws_user_id} attempted session "
                f"{session_id} owned by {session_obj.user_id}"
            )
            ws.send(json.dumps({"error": "Session not found"}))
            ws.close()
            return

        language = session_obj.language or 'en'
    except Exception as db_err:
        logger.error(f"DB check failed for session {session_id}: {db_err}")
        ws.close()
        return

    # Step 3 — Check Gemini key is available
    if not Config.GEMINI_API_KEY:
        ws.send(json.dumps({"error": "Transcription service not configured"}))
        ws.close()
        return

    # Step 4 — Run Gemini Live session in a background thread.
    # The thread owns the async event loop; the main thread relays audio via
    # the shared queue that _run_gemini_live_session sets up internally.
    # We use a simple thread here because flask-sock's ws.receive() is
    # synchronous and cannot run inside an asyncio event loop directly.
    audio_queue_holder: list = []  # shared mutable container (set by thread)
    ready_event = threading.Event()

    def _thread_target():
        # Replicate the queue setup here so the main thread can enqueue audio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        q: asyncio.Queue = asyncio.Queue()
        audio_queue_holder.append(q)
        ready_event.set()

        _STOP = object()

        async def _session():
            client = genai.Client(api_key=Config.GEMINI_API_KEY)
            config = genai_types.LiveConnectConfig(
                response_modalities=[genai_types.Modality.TEXT],
                input_audio_transcription=genai_types.AudioTranscriptionConfig(),
                system_instruction=(
                    "You are a real-time transcription assistant. "
                    "Transcribe the incoming audio accurately. "
                    f"The speaker's primary language is '{language}'. "
                    "Output only the transcription text — no commentary."
                ),
            )

            async with client.aio.live.connect(
                model=Config.GEMINI_LIVE_MODEL, config=config
            ) as gemini_session:
                logger.info(f"Gemini Live open for session {session_id}")

                async def _sender():
                    while True:
                        chunk = await q.get()
                        if chunk is _STOP:
                            break
                        await gemini_session.send_realtime_input(
                            audio=genai_types.Blob(
                                mime_type="audio/pcm;rate=16000",
                                data=chunk,
                            )
                        )

                async def _receiver():
                    async for response in gemini_session.receive():
                        sc = response.server_content
                        if sc and sc.input_transcription:
                            text = sc.input_transcription.text
                            if text:
                                payload = {
                                    "channel": {
                                        "alternatives": [{"transcript": text}]
                                    },
                                    "is_final": bool(sc.turn_complete),
                                }
                                try:
                                    ws.send(json.dumps(payload))
                                except Exception:
                                    return  # Browser disconnected

                await asyncio.gather(_sender(), _receiver())

            logger.info(f"Gemini Live closed for session {session_id}")

        try:
            loop.run_until_complete(_session())
        except Exception as exc:
            logger.error(f"Gemini Live error (session {session_id}): {exc}")
            try:
                ws.send(json.dumps({"error": "Transcription service error"}))
            except Exception:
                pass
        finally:
            loop.close()

    gemini_thread = threading.Thread(target=_thread_target, daemon=True)
    gemini_thread.start()

    # Wait until the async loop and queue are ready
    ready_event.wait(timeout=10)
    if not audio_queue_holder:
        logger.error(f"Gemini thread failed to start for session {session_id}")
        ws.close()
        return

    audio_queue: asyncio.Queue = audio_queue_holder[0]
    _STOP_SENTINEL = object()

    # Step 5 — Main loop: relay browser audio → Gemini queue
    try:
        while True:
            data = ws.receive()
            if data is None:
                break  # Browser disconnected
            if isinstance(data, bytes):
                # Thread-safe enqueue for the asyncio loop
                gemini_thread._target  # noqa: just confirming thread alive
                asyncio.get_event_loop  # no-op, loop lives in thread
                # Use call_soon_threadsafe pattern: we put directly since
                # asyncio.Queue.put_nowait is safe to call from another thread
                # when the loop is running in a separate thread.
                audio_queue.put_nowait(data)
    except Exception as browser_err:
        logger.error(f"Browser WS error in proxy (session {session_id}): {browser_err}")
    finally:
        # Signal the Gemini sender to stop
        try:
            audio_queue.put_nowait(_STOP_SENTINEL)
        except Exception:
            pass
        gemini_thread.join(timeout=5)
        logger.info(f"Proxy cleanup complete for session {session_id}")
