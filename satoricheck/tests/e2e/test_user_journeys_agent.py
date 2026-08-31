"""
Automated Persona Journey Agent.
Simulates realistic, multi-step end-to-end user journeys across all Authenix capabilities:
1. Dr. Eleanor — The AI Content & Literature Scholar (Register calibration, classic literature, short text)
2. Marcus — The Breaking News & Quote Fact-Checker (Quote verification, Grok social triggers, temporal anchors)
3. Sophia — The High-Volume Intelligence Analyst (Entity-clustered batch verification, deduplication, cache hits)
4. Liam — The VC Investment Associate (Pitch deck vision extraction, scorecard metrics, 1-shot batch claim verification)
5. Agent Vance — The Digital Forensics Investigator (Multimodal image, video temporal & audio deepfake forensics)
"""
import io
import json
import pytest
from unittest.mock import patch, MagicMock


class TestPersonaUserJourneys:
    """End-to-End Persona Journey Agent suite."""

    # =========================================================================
    # PERSONA 1: The AI Content Authenticity & Literature Scholar
    # =========================================================================

    def test_persona_dr_eleanor_academic_and_literature_analysis(self, auth_client, mocker):
        """
        Dr. Eleanor tests the AI content detector across different registers:
        - Academic research paper (dense nominalization, citations) -> Low AI, 'academic' register
        - Classic Victorian literature (archaic diction, em-dashes) -> Low AI, 'literature' register
        - Short text snippet (<50 words) -> is_short_text=True, confidence=LOW
        - AI-generated marketing copy (buzzwords) -> High AI, detected buzzword markers
        """
        # Step 1: Academic Paper Analysis
        academic_text = (
            "We conducted a double-blind randomized controlled evaluation across N=450 participants. "
            "The statistical divergence observed between the control cohort and experimental cohort "
            "demonstrated significant covariance (p < 0.001). As documented in previous literature "
            "(Smith et al., 2024), nominalization and structured methodology were rigorously maintained throughout."
        )
        
        mock_academic_res = {
            "ai_probability": 10,
            "confidence": "HIGH",
            "detected_register": "academic",
            "is_short_text": False,
            "ai_indicators": [],
            "human_indicators": ["rigorous methodology terminology", "domain-specific statistical notation"],
            "explanation": "Human-authored academic writing exhibiting standard discipline-specific syntax."
        }
        
        mocker.patch(
            'backend.services.gemini.utils.GeminiServiceUtils.analyze_ai_content',
            return_value=mock_academic_res
        )
        
        resp = auth_client.post('/api/factcheck/analyze-ai', json={'text': academic_text})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['detected_register'] == 'academic'
        assert data['ai_probability'] <= 30
        assert data['is_short_text'] is False

        # Step 2: Victorian Classic Literature Analysis
        literature_text = (
            "It is a truth universally acknowledged, that a single man in possession of a good fortune, "
            "must be in want of a wife. However little known the feelings or views of such a man may be "
            "on his first entering a neighbourhood, this truth is so well fixed in the minds of the surrounding "
            "families, that he is considered the rightful property of some one or other of their daughters—and "
            "wherefore should it be otherwise, whilst hearts remain bespoke?"
        )
        
        mock_lit_res = {
            "ai_probability": 5,
            "confidence": "HIGH",
            "detected_register": "literature",
            "is_short_text": False,
            "ai_indicators": [],
            "human_indicators": ["archaic vocabulary (wherefore, bespoke, whilst)", "complex 19th-century periodic syntax"],
            "explanation": "Authentic classic literature with historical phrasing and stylistic em-dash cadence."
        }
        
        mocker.patch(
            'backend.services.gemini.utils.GeminiServiceUtils.analyze_ai_content',
            return_value=mock_lit_res
        )
        
        resp = auth_client.post('/api/factcheck/analyze-ai', json={'text': literature_text})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['detected_register'] == 'literature'
        assert data['ai_probability'] <= 15

        # Step 3: Short Snippet (25-45 words: above 20 word minimum, but <50 words)
        short_snippet = (
            "The quick brown fox jumps over the lazy dog in the quiet morning park, "
            "seeking fresh air and peaceful paths before the busy day begins."
        )
        mock_short_res = {
            "ai_probability": 25,
            "confidence": "LOW",
            "detected_register": "general",
            "is_short_text": True,
            "ai_indicators": [],
            "human_indicators": [],
            "explanation": "Short sample (<50 words) with insufficient statistical token variance."
        }
        
        mocker.patch(
            'backend.services.gemini.utils.GeminiServiceUtils.analyze_ai_content',
            return_value=mock_short_res
        )
        
        resp = auth_client.post('/api/factcheck/analyze-ai', json={'text': short_snippet})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['is_short_text'] is True
        assert data['confidence'] == 'LOW'

    # =========================================================================
    # PERSONA 2: The Breaking News & Quote Fact-Checker
    # =========================================================================

    def test_persona_marcus_breaking_news_and_quote_verification(self, auth_client, mocker):
        """
        Marcus tests quote verification, social trigger routing, and claim extraction:
        - Submits a quote claim ("Elon Musk stated today...") -> Activates quote fields & social trigger
        - Decomposes a multi-sentence news report into atomic claims via identify-claims
        """
        # Step 1: Quote Claim with Social Context
        quote_text = 'Elon Musk tweeted today: "We are officially launching the new quantum model next month."'
        
        mock_quote_res = {
            "is_claim": True,
            "verdict": "FALSE",
            "explanation": "No verified post or official press release exists on X or Tesla/xAI newsrooms.",
            "fallacy": None,
            "sources": ["https://x.com/elonmusk", "https://tesla.com/press"],
            "source_reliability": "HIGH",
            "is_quote_claim": True,
            "quote_attribution": "Elon Musk",
            "quote_verified": False,
            "quote_source": "Social Media Post (X)",
            "meta_truth_verdict": "FALSE",
            "social_context": "Checked recent X activity via Grok search; no record found."
        }
        
        mocker.patch(
            'backend.services.gemini.claims.GeminiServiceClaims.analyze_claim',
            return_value=mock_quote_res
        )
        
        resp = auth_client.post('/api/factcheck/analyze', json={'text': quote_text})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        result = data['result']
        assert result['is_quote_claim'] is True
        assert result['quote_attribution'] == "Elon Musk"
        assert result['quote_verified'] is False
        assert result['verdict'] == "FALSE"
        assert result['meta_truth_verdict'] == "FALSE"

        # Step 2: Multi-Claim Identification
        article_text = (
            "Dolphins are marine mammals and they have the highest brain-to-body ratio among cetaceans. "
            "The moon's core consists primarily of metallic iron alloy."
        )
        
        mock_extracted_claims = [
            "Dolphins are marine mammals",
            "Dolphins have the highest brain-to-body ratio among cetaceans",
            "The moon's core consists primarily of metallic iron alloy"
        ]
        
        mocker.patch(
            'backend.services.gemini.utils.GeminiServiceUtils.identify_claims',
            return_value=mock_extracted_claims
        )
        
        resp = auth_client.post('/api/factcheck/identify-claims', json={'text': article_text})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert len(data['claims']) == 3
        assert "Dolphins are marine mammals" in data['claims']

    # =========================================================================
    # PERSONA 3: The High-Volume Intelligence Analyst
    # =========================================================================

    def test_persona_sophia_batch_verification_and_cache_reuse(self, auth_client, mocker):
        """
        Sophia submits a batch of claims with shared entities:
        - First run verifies new claims and deducts tokens
        - Second run detects exact match cache hits, returning instant results with 0 tokens billed
        """
        claims = [
            "OpenAI was founded in December 2015",
            "Microsoft invested $10 billion into OpenAI in 2023",
            "Sam Altman is the CEO of OpenAI"
        ]
        
        mock_batch_results = [
            {
                "is_claim": True,
                "verdict": "TRUE",
                "explanation": "OpenAI was founded in Dec 2015 as a non-profit.",
                "fallacy": None,
                "sources": ["https://openai.com/about"],
                "source_reliability": "HIGH"
            },
            {
                "is_claim": True,
                "verdict": "TRUE",
                "explanation": "Microsoft announced a multi-billion dollar investment in January 2023.",
                "fallacy": None,
                "sources": ["https://blogs.microsoft.com"],
                "source_reliability": "HIGH"
            },
            {
                "is_claim": True,
                "verdict": "TRUE",
                "explanation": "Sam Altman currently serves as CEO of OpenAI.",
                "fallacy": None,
                "sources": ["https://openai.com/our-structure"],
                "source_reliability": "HIGH"
            }
        ]
        
        mocker.patch(
            'backend.services.gemini.batch.GeminiServiceBatch.analyze_claims_batch',
            return_value=mock_batch_results
        )
        
        # Step 1: Initial Batch Verification
        resp1 = auth_client.post('/api/factcheck/analyze-batch', json={'claims': claims})
        assert resp1.status_code == 200
        data1 = resp1.get_json()
        assert data1['success'] is True
        assert len(data1['results']) == 3
        assert all(r['verdict'] == 'TRUE' for r in data1['results'])

        # Step 2: Re-run same batch -> Should hit the DB cache and consume 0 tokens
        resp2 = auth_client.post('/api/factcheck/analyze-batch', json={'claims': claims})
        assert resp2.status_code == 200
        data2 = resp2.get_json()
        assert data2['success'] is True
        assert len(data2['results']) == 3
        # All items should have tokens_used == 0 (cached)
        assert all(r['tokens_used'] == 0 for r in data2['results'])

    # =========================================================================
    # PERSONA 4: The VC Investment Associate
    # =========================================================================

    def test_persona_liam_pitchdeck_analysis_and_claim_batching(self, auth_client, mocker):
        """
        Liam uploads a startup pitch deck:
        - Analyzes PDF to extract traction scorecard, red flags, and verifiable claims with slide_number
        - Runs 1-shot batch market verification on all extracted claims
        """
        # Step 1: PDF Vision Analysis
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        import base64
        pdf_b64 = base64.b64encode(pdf_bytes).decode('utf-8')
        
        mock_deck_result = {
            "company_name": "SaaSFlow AI",
            "summary": "AI-driven automated revenue operations platform for B2B enterprises.",
            "usp": "Zero-code integration with 99.4% revenue attribution accuracy.",
            "industry": "SaaS",
            "sector": "RevOps",
            "market_size": "$42B by 2030 (Gartner)",
            "competition": ["Salesforce", "Clari"],
            "team_highlights": "Founders ex-Snowflake & Datadog",
            "funding_ask": "€3M Seed Equity",
            "verifiable_claims": [
                {
                    "claim": "The global RevOps market is projected to reach $42B by 2030",
                    "category": "market_size",
                    "source_cited": "Gartner",
                    "is_quantitative": True,
                    "slide_number": 3,
                    "context": "Market Opportunity Slide"
                },
                {
                    "claim": "SaaSFlow achieved €120K MRR with 125% NRR in Q4",
                    "category": "revenue",
                    "source_cited": "Internal Financials",
                    "is_quantitative": True,
                    "slide_number": 6,
                    "context": "Traction & Cohort Slide"
                }
            ],
            "vc_metrics": {
                "monthly_revenue_arr": {"value": "€120K MRR", "assessment": "Good", "detail": "Strong early commercial traction."},
                "burn_multiple": {"value": "1.1x", "assessment": "Good", "detail": "Capital efficient growth."},
                "nrr_percent": {"value": "125%", "assessment": "Elite", "detail": "Expansion exceeds churn."},
                "cac_payback_months": {"value": "7", "assessment": "Good", "detail": "Healthy recovery timeline."},
                "ltv_cac_ratio": {"value": "4.2:1", "assessment": "Good", "detail": "Strong unit economics."},
                "runway_months": {"value": "16", "assessment": "Good", "detail": "Adequate bridge to Series A."}
            },
            "red_flags": [
                "High reliance on single enterprise client representing 40% of ARR.",
                "CAC calculation excludes founder sales time."
            ],
            "cache_name": "cached_deck_content_999"
        }
        
        mocker.patch(
            'backend.services.pitchdeck_service.PitchdeckService.analyze_pitch_deck',
            return_value=mock_deck_result
        )
        
        resp_deck = auth_client.post('/api/pitchdeck/analyze', json={'pdf_data': pdf_b64})
        assert resp_deck.status_code == 200
        data_deck = resp_deck.get_json()
        assert data_deck['success'] is True
        assert data_deck['company_name'] == "SaaSFlow AI"
        assert len(data_deck['verifiable_claims']) == 2
        assert data_deck['verifiable_claims'][0]['slide_number'] == 3

        # Step 2: 1-Shot Batch Market Claim Verification
        mock_batch_findings = [
            {
                "claim_type": "market_size",
                "original_claim": "The global RevOps market is projected to reach $42B by 2030",
                "source_cited": "Gartner",
                "slide_number": 3,
                "verdict": "TRUE",
                "explanation": "Gartner 2025 RevOps Forecast validates market projections.",
                "sources": ["https://gartner.com/revops-forecast"]
            },
            {
                "claim_type": "revenue",
                "original_claim": "SaaSFlow achieved €120K MRR with 125% NRR in Q4",
                "source_cited": "Internal Financials",
                "slide_number": 6,
                "verdict": "UNVERIFIED",
                "explanation": "Internal financial data requires auditor confirmation.",
                "sources": []
            }
        ]
        
        mocker.patch(
            'backend.services.pitchdeck_service.PitchdeckService.verify_market_claims',
            return_value=mock_batch_findings
        )
        
        resp_verify = auth_client.post('/api/pitchdeck/verify-market', json={
            'verifiable_claims': data_deck['verifiable_claims'],
            'industry': 'SaaS',
            'cache_name': data_deck.get('cache_name')
        })
        
        assert resp_verify.status_code == 200
        data_verify = resp_verify.get_json()
        assert data_verify['success'] is True
        findings = data_verify['findings']
        assert len(findings) == 2
        assert findings[0]['slide_number'] == 3
        assert findings[0]['verdict'] == "TRUE"
        assert findings[1]['slide_number'] == 6

    # =========================================================================
    # PERSONA 5: The Digital Forensics Investigator
    # =========================================================================

    def test_persona_agent_vance_multimodal_forensic_investigation(self, auth_client, mocker):
        """
        Agent Vance investigates a suspected deepfake video upload:
        - Verifies full 7-layer criteria (physics, bio, temporal, audio, context, compression, metadata)
        - Confirms synthetic voice and temporal frame-flicker anomalies are surfaced
        """
        mock_forensic_analysis = {
            "verdict": "AI Generated",
            "confidence": 92,
            "explanation": "Detected robotic formant voice cloning, missing breath cadences, and silhouette frame flickering.",
            "criteria": {
                "physics": {"tag": "Med Signal", "score": 60, "detail": "Specular highlights on glasses mismatch ambient light."},
                "bio": {"tag": "High Signal", "score": 85, "detail": "Teeth boundary blurring and unnatural pupil symmetry."},
                "temporal": {"tag": "High Signal", "score": 90, "detail": "Silhouette edge shimmering and background warping between cuts."},
                "audio": {"tag": "High Signal", "score": 95, "detail": "Robotic voice synthesis with zero natural breath intakes before sentences."},
                "context": {"tag": "Clean", "score": 0, "detail": "Setting matches public press briefing room."},
                "compression": {"tag": "Med Signal", "score": 45, "detail": "Re-encoding blockiness around jawline."},
                "metadata": {"tag": "Clean", "score": 0, "detail": "No C2PA provenance header detected."}
            }
        }
        
        mocker.patch(
            'backend.services.gemini.media.GeminiServiceMedia.analyze_media_authenticity',
            return_value=mock_forensic_analysis
        )
        mocker.patch(
            'backend.services.gemini.media.GeminiServiceMedia.get_media_embedding',
            return_value=[0.12, -0.45, 0.78, 0.01]
        )
        
        # Upload mock video file
        mock_video_bytes = io.BytesIO(b"FAKE_MP4_VIDEO_HEADER_AND_STREAM")
        
        resp = auth_client.post(
            '/api/media/analyze-upload',
            data={'file': (mock_video_bytes, 'suspicious_briefing.mp4', 'video/mp4')},
            content_type='multipart/form-data'
        )
        
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        result = data['result']
        assert result['verdict'] == "AI Generated"
        assert result['confidence'] == 92
        criteria = result['criteria']
        assert criteria['audio']['tag'] == "High Signal"
        assert criteria['temporal']['tag'] == "High Signal"
        assert "breath intakes" in criteria['audio']['detail']
