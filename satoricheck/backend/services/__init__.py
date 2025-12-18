"""
Global service instances for external APIs.
Initialized once at app startup for optimal Cloud Run performance.
"""
from backend.services.gemini_service import GeminiService
import logging

logger = logging.getLogger(__name__)

# Global singleton instances
_gemini_service = None


def init_services():
    """Initialize all external API service clients."""
    global _gemini_service
    
    if _gemini_service is None:
        _gemini_service = GeminiService()
        logger.info("✓ Gemini service initialized")


def get_gemini_service():
    """Get global Gemini service instance."""
    if _gemini_service is None:
        raise RuntimeError("Services not initialized. Call init_services() first.")
    return _gemini_service
