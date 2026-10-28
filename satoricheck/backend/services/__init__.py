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
    """Initialize all external API service clients at startup."""
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
    """Get global Pitchdeck service instance, sharing the Gemini SDK client."""
    global _pitchdeck_service
    if _pitchdeck_service is None:
        gemini_svc = get_gemini_service()
        _pitchdeck_service = PitchdeckService.__new__(PitchdeckService)
        _pitchdeck_service.api_key = gemini_svc.api_key
        _pitchdeck_service.client = gemini_svc.client
        logger.info("✓ Pitchdeck service initialized (shared Gemini client)")
    return _pitchdeck_service
