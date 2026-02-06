"""
Test Agentic Loop Logic in GeminiService.
Simulates multi-turn conversations and tool usage.
"""
import pytest
from unittest.mock import patch, MagicMock
from backend.services.gemini_service import GeminiService

class TestAgenticLoop:
    
    @patch('backend.services.gemini_service.requests.post')
    @patch('backend.services.grok_service.get_grok_service')
    def test_agentic_loop_flow(self, mock_get_grok, mock_post):
        """
        Test a full agentic flow:
        1. User asks question
        2. Agent THINKS and calls TOOL (search_social)
        3. Tool returns result
        4. Agent returns FINAL VERDICT
        """
        service = GeminiService()
        
        # Mock Grok Service
        mock_grok = MagicMock()
        mock_grok.get_tool_definition.return_value = {"name": "search_social"}
        mock_grok.search_social.return_value = {"found": True, "text": "Viral tweet confirmed"}
        mock_get_grok.return_value = mock_grok
        
        # We need to simulate a sequence of API responses from Gemini
        
        # RESPONSE 1: Agent decides to use a tool
        response_tool_call = MagicMock()
        response_tool_call.status_code = 200
        response_tool_call.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"thought": True, "text": "I need to check social media."},
                        {"functionCall": {"name": "search_social", "args": {"query": "breaking news"}}}
                    ]
                }
            }]
        }
        
        # RESPONSE 2: Agent gives final verdict after seeing tool result
        response_final = MagicMock()
        response_final.status_code = 200
        response_final.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"text": '{"verdict": "TRUE", "is_claim": true, "explanation": "Confirmed by viral tweet."}'}
                    ]
                }
            }]
        }
        
        # Set side_effect to return these in order
        mock_post.side_effect = [response_tool_call, response_final]
        
        # Execute
        result = service._analyze_claim_agentic("Is there breaking news?")
        
        # Verification
        assert result['verdict'] == "TRUE"
        assert result['explanation'] == "Confirmed by viral tweet."
        
        # Verify Tool was actually called
        mock_grok.search_social.assert_called_once_with("breaking news")
        
        # Verify API called twice (once for tool, once for final)
        assert mock_post.call_count == 2

    @patch('backend.services.gemini_service.requests.post')
    @patch('backend.services.grok_service.get_grok_service')
    def test_agentic_loop_max_turns(self, mock_get_grok, mock_post):
        """Test that the loop terminates if max turns reached."""
        service = GeminiService()
        
        mock_grok = MagicMock()
        mock_grok.get_tool_definition.return_value = {"name": "search_social"}
        mock_get_grok.return_value = mock_grok

        # Response that ALWAYS asks for a tool (infinite loop scenario)
        response_loop = MagicMock()
        response_loop.status_code = 200
        response_loop.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [
                        {"functionCall": {"name": "search_social", "args": {"query": "loop"}}}
                    ]
                }
            }]
        }
        
        # Set side_effect to return this forever
        mock_post.side_effect = [response_loop] * 10 
        
        # Expect fallback to _analyze_claim_standard
        with patch.object(service, '_analyze_claim_standard') as mock_standard:
             mock_standard.return_value = {"verdict": "FALLBACK"}
             
             result = service._analyze_claim_agentic("Infinite loop test")
             
             # Should have called standard analysis
             mock_standard.assert_called_once()
             assert result['verdict'] == "FALLBACK"
             
        # Should have tried 5 times (max_turns)
        assert mock_post.call_count == 5
