from typing import Optional
from backend.services.gemini_service import GeminiService
from backend.services.grok_service import GrokService
from backend.services.pitchdeck_service import PitchdeckService

import logging

logger = logging.getLogger(__name__)

# Global singleton instances
_gemini_service: Optional[GeminiService] = None
_grok_service: Optional[GrokService] = None
_pitchdeck_service: Optional[PitchdeckService] = None


def init_services():
    """
    Initialize all external API service clients.
    Though services now lazy-init, this can be called at startup 
    to warm up connections and validate API keys.
    """
    get_gemini_service()
    get_grok_service()
    get_pitchdeck_service()


def get_gemini_service() -> GeminiService:
    """Get global Gemini service instance."""
    global _gemini_service
    if _gemini_service is None:
        _gemini_service = GeminiService()
        logger.info("✓ Gemini service initialized")
    return _gemini_service


def get_grok_service() -> GrokService:
    """Get global Grok service instance."""
    global _grok_service
    if _grok_service is None:
        _grok_service = GrokService()
        logger.info("✓ Grok service initialized")
    return _grok_service


def get_pitchdeck_service() -> PitchdeckService:
    """Get global Pitchdeck service instance."""
    global _pitchdeck_service
    if _pitchdeck_service is None:
        _pitchdeck_service = PitchdeckService()
        logger.info("✓ Pitchdeck service initialized")
    return _pitchdeck_service
