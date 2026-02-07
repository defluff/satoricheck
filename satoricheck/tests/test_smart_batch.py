"""
Tests for smart batch analysis functionality.
Updated after legacy triage router removal.
"""

import pytest
from unittest.mock import MagicMock, patch
from backend.services.gemini_service import GeminiService, ClaimPriority


@pytest.fixture
def gemini_service():
    return GeminiService()


@patch('backend.services.gemini_service.requests.post')
def test_analyze_claims_batch_no_context(mock_post, gemini_service):
    """Verify batch analysis handles claims without context."""
    # Mock the API response
    mock_post.return_value.status_code = 200
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": '{"results": [{"claim_index": 1, "verdict": "TRUE", "sources": ["http://x.com"]}]}'
                }]
            }
        }]
    }
    
    claims = ["Sky is blue"]
    results = gemini_service.analyze_claims_batch(claims)
    
    assert len(results) == 1
    assert results[0]["verdict"] == "TRUE"


@patch('backend.services.gemini_service.requests.post')
def test_analyze_claims_batch_with_context_cache(mock_post, gemini_service):
    """Verify batch analysis with context creates cache when large enough."""
    # Mock Cache Creation
    gemini_service.create_cache = MagicMock(return_value="cachedContents/12345")
    
    # Mock API response
    mock_post.return_value.status_code = 200
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": '{"results": [{"claim_index": 1, "verdict": "TRUE", "strategy_used": "CONTEXT_CHECK", "sources": ["http://example.com"]}]}'
                }]
            }
        }]
    }
    
    claims = ["The consulate opened in Nuuk."]
    context = "Reference text about Nuuk consulate..." * 150  # >4000 chars
    
    results = gemini_service.analyze_claims_batch(claims, context=context)
    
    # Verify Cache created
    gemini_service.create_cache.assert_called_once_with(context, ttl_minutes=5)
    
    # Verify result returned
    assert len(results) == 1
    assert results[0]["verdict"] == "TRUE"


def test_analyze_agentic_batch_payload(gemini_service):
    """Verify _analyze_agentic_batch constructs correct Gemini 3 payload."""
    claims = ["Claim 1", "Claim 2"]
    cache_name = "cachedContents/ABC"
    
    # Mock internal execute loop to capture payload
    gemini_service._execute_agentic_batch_loop = MagicMock(return_value=[])
    
    # Mock Grok
    with patch('backend.services.grok_service.get_grok_service') as mock_get_grok:
        mock_grok = MagicMock()
        mock_grok.get_tool_definition.return_value = {"name": "search_social", "description": "Search X/Twitter"}
        mock_get_grok.return_value = mock_grok
        
        gemini_service._analyze_agentic_batch(claims, cache_name)
        
        # Check calls
        assert gemini_service._execute_agentic_batch_loop.called
        call_args = gemini_service._execute_agentic_batch_loop.call_args
        payload = call_args[0][0]
        
        # 1. Check Thinking Config (Gemini 3)
        assert "generationConfig" in payload
        assert "thinkingConfig" in payload["generationConfig"]
        assert payload["generationConfig"]["thinkingConfig"]["includeThoughts"] is True
        
        # 2. Check Cache
        assert payload["cachedContent"] == cache_name
        
        # 3. Check Prompt contains Phase instructions
        prompt_text = payload["contents"][0]["parts"][0]["text"]
        assert "PHASE 1 (ANALYSIS)" in prompt_text
        assert "PHASE 2 (VERIFICATON)" in prompt_text


def test_claim_priority_enum():
    """Verify ClaimPriority enum for Funnel architecture."""
    assert ClaimPriority.IMMEDIATE == "immediate"
    assert ClaimPriority.NORMAL == "normal"
    assert ClaimPriority.DEFERRED == "deferred"
    assert ClaimPriority.SKIP == "skip"


def test_triage_for_stream_stub(gemini_service):
    """Verify triage_for_stream returns expected stub format."""
    claims = ["Breaking news claim", "Historical fact"]
    
    results = gemini_service.triage_for_stream(claims)
    
    assert len(results) == 2
    assert results[0]["claim"] == "Breaking news claim"
    assert results[0]["priority"] == ClaimPriority.NORMAL
    assert results[0]["strategy"] == "SEARCH_VERIFY"
    assert results[1]["claim"] == "Historical fact"
