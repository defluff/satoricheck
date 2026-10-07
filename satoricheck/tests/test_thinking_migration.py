"""
Tests verifying migration from deprecated sampling/thinking parameters to thinking_level.

Ensures:
1. No deprecated parameters (temperature, top_p, top_k, thinking_budget) are sent in Gemini configs.
2. Fast stream triage uses thinking_level="minimal" to preserve low-latency SLAs.
3. Audio chunk extraction uses thinking_level="minimal".
4. Agentic verification (single claim and batch) uses thinking_level="high" for deep reasoning.
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


def test_triage_for_stream_uses_minimal_thinking(gemini_service):
    """Verify stream triage enforces thinking_level='minimal' and zero sampling params."""
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
    assert config.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL


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


def test_transcribe_audio_uses_minimal_thinking(gemini_service):
    """Verify audio claim extraction uses thinking_level='minimal' and zero sampling params."""
    gemini_service.client.models.generate_content.return_value = make_mock_response(
        '{"transcript": "Hello world", "claims": []}'
    )

    gemini_service.transcribe_and_extract_claims_from_audio(
        audio_bytes=b"fake-audio-bytes",
        mime_type="audio/webm"
    )

    assert gemini_service.client.models.generate_content.call_count == 1
    call_kwargs = gemini_service.client.models.generate_content.call_args.kwargs
    config = call_kwargs.get('config')

    assert config is not None
    _assert_no_deprecated_sampling_params(config)
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.MINIMAL
