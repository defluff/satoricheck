"""
Test Smart Agent scaling logic (text chunking).
"""
import pytest
from unittest.mock import patch, MagicMock
from backend.services.gemini_service import GeminiService

class TestSmartAgentScaling:
    
    def test_chunk_text_logic(self):
        """Test that _chunk_text splits correctly with overlap."""
        service = GeminiService()
        
        # Create a text string of length 100
        text = "0123456789" * 10
        assert len(text) == 100
        
        # Chunk with size 50, overlap 10
        chunks = service._chunk_text(text, chunk_size=50, overlap=10)
        
        # Expect 3 chunks:
        # 1. 0-50
        # 2. 40-90 (starts at 50-10=40)
        # 3. 80-100 (starts at 90-10=80)
        assert len(chunks) >= 2
        
        # Check overlap
        # Chunk 1 ends with ...456789 (indices 40-49)
        # Chunk 2 starts with ...456789 (indices 40-49)
        assert chunks[0][-10:] == chunks[1][:10]
        
    def test_identify_claims_large_text(self):
        """Test identify_claims calls API multiple times for large text."""
        service = GeminiService()
        
        # Create a mock long text (> 4000 chars) that forces chunking
        # Default chunk is 4000. Let's make text 6000 chars.
        long_text = "Statement. " * 600 
        
        with patch('backend.services.gemini_service.requests.post') as mock_post:
            # Mock successful response
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "candidates": [{
                    "content": {
                        "parts": [{"text": '{"claims": ["Claim A", "Claim B"]}'}]
                    }
                }]
            }
            mock_post.return_value = mock_response
            
            claims = service.identify_claims(long_text)
            
            # Should have called API at least twice (6000 / 4000 = ~2 chunks)
            assert mock_post.call_count >= 2
            
            # Claims should be aggregated
            # API returns ["Claim A", "Claim B"] each time. Set dedumps.
            assert "Claim A" in claims
            assert "Claim B" in claims
            assert len(claims) == 2

    def test_deduplication(self):
        """Test that duplicate claims across chunks are removed."""
        service = GeminiService()
        text = "Short text."
        
        # Force 2 chunks even for short text by mocking _chunk_text call or just relying on internal logic logic if we could inject it
        # Easier: just mock _chunk_text
        with patch.object(service, '_chunk_text', return_value=["Chunk1", "Chunk2"]):
             with patch('backend.services.gemini_service.requests.post') as mock_post:
                # Mock responses: Both chunks return "Same Claim"
                mock_response = MagicMock()
                mock_response.json.return_value = {
                    "candidates": [{"content": {"parts": [{"text": '{"claims": ["Same Claim"]}'}]}}]
                }
                mock_response.status_code = 200
                mock_post.return_value = mock_response
                
                claims = service.identify_claims(text)
                
                # Should be only 1 claim despite 2 API calls returning it
                assert len(claims) == 1
                assert claims[0] == "Same Claim"
