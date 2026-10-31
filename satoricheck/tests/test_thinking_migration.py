"""
Tests verifying migration from deprecated sampling/thinking parameters to thinking_level.

Ensures:
1. No deprecated parameters (temperature, top_p, top_k, thinking_budget) are sent in Gemini configs.
2. Fast paths (triage, claim extraction) use thinking_level="low" — the lowest level
   supported by current Gemini models ("minimal" is rejected by gemini-3.8-flash / gemini-4-argon).
3. Agentic verification (single claim and batch) uses thinking_level="high" for deep reasoning.
4. All calls route through the single configured GeminiService.MODEL.
"""
import pytest
from unittest.mock import MagicMock, patch
from google.genai import types
from backend.services.gemini_service import GeminiService


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


def _assert_no_deprecated_sampling_params(config: types.GenerateContentConfig):
    """Assert that none of the deprecated sampling parameters are present."""
    assert getattr(config, 'temperature', None) is None, "temperature must not be set"
    assert getattr(config, 'top_p', None) is None, "top_p must not be set"
    assert getattr(config, 'top_k', None) is None, "top_k must not be set"
    if config.thinking_config:
        assert getattr(config.thinking_config, 'thinking_budget', None) is None, "thinking_budget must not be set"


def test_triage_for_stream_uses_low_thinking(gemini_service):
    """Verify stream triage enforces thinking_level='low' and zero sampling params."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '[{"index": 1, "priority": "NORMAL", "strategy": "SEARCH_VERIFY", "is_hyperbole": false}]'
    )

    claims = ["The unemployment rate reached 3.5% in 2024."]
    gemini_service.triage_for_stream(claims)

    assert gemini_service.client.models.generate_content.call_count == 1
    call_kwargs = gemini_service.client.models.generate_content.call_args.kwargs
    config = call_kwargs.get('config')

    assert config is not None
    _assert_no_deprecated_sampling_params(config)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW


def test_batch_agentic_uses_high_thinking(gemini_service):
    """Verify batch verification uses thinking_level='high' and removes temperature=1.0."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "TRUE", "explanation": "Valid", "sources": []}]}'
    )

    claims = ["Claim 1"]
    with patch.object(gemini_service, '_build_grok_tool', return_value=MagicMock()):
        gemini_service.analyze_claims_batch(claims)

    assert gemini_service.client.models.generate_content.call_count == 1
    call_kwargs = gemini_service.client.models.generate_content.call_args.kwargs
    config = call_kwargs.get('config')

    assert config is not None
    _assert_no_deprecated_sampling_params(config)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert config.thinking_config.include_thoughts is True


def test_single_agentic_claim_uses_high_thinking(gemini_service):
    """Verify single agentic claim analysis uses thinking_level='high' and no sampling params."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"is_claim": true, "verdict": "TRUE", "explanation": "Verified", "fallacy": null, "sources": []}'
    )

    with patch('backend.services.grok_service.get_grok_service') as mock_get_grok:
        mock_grok = MagicMock()
        mock_grok.get_tool_definition.return_value = {"name": "search_social"}
        mock_get_grok.return_value = mock_grok

        with patch.object(gemini_service, '_build_grok_tool', return_value=MagicMock()):
            gemini_service.analyze_claim("Test claim", smart_agent=True)

    call_kwargs = gemini_service.client.models.generate_content.call_args.kwargs
    config = call_kwargs.get('config')

    assert config is not None
    _assert_no_deprecated_sampling_params(config)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert config.thinking_config.include_thoughts is True


def test_unified_model_used_across_thinking_levels(gemini_service):
    """Verify extraction, AI detection, and verification all use unified GeminiService.MODEL."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"claims": ["Water boils at 100C"]}'
    )
    gemini_service.identify_claims("Water boils at 100C.")
    identify_kwargs = gemini_service.client.models.generate_content.call_args.kwargs
    assert identify_kwargs['model'] == gemini_service.MODEL
    assert identify_kwargs['config'].thinking_config.thinking_level == types.ThinkingLevel.LOW

    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "TRUE", "explanation": "Valid", "sources": []}]}'
    )
    gemini_service.analyze_claims_batch(["Water boils at 100C."])
    batch_kwargs = gemini_service.client.models.generate_content.call_args.kwargs
    assert batch_kwargs['model'] == gemini_service.MODEL
    assert batch_kwargs['config'].thinking_config.thinking_level == types.ThinkingLevel.HIGH


def test_grok_tool_gated_by_social_triggers(gemini_service):
    """Verify Grok tool is omitted on standard factual claims and attached on social triggers."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"results": [{"claim_index": 1, "verdict": "TRUE", "explanation": "Valid", "sources": []}]}'
    )

    with patch.object(gemini_service, '_build_grok_tool', return_value=MagicMock()) as mock_build_grok:
        # Non-social historical/scientific claim -> only GoogleSearch tool attached
        gemini_service.analyze_claims_batch(["Water boils at 100 degrees Celsius."])
        non_social_cfg = gemini_service.client.models.generate_content.call_args.kwargs['config']
        assert len(non_social_cfg.tools) == 1
        mock_build_grok.assert_not_called()

        # Social trigger claim (@handle / breaking news) -> GoogleSearch + Grok tool attached
        gemini_service.analyze_claims_batch(["@elonmusk just tweeted breaking news today"])
        social_cfg = gemini_service.client.models.generate_content.call_args.kwargs['config']
        assert len(social_cfg.tools) == 2
        mock_build_grok.assert_called_once()


