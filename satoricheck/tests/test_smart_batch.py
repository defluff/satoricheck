"""
Tests for smart batch analysis functionality.
Updated after context cache and batching improvements.
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


@patch('backend.services.gemini_service.requests.post')
def test_analyze_claims_batch_reuses_precreated_cache(mock_post, gemini_service):
    """Verify batch analysis reuses a pre-created cache_name instead of creating a new one."""
    gemini_service.create_cache = MagicMock()

    mock_post.return_value.status_code = 200
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": '{"results": [{"claim_index": 1, "verdict": "FALSE", "sources": ["http://src.com"]}]}'
                }]
            }
        }]
    }

    claims = ["Claim A"]
    context = "X" * 5000  # Large context
    cache_name = "cachedContents/pre-created-abc"

    results = gemini_service.analyze_claims_batch(claims, context=context, cache_name=cache_name)

    # Cache should NOT be created again since cache_name was provided
    gemini_service.create_cache.assert_not_called()
    assert results[0]["verdict"] == "FALSE"

    # Verify cachedContent was injected into payload
    call_args = mock_post.call_args
    payload = call_args.kwargs.get('json') or call_args[1].get('json')
    assert payload["cachedContent"] == cache_name


@patch('backend.services.gemini_service.requests.post')
def test_analyze_claims_batch_sub_batches_large_lists(mock_post, gemini_service):
    """Verify large claim lists are split into sub-batches."""
    mock_post.return_value.status_code = 200
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": '{"results": [{"claim_index": 1, "verdict": "TRUE", "sources": ["http://a.com"]}]}'
                }]
            }
        }]
    }

    # 10 claims > MAX_CLAIMS_PER_PROMPT (8), so should split into 2 sub-batches
    claims = [f"Claim {i}" for i in range(10)]
    results = gemini_service.analyze_claims_batch(claims)

    assert len(results) == 10
    # All results should be populated (no None values)
    assert all(r is not None for r in results)
    # Agentic loop should have been called twice (2 sub-batches of 8 + 2)
    # Note: triage also makes a call, so total = 1 triage + 2 agentic = 3
    assert mock_post.call_count >= 2


def test_claim_priority_enum():
    """Verify ClaimPriority enum for Funnel architecture."""
    assert ClaimPriority.IMMEDIATE == "immediate"
    assert ClaimPriority.NORMAL == "normal"
    assert ClaimPriority.DEFERRED == "deferred"
    assert ClaimPriority.SKIP == "skip"


@patch('backend.services.gemini_service.requests.post')
def test_triage_for_stream_classifies_claims(mock_post, gemini_service):
    """Verify triage_for_stream calls Flash-Lite and returns structured results."""
    # Mock Flash-Lite triage response
    mock_post.return_value.status_code = 200
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": '[{"index": 1, "priority": "IMMEDIATE", "strategy": "SOCIAL_VERIFY"}, {"index": 2, "priority": "NORMAL", "strategy": "SEARCH_VERIFY"}]'
                }]
            }
        }]
    }
    
    claims = ["Breaking news claim", "Historical fact"]
    results = gemini_service.triage_for_stream(claims)
    
    assert len(results) == 2
    assert results[0]["claim"] == "Breaking news claim"
    assert results[0]["priority"] == ClaimPriority.IMMEDIATE
    assert results[0]["strategy"] == "SOCIAL_VERIFY"
    assert results[1]["claim"] == "Historical fact"
    assert results[1]["priority"] == ClaimPriority.NORMAL


@patch('backend.services.gemini_service.requests.post')
def test_triage_fallback_on_error(mock_post, gemini_service):
    """Verify triage falls back to NORMAL priority on API error."""
    mock_post.side_effect = Exception("API Error")
    
    claims = ["Some claim"]
    results = gemini_service.triage_for_stream(claims)
    
    # Should fallback to NORMAL
    assert len(results) == 1
    assert results[0]["priority"] == ClaimPriority.NORMAL


def test_normalize_claim_text():
    """Verify claim text normalization strips punctuation, collapses whitespace, lowercases."""
    assert GeminiService._normalize_claim_text("GDP grew 4% in 2024.") == "gdp grew 4% in 2024"
    assert GeminiService._normalize_claim_text("GDP grew 4% in 2024!") == "gdp grew 4% in 2024"
    assert GeminiService._normalize_claim_text("  GDP  grew  4%  ") == "gdp grew 4%"
    # Same claim, different punctuation
    assert GeminiService._normalize_claim_text("The sky is blue.") == GeminiService._normalize_claim_text("The sky is blue")


@patch('backend.services.gemini_service.requests.post')
def test_identify_claims_single_chunk_no_summary(mock_post, gemini_service):
    """Verify short text skips context summary generation."""
    mock_post.return_value.status_code = 200
    mock_post.return_value.raise_for_status = MagicMock()
    mock_post.return_value.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": '{"claims": ["Water boils at 100C"]}'
                }]
            }
        }]
    }

    short_text = "Water boils at 100 degrees Celsius at sea level."
    claims = gemini_service.identify_claims(short_text)

    assert len(claims) == 1
    assert "100" in claims[0]
    # Only 1 API call (no summary call since single chunk)
    assert mock_post.call_count == 1
