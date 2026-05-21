"""
Test Smart Agent scaling logic (text chunking).
"""
import pytest
from unittest.mock import patch, MagicMock
from backend.services.gemini_service import GeminiService

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

class TestSmartAgentScaling:
    
    @pytest.fixture
    def gemini_service(self):
        with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            service = GeminiService()
            service.client = mock_client
            return service

    def test_chunk_text_logic(self, gemini_service):
        """Test that _chunk_text splits correctly with overlap."""
        # Create a text string of length 100
        text = "0123456789" * 10
        assert len(text) == 100
        
        # Chunk with size 50, overlap 10
        chunks = gemini_service._chunk_text(text, chunk_size=50, overlap=10)
        
        # Expect 3 chunks:
        # 1. 0-50
        # 2. 40-90 (starts at 50-10=40)
        # 3. 80-100 (starts at 90-10=80)
        assert len(chunks) >= 2
        
        # Check overlap
        # Chunk 1 ends with ...456789 (indices 40-49)
        # Chunk 2 starts with ...456789 (indices 40-49)
        assert chunks[0][-10:] == chunks[1][:10]
        
    def test_identify_claims_large_text(self, gemini_service):
        """Test identify_claims calls API multiple times for large text."""
        # Create a mock long text (> 4000 chars) that forces chunking
        # Default chunk is 4000. Let's make text 6000 chars.
        long_text = "Statement. " * 600 
        
        summary_response = make_mock_response("Context Summary of the statement")
        claims_response = make_mock_response('{"claims": ["Claim A", "Claim B"]}')
        
        gemini_service.client.models.generate_content.side_effect = [
            summary_response,
            claims_response,
            claims_response
        ]
        
        claims = gemini_service.identify_claims(long_text)
        
        # Should have called API 3 times (1 summary + 2 chunks)
        assert gemini_service.client.models.generate_content.call_count == 3
        
        # Claims should be aggregated
        # API returns ["Claim A", "Claim B"] each time. Set dedumps.
        assert "Claim A" in claims
        assert "Claim B" in claims
        assert len(claims) == 2

    def test_deduplication(self, gemini_service):
        """Test that duplicate claims across chunks are removed."""
        text = "Short text."
        
        # Force 2 chunks even for short text by mocking _chunk_text
        with patch.object(gemini_service, '_chunk_text', return_value=["Chunk1", "Chunk2"]):
            summary_response = make_mock_response("Context Summary")
            claims_response = make_mock_response('{"claims": ["Same Claim"]}')
            
            gemini_service.client.models.generate_content.side_effect = [
                summary_response,
                claims_response,
                claims_response
            ]
            
            claims = gemini_service.identify_claims(text)
            
            # Should be only 1 claim despite 2 API calls returning it
            assert len(claims) == 1
            assert claims[0] == "Same Claim"
            assert gemini_service.client.models.generate_content.call_count == 3
