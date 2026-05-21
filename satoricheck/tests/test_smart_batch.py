"""
Tests for smart batch analysis functionality.
Updated after context cache and batching improvements.
"""

import pytest
from unittest.mock import MagicMock, patch
from backend.services.gemini_service import GeminiService, ClaimPriority

@pytest.fixture
def gemini_service():
    with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        service = GeminiService()
        service.client = mock_client
        return service

def make_mock_response(text):
    part = MagicMock()
    part.thought = False
    part.text = text
    
    content = MagicMock()
    content.parts = [part]
    
    candidate = MagicMock()
    candidate.content = content
    
    response = MagicMock()
    response.candidates = [candidate]
    response.text = text
    response.function_calls = []
    return response

def test_analyze_claims_batch_no_context(gemini_service):
    """Verify batch analysis handles claims without context."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "TRUE", "sources": ["http://x.com"]}]}'
    )
    
    claims = ["Sky is blue"]
    results = gemini_service.analyze_claims_batch(claims)
    
    assert len(results) == 1
    assert results[0]["verdict"] == "TRUE"
    assert gemini_service.client.models.generate_content.call_count == 1

def test_analyze_claims_batch_with_context_cache(gemini_service):
    """Verify batch analysis with context creates cache when large enough."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "TRUE", "strategy_used": "CONTEXT_CHECK", "sources": ["http://example.com"]}]}'
    )
    
    # Mock Cache Creation
    gemini_service.create_cache = MagicMock(return_value="cachedContents/12345")
    
    claims = ["The consulate opened in Nuuk."]
    context = "Reference text about Nuuk consulate..." * 150  # >4000 chars
    
    results = gemini_service.analyze_claims_batch(claims, context=context)
    
    # Verify Cache created
    gemini_service.create_cache.assert_called_once_with(context, ttl_minutes=5)
    
    # Verify result returned
    assert len(results) == 1
    assert results[0]["verdict"] == "TRUE"

def test_analyze_claims_batch_reuses_precreated_cache(gemini_service):
    """Verify batch analysis reuses a pre-created cache_name instead of creating a new one."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "FALSE", "sources": ["http://src.com"]}]}'
    )
    
    gemini_service.create_cache = MagicMock()

    claims = ["Claim A"]
    context = "X" * 5000  # Large context
    cache_name = "cachedContents/pre-created-abc"

    results = gemini_service.analyze_claims_batch(claims, context=context, cache_name=cache_name)

    # Cache should NOT be created again since cache_name was provided
    gemini_service.create_cache.assert_not_called()
    assert results[0]["verdict"] == "FALSE"

    # Verify cachedContent was injected into config
    call_args = gemini_service.client.models.generate_content.call_args
    config = call_args.kwargs.get('config')
    assert config.cached_content == cache_name

def test_analyze_claims_batch_sub_batches_large_lists(gemini_service):
    """Verify large claim lists are split into sub-batches."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "TRUE", "sources": ["http://a.com"]}]}'
    )

    # 10 claims > MAX_CLAIMS_PER_PROMPT (8), so should split into 2 sub-batches
    claims = [f"Claim {i}" for i in range(10)]
    results = gemini_service.analyze_claims_batch(claims)

    assert len(results) == 10
    assert all(r is not None for r in results)
    # 2 sub-batches + 1 triage call = 3 total calls
    assert gemini_service.client.models.generate_content.call_count == 3

def test_claim_priority_enum():
    """Verify ClaimPriority enum for Funnel architecture."""
    assert ClaimPriority.IMMEDIATE == "immediate"
    assert ClaimPriority.NORMAL == "normal"
    assert ClaimPriority.DEFERRED == "deferred"
    assert ClaimPriority.SKIP == "skip"

def test_triage_for_stream_classifies_claims(gemini_service):
    """Verify triage_for_stream calls Flash-Lite and returns structured results."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '[{"index": 1, "priority": "IMMEDIATE", "strategy": "SOCIAL_VERIFY"}, {"index": 2, "priority": "NORMAL", "strategy": "SEARCH_VERIFY"}]'
    )
    
    claims = ["Breaking news claim", "Historical fact"]
    results = gemini_service.triage_for_stream(claims)
    
    assert len(results) == 2
    assert results[0]["claim"] == "Breaking news claim"
    assert results[0]["priority"] == ClaimPriority.IMMEDIATE
    assert results[0]["strategy"] == "SOCIAL_VERIFY"
    assert results[1]["claim"] == "Historical fact"
    assert results[1]["priority"] == ClaimPriority.NORMAL

def test_triage_fallback_on_error(gemini_service):
    """Verify triage falls back to NORMAL priority on API error."""
    gemini_service.client.models.generate_content.side_effect = Exception("API Error")
    
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
    assert GeminiService._normalize_claim_text("The sky is blue.") == GeminiService._normalize_claim_text("The sky is blue")

def test_identify_claims_single_chunk_no_summary(gemini_service):
    """Verify short text skips context summary generation."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"claims": ["Water boils at 100C"]}'
    )

    short_text = "Water boils at 100 degrees Celsius at sea level."
    claims = gemini_service.identify_claims(short_text)

    assert len(claims) == 1
    assert "100" in claims[0]
    assert gemini_service.client.models.generate_content.call_count == 1


def test_analyze_claims_batch_cache_conflict_retry(gemini_service):
    """Verify that when a cache conflict occurs during batch analysis,
    the service catches the exception, clears the cache reference, and retries."""
    
    # The batch verification response
    batch_response = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "FALSE", "sources": ["http://src.com"]}]}'
    )
    
    recorded_configs = []
    def side_effect(*args, **kwargs):
        cfg = kwargs.get('config')
        recorded_configs.append(cfg.cached_content if cfg else None)
        if len(recorded_configs) == 1:
            raise Exception("400 Cache conflict tools")
        return batch_response

    gemini_service.client.models.generate_content.side_effect = side_effect
    
    claims = ["Claim A"]
    context = "Some large context"
    cache_name = "cachedContents/conflict-cache"
    
    results = gemini_service.analyze_claims_batch(claims, context=context, cache_name=cache_name)
    
    # Verify result
    assert len(results) == 1
    assert results[0]["verdict"] == "FALSE"
    
    # Verify generate_content called 2 times (no triage call since len(claims) <= 2)
    assert gemini_service.client.models.generate_content.call_count == 2
    
    # Verify cached_content at the time of each call
    assert len(recorded_configs) == 2
    assert recorded_configs[0] == "cachedContents/conflict-cache"
    assert recorded_configs[1] is None


