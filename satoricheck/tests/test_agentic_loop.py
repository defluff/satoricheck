"""
Test Agentic Loop Logic in GeminiService.
Simulates multi-turn conversations and tool usage.
"""
import pytest
from unittest.mock import patch, MagicMock
from backend.services.gemini_service import GeminiService

class TestAgenticLoop:
    
    @patch('backend.services.gemini.client.genai.Client')
    @patch('backend.services.grok_service.get_grok_service')
    def test_agentic_loop_flow(self, mock_get_grok, mock_client_class):
        """
        Test a full agentic flow:
        1. User asks question
        2. Agent THINKS and calls TOOL (search_social)
        3. Tool returns result
        4. Agent returns FINAL VERDICT
        """
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        # Mock Grok Service
        mock_grok = MagicMock()
        mock_grok.get_tool_definition.return_value = {"name": "search_social"}
        mock_grok.search_social.return_value = {"found": True, "text": "Viral tweet confirmed"}
        mock_get_grok.return_value = mock_grok
        
        # RESPONSE 1: Agent decides to use a tool
        fc = MagicMock()
        fc.name = "search_social"
        fc.args = {"query": "breaking news"}
        
        part_thought = MagicMock()
        part_thought.thought = True
        part_thought.text = "I need to check social media."
        
        part_fc = MagicMock()
        part_fc.thought = False
        part_fc.text = None
        
        content1 = MagicMock()
        content1.parts = [part_thought, part_fc]
        
        candidate1 = MagicMock()
        candidate1.content = content1
        
        resp1 = MagicMock()
        resp1.candidates = [candidate1]
        resp1.function_calls = [fc]
        
        # RESPONSE 2: Agent gives final verdict after seeing tool result
        part_text = MagicMock()
        part_text.thought = False
        part_text.text = '{"verdict": "TRUE", "is_claim": true, "explanation": "Confirmed by viral tweet."}'
        
        content2 = MagicMock()
        content2.parts = [part_text]
        
        candidate2 = MagicMock()
        candidate2.content = content2
        
        resp2 = MagicMock()
        resp2.candidates = [candidate2]
        resp2.function_calls = []
        
        mock_client.models.generate_content.side_effect = [resp1, resp2]
        
        service = GeminiService()
        result = service._analyze_claim_agentic("Is there breaking news?")
        
        # Verification
        assert result['verdict'] == "TRUE"
        assert result['explanation'] == "Confirmed by viral tweet."
        
        # Verify Tool was actually called
        mock_grok.search_social.assert_called_once_with("breaking news")
        
        # Verify API called twice (once for tool, once for final)
        assert mock_client.models.generate_content.call_count == 2

    @patch('backend.services.gemini.client.genai.Client')
    @patch('backend.services.grok_service.get_grok_service')
    def test_agentic_loop_max_turns(self, mock_get_grok, mock_client_class):
        """Test that the loop terminates if max turns reached."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        mock_grok = MagicMock()
        mock_grok.get_tool_definition.return_value = {"name": "search_social"}
        mock_grok.search_social.return_value = {"found": False}
        mock_get_grok.return_value = mock_grok

        # Response that ALWAYS asks for a tool (infinite loop scenario)
        fc = MagicMock()
        fc.name = "search_social"
        fc.args = {"query": "loop"}
        
        content = MagicMock()
        content.parts = [MagicMock()]
        
        candidate = MagicMock()
        candidate.content = content
        
        resp_loop = MagicMock()
        resp_loop.candidates = [candidate]
        resp_loop.function_calls = [fc]
        
        # Set side_effect to return this forever
        mock_client.models.generate_content.side_effect = [resp_loop] * 10
        
        service = GeminiService()
        # Expect fallback to _analyze_claim_standard
        with patch.object(service, '_analyze_claim_standard') as mock_standard:
             mock_standard.return_value = {"verdict": "FALLBACK"}
             
             result = service._analyze_claim_agentic("Infinite loop test")
             
             # Should have called standard analysis
             mock_standard.assert_called_once()
             assert result['verdict'] == "FALLBACK"
             
        # Should have tried 5 times (max_turns)
        assert mock_client.models.generate_content.call_count == 5

    @patch('backend.services.gemini.client.genai.Client')
    @patch('backend.services.grok_service.get_grok_service')
    def test_agentic_loop_cache_conflict_retry(self, mock_get_grok, mock_client_class):
        """
        Test that when cache conflict / error occurs on a cached generation call,
        the agentic loop catches the exception, clears the cached_content,
        and immediately retries the API call without the cache in the same turn.
        """
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        mock_grok = MagicMock()
        mock_grok.get_tool_definition.return_value = {"name": "search_social"}
        mock_get_grok.return_value = mock_grok
        
        part_text = MagicMock()
        part_text.thought = False
        part_text.text = '{"verdict": "FALSE", "is_claim": true, "explanation": "Recovered from cache conflict."}'
        
        content = MagicMock()
        content.parts = [part_text]
        
        candidate = MagicMock()
        candidate.content = content
        
        resp = MagicMock()
        resp.candidates = [candidate]
        resp.function_calls = []
        
        recorded_configs = []
        def side_effect(*args, **kwargs):
            cfg = kwargs.get('config')
            recorded_configs.append(cfg.cached_content if cfg else None)
            if len(recorded_configs) == 1:
                raise Exception("400 Cache conflict tools")
            return resp
            
        mock_client.models.generate_content.side_effect = side_effect
        
        service = GeminiService()
        service.client = mock_client
        
        result = service._analyze_claim_agentic("Test claim with cache", cache_name="cachedContents/conflict-cache")
        
        assert result['verdict'] == "FALSE"
        assert result['explanation'] == "Recovered from cache conflict."
        
        # generate_content should have been called exactly 2 times
        assert mock_client.models.generate_content.call_count == 2
        
        # Verify cached_content at the time of each call
        assert len(recorded_configs) == 2
        assert recorded_configs[0] == "cachedContents/conflict-cache"
        assert recorded_configs[1] is None


