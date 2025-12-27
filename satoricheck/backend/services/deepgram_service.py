"""
Deepgram Service for Live Pro transcription.
Handles WebSocket streaming and real-time speech-to-text.
"""
import asyncio
import json
import logging
from typing import Callable, Optional

from backend.config import Config

logger = logging.getLogger(__name__)

# Deepgram WebSocket URL
DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramService:
    """Service for Deepgram real-time transcription."""
    
    def __init__(self):
        self.api_key = Config.DEEPGRAM_API_KEY
        if not self.api_key:
            logger.warning("DEEPGRAM_API_KEY not configured - Live Pro will be unavailable")
    
    def is_available(self) -> bool:
        """Check if Deepgram is configured and available."""
        return bool(self.api_key)
    
    def get_websocket_url(self, language: str = "en") -> str:
        """
        Get the Deepgram WebSocket URL with parameters.
        
        Args:
            language: Language code (e.g., 'en', 'de', 'fr')
            
        Returns:
            Full WebSocket URL with query parameters
        """
        params = {
            "model": "nova-2",  # Latest model
            "language": language,
            "punctuate": "true",
            "interim_results": "true",  # Get partial results
            "utterance_end_ms": "1000",
            "vad_events": "true"  # Voice activity detection
        }
        
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{DEEPGRAM_WS_URL}?{query_string}"
    
    def get_auth_header(self) -> dict:
        """Get authorization header for WebSocket connection."""
        return {"Authorization": f"Token {self.api_key}"}


# Global service instance
_deepgram_service: Optional[DeepgramService] = None


def init_deepgram_service():
    """Initialize the global Deepgram service."""
    global _deepgram_service
    _deepgram_service = DeepgramService()
    if _deepgram_service.is_available():
        logger.info("✓ Deepgram service initialized (Live Pro available)")
    else:
        logger.warning("! Deepgram service not configured (Live Pro unavailable)")
    return _deepgram_service


def get_deepgram_service() -> DeepgramService:
    """Get the global Deepgram service instance."""
    global _deepgram_service
    if _deepgram_service is None:
        _deepgram_service = init_deepgram_service()
    return _deepgram_service
