"""
Transcription Service for Live Pro.
Wraps Gemini Live API for real-time speech-to-text, replacing the previous
Deepgram dependency. Public interface is identical to the old DeepgramService
to minimise call-site changes.
"""
import logging
from typing import Optional

from backend.config import Config

logger = logging.getLogger(__name__)


class GeminiTranscriptionService:
    """Service for real-time audio transcription via Gemini Live API.

    Drop-in replacement for the removed DeepgramService.
    Availability is gated on GEMINI_API_KEY — no additional key required.
    """

    def is_available(self) -> bool:
        """Return True when the Gemini API key is configured.

        Live Pro availability now shares the same key as the rest of the
        Gemini fact-checking pipeline, eliminating the separate Deepgram
        credential.
        """
        return bool(Config.GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors old deepgram_service pattern)
# ---------------------------------------------------------------------------

_transcription_service: Optional[GeminiTranscriptionService] = None


def init_transcription_service() -> GeminiTranscriptionService:
    """Initialise and return the global transcription service instance.

    Called once at server startup from server.py.
    """
    global _transcription_service
    _transcription_service = GeminiTranscriptionService()
    if _transcription_service.is_available():
        logger.info("✓ Gemini transcription service initialised (Live Pro available)")
    else:
        logger.warning("! GEMINI_API_KEY not set — Live Pro unavailable")
    return _transcription_service


def get_transcription_service() -> GeminiTranscriptionService:
    """Return the global transcription service, initialising lazily if needed."""
    global _transcription_service
    if _transcription_service is None:
        _transcription_service = init_transcription_service()
    return _transcription_service
