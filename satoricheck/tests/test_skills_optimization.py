"""
Tests for Skill Optimizations, Register Calibration, Audio/Video Forensics, and Batch Verification.
"""
import pytest
from unittest.mock import patch, MagicMock
from backend.services.gemini.utils import GeminiServiceUtils
from backend.services.gemini.media import GeminiServiceMedia
from backend.services.pitchdeck_service import PitchdeckService


class TestSkillOptimizations:
    """Tests for register calibration, short-text flags, and media forensics."""

    def test_analyze_ai_short_text_flag_and_register(self, app):
        """Verify analyze_ai_content tags short text and parses detected_register."""
        utils = GeminiServiceUtils()
        short_text = "This is a brief human sentence."
        
        mock_response = MagicMock()
        mock_response.text = '''{
            "ai_probability": 15,
            "confidence": "LOW",
            "detected_register": "social",
            "is_short_text": true,
            "ai_indicators": [],
            "human_indicators": ["conversational rhythm"],
            "explanation": "Short authentic human copy."
        }'''
        
        with patch.object(utils, 'client') as mock_client:
            mock_client.models.generate_content.return_value = mock_response
            result = utils.analyze_ai_content(short_text)
            
            assert result['is_short_text'] is True
            assert result['detected_register'] == 'social'
            assert result['ai_probability'] == 15

    def test_analyze_media_prompt_enrichment(self, app):
        """Verify analyze_media_authenticity passes video/audio context and layers."""
        media_svc = GeminiServiceMedia()
        
        mock_response = MagicMock()
        mock_response.text = '''{
            "verdict": "Appears Authentic",
            "confidence": 90,
            "explanation": "No anomalies found.",
            "criteria": {
                "physics": {"tag": "Clean", "score": 0, "detail": "Consistent lighting."},
                "bio": {"tag": "Clean", "score": 0, "detail": "Natural facial features."},
                "temporal": {"tag": "Clean", "score": 0, "detail": "Stable frames."},
                "audio": {"tag": "Clean", "score": 0, "detail": "Natural speech cadence."},
                "context": {"tag": "Clean", "score": 0, "detail": "Plausible scene."},
                "compression": {"tag": "Clean", "score": 10, "detail": "Standard MP4."},
                "metadata": {"tag": "Clean", "score": 0, "detail": "Clean."}
            }
        }'''
        
        with patch.object(media_svc, 'client') as mock_client, \
             patch.object(media_svc, '_prepare_media_part') as mock_prep, \
             patch.object(media_svc, 'create_cache', return_value=None):
            
            mock_prep.return_value = MagicMock()
            mock_client.models.generate_content.return_value = mock_response
            
            res = media_svc.analyze_media_authenticity(
                b"fake_video_bytes", input_type="bytes", mime_type="video/mp4"
            )
            
            assert res['verdict'] == "Appears Authentic"
            assert "audio" in res['criteria']
            assert "temporal" in res['criteria']
            mock_client.models.generate_content.assert_called_once()
            call_args = mock_client.models.generate_content.call_args
            contents = call_args[1].get('contents') or call_args[0][0]
            assert any("video with audio track" in str(c) for c in contents)

    def test_pitchdeck_verify_market_claims_uses_single_batch_call(self, app):
        """Verify pitch deck claim verification uses analyze_claims_batch in 1 call instead of N calls."""
        service = PitchdeckService()
        claims = [
            {"claim": "Market is $50B in 2026", "category": "market_size", "source_cited": "Statista", "slide_number": 2},
            {"claim": "ARR grew 300% YoY to €5M", "category": "revenue", "source_cited": "Company data", "slide_number": 5},
        ]
        
        mock_batch_results = [
            {"is_claim": True, "verdict": "TRUE", "explanation": "Statista confirms market.", "sources": ["https://statista.com"]},
            {"is_claim": True, "verdict": "UNVERIFIED", "explanation": "Internal metrics.", "sources": []}
        ]
        
        with patch('backend.services.get_gemini_service') as mock_get_svc:
            mock_gemini = MagicMock()
            mock_gemini.analyze_claims_batch.return_value = mock_batch_results
            mock_get_svc.return_value = mock_gemini
            
            findings = service.verify_market_claims(
                verifiable_claims=claims,
                industry="FinTech",
                cache_name="test_cache_123"
            )
            
            assert len(findings) == 2
            assert findings[0]["verdict"] == "TRUE"
            assert findings[0]["slide_number"] == 2
            assert findings[1]["verdict"] == "UNVERIFIED"
            assert findings[1]["slide_number"] == 5
            # Crucial: analyze_claims_batch was called ONCE, not twice sequentially
            mock_gemini.analyze_claims_batch.assert_called_once()
