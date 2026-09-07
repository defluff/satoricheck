"""
Pitchdeck Analysis Service.

Provides investor-grade intelligence extraction from pitch deck PDFs.
Maintains full backward compatibility by subclassing GeminiServicePitchdeck.
"""
import logging
from backend.config import Config
from backend.services.gemini.pitchdeck import GeminiServicePitchdeck

logger = logging.getLogger(__name__)


class PitchdeckService(GeminiServicePitchdeck):
    """Service for analyzing pitch deck PDFs with Gemini Vision."""
    pass


# Singleton instance
pitchdeck_service = PitchdeckService()
