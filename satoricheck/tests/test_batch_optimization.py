"""
Test batch optimization for fact-checking.
Verifies that batching reduces API calls from N to 1.
"""
import pytest
from unittest.mock import patch, MagicMock
from backend.services import get_gemini_service

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

class TestBatchOptimization:
    
    @pytest.fixture(autouse=True)
    def mock_gemini_client(self):
        """Mock the genai.Client on the global GeminiService singleton."""
        with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            svc = get_gemini_service()
            old_client = svc.client
            svc.client = mock_client
            yield mock_client
            svc.client = old_client

    def test_legacy_chatty_behavior(self, auth_client, mock_gemini_client):
        """
        REPRODUCTION TEST:
        Simulate frontend behavior: Identifying 3 claims and sending 3 separate requests.
        Expectation: 3 separate calls to Gemini API.
        """
        # Setup mock for SINGLE claim response
        mock_gemini_client.models.generate_content.return_value = make_mock_response(
            '{"is_claim": true, "verdict": "TRUE", "explanation": "Verified.", "sources": ["http://example.com"]}'
        )

        claims = [
            "The sky is blue.",
            "Water is wet.",
            "Fire is hot."
        ]
        
        # Simulate frontend loop
        for claim in claims:
            auth_client.post('/api/factcheck/analyze', json={'text': claim})
            
        assert mock_gemini_client.models.generate_content.call_count == 3
        print(f"\n[Legacy] Sent {len(claims)} claims -> {mock_gemini_client.models.generate_content.call_count} API calls (Expected: {len(claims)})")

    def test_batch_endpoint_behavior(self, auth_client, mock_gemini_client):
        """
        VERIFICATION TEST:
        Send 3 claims in ONE batch request.
        Expectation: 1 triage + 1 batch call = 2 calls to Gemini API.
        """
        triage_response = make_mock_response(
            '[{"index": 1, "priority": "NORMAL", "strategy": "SEARCH_VERIFY"}, {"index": 2, "priority": "NORMAL", "strategy": "SEARCH_VERIFY"}, {"index": 3, "priority": "NORMAL", "strategy": "SEARCH_VERIFY"}]'
        )
        batch_response = make_mock_response(
            '{"results": [{"claim_index": 1, "is_claim": true, "verdict": "TRUE", "explanation": "Verified.", "sources": ["http://example.com"]}, {"claim_index": 2, "is_claim": true, "verdict": "FALSE", "explanation": "Debunked.", "sources": []}, {"claim_index": 3, "is_claim": true, "verdict": "MISLEADING", "explanation": "Context missing.", "sources": []}]}'
        )
        mock_gemini_client.models.generate_content.side_effect = [triage_response, batch_response]

        claims = [
            "The earth is round.",
            "The moon is flat.",
            "Sun is hot."
        ]
        
        response = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': claims
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert len(data['results']) == 3
        
        # 1 triage + 1 batch verify = 2 calls
        assert mock_gemini_client.models.generate_content.call_count == 2
        print(f"\n[Batch] Sent {len(claims)} claims -> {mock_gemini_client.models.generate_content.call_count} API calls (Expected: 2 with triage)")
