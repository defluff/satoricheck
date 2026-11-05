"""
Tests for Skill Optimizations, Register Calibration, Audio/Video Forensics, and Batch Verification.
"""
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

    def test_claim_extraction_skill_stream_calibration(self, app):
        """Verify claim_extraction.md skill contains falsifiability standard and stream filters."""
        utils = GeminiServiceUtils()
        skill_text = utils._load_skill("claim_extraction")
        
        assert "Falsifiability Standard" in skill_text
        assert "Rhetorical Hyperbole" in skill_text
        assert "Conversational Filler & Disfluencies" in skill_text

    def test_stream_triage_skill_loaded(self, app):
        """Verify dedicated skill file for stream_triage loads properly."""
        from backend.services.gemini.batch import GeminiServiceBatch
        batch_svc = GeminiServiceBatch()

        triage_skill = batch_svc._load_skill("stream_triage")

        assert "Stream Claim Triage" in triage_skill
        assert "is_hyperbole" in triage_skill

    def test_batch_parser_standardizes_verdict_and_source_reliability(self, app):
        """Verify _parse_thinking_batch_response normalizes UNVERIFIABLE and extracts source_reliability."""
        from backend.services.gemini.batch import GeminiServiceBatch
        batch_svc = GeminiServiceBatch()

        mock_resp = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '''{
                            "results": [
                                {
                                    "claim_index": 1,
                                    "verdict": "UNVERIFIABLE",
                                    "explanation": "No public record found.",
                                    "source_reliability": "LOW",
                                    "sources": []
                                },
                                {
                                    "claim_index": 2,
                                    "verdict": "PARTIALLY TRUE",
                                    "explanation": "Revenue grew, but by 150%, not 300%.",
                                    "source_reliability": "HIGH",
                                    "sources": ["https://sec.gov/filing"]
                                }
                            ]
                        }'''
                    }]
                }
            }]
        }

        with patch.object(batch_svc, '_validate_url', return_value=True):
            parsed = batch_svc._parse_thinking_batch_response(mock_resp, expected_count=2)

        assert len(parsed) == 2
        assert parsed[0]["verdict"] == "COULD_NOT_VERIFY"
        assert parsed[0]["source_reliability"] == "LOW"
        assert parsed[1]["verdict"] == "PARTIALLY TRUE"
        assert parsed[1]["source_reliability"] == "HIGH"

    def test_single_pass_json_calls_enforce_response_mime_type(self, app):
        """Verify single-pass JSON calls set response_mime_type='application/json'."""
        from backend.services.gemini.batch import GeminiServiceBatch
        utils = GeminiServiceUtils()
        batch_svc = GeminiServiceBatch()
        media_svc = GeminiServiceMedia()

        mock_resp = MagicMock()
        mock_resp.text = '{"claims": ["The Earth orbits the Sun every 365.25 days."]}'
        with patch.object(utils, 'client') as mock_client:
            mock_client.models.generate_content.return_value = mock_resp
            utils.identify_claims("The Earth orbits the Sun every 365.25 days.")
            cfg = mock_client.models.generate_content.call_args.kwargs['config']
            assert cfg.response_mime_type == "application/json"

        mock_triage_resp = MagicMock()
        mock_triage_resp.text = '[{"index": 1, "priority": "NORMAL", "strategy": "SEARCH_VERIFY", "is_hyperbole": false}]'
        with patch.object(batch_svc, 'client') as mock_client:
            mock_client.models.generate_content.return_value = mock_triage_resp
            batch_svc.triage_for_stream(["Claim 1"])
            cfg = mock_client.models.generate_content.call_args.kwargs['config']
            assert cfg.response_mime_type == "application/json"

    def test_ai_detection_and_triage_skills_include_grok_and_claude_watermark_calibration(self, app):
        """Verify ai_detection.md and stream_triage.md integrate Grok + Claude watermark provenance and triage rules."""
        from backend.services.gemini.batch import GeminiServiceBatch
        from backend.services.gemini.client import _read_skill_file
        _read_skill_file.cache_clear()

        batch_svc = GeminiServiceBatch()
        ai_skill = batch_svc._load_skill("ai_detection")
        triage_skill = batch_svc._load_skill("stream_triage")

        # AI detection skill covers SynthID-Text, Claude, and xAI Grok watermarks + Negative Evidence Rule
        assert "SynthID-Text" in ai_skill
        assert "Claude" in ai_skill
        assert "Grok" in ai_skill
        assert "x-watermark" in ai_skill
        assert "Negative Evidence Rule" in ai_skill

        # Stream triage skill guards against LLM citation hallucinations and drops synthetic boilerplate
        assert "Claude and Grok" in triage_skill
        assert "Never use `KNOWLEDGE_CHECK` for specific statistics, named studies/DOIs" in triage_skill
        assert "boilerplate" in triage_skill

    def test_single_and_batch_claims_share_master_verification_loop(self, app):
        """Verify both single-claim and batch verification delegate to _run_verification_loop."""
        from backend.services.gemini_service import GeminiService
        svc = GeminiService()

        with patch.object(svc, '_run_verification_loop') as mock_master_loop, \
             patch('backend.services.grok_service.get_grok_service') as mock_get_grok:
            mock_get_grok.return_value = MagicMock()

            mock_master_loop.return_value = {"is_claim": True, "verdict": "TRUE", "explanation": "OK", "sources": []}
            single_res = svc.analyze_claim("The Earth orbits the Sun.", smart_agent=True)
            assert single_res["verdict"] == "TRUE"
            assert mock_master_loop.call_count == 1
            assert mock_master_loop.call_args.kwargs["context_label_prefix"] == "agentic"

            mock_master_loop.reset_mock()
            mock_master_loop.return_value = [{"is_claim": True, "verdict": "FALSE", "explanation": "No", "sources": []}]
            batch_res = svc.analyze_claims_batch(["The Moon is made of cheese."], pre_triaged=True)
            assert len(batch_res) == 1
            assert batch_res[0]["verdict"] == "FALSE"
            assert mock_master_loop.call_count == 1
            assert mock_master_loop.call_args.kwargs["context_label_prefix"] == "batch"


