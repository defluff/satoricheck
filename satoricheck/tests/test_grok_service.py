"""
Grok Service Unit Tests.
Tests trigger detection, sanitization, and API response parsing.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestTriggerDetection:
    """Test should_fire_grok trigger logic."""
    
    def test_trigger_on_handle(self):
        """Should trigger on @mentions."""
        from backend.services.grok_service import should_fire_grok
        assert should_fire_grok("@elonmusk tweeted this") == True
        assert should_fire_grok("Check @realDonaldTrump") == True
    
    def test_trigger_on_hashtag(self):
        """Should trigger on #hashtags."""
        from backend.services.grok_service import should_fire_grok
        assert should_fire_grok("#breaking news today") == True
        assert should_fire_grok("Trending #Bitcoin") == True
    
    def test_trigger_on_temporal_keywords(self):
        """Should trigger on temporal keywords."""
        from backend.services.grok_service import should_fire_grok
        assert should_fire_grok("Trump just announced") == True
        assert should_fire_grok("happened today") == True
        assert should_fire_grok("breaking news") == True
    
    def test_trigger_on_quote_patterns(self):
        """Should trigger on quote claim patterns."""
        from backend.services.grok_service import should_fire_grok
        assert should_fire_grok("Trump said he will win") == True
        assert should_fire_grok("According to Elon") == True
        assert should_fire_grok("Biden tweeted about it") == True
    
    def test_no_trigger_on_normal_claims(self):
        """Should NOT trigger on regular factual claims."""
        from backend.services.grok_service import should_fire_grok
        assert should_fire_grok("The earth is round") == False
        assert should_fire_grok("Water boils at 100 degrees") == False
        assert should_fire_grok("Paris is the capital of France") == False
    
    def test_fallback_trigger_on_could_not_verify(self):
        """Should trigger when Gemini couldn't verify."""
        from backend.services.grok_service import should_fire_grok
        gemini_result = {'verdict': 'COULD_NOT_VERIFY'}
        assert should_fire_grok("Some obscure claim", gemini_result) == True


class TestSanitization:
    """Test security sanitization functions."""
    
    def test_strip_invisible_chars(self):
        """Should remove invisible Unicode characters."""
        from backend.services.grok_service import strip_invisible_chars
        
        # Zero-width space, BOM, other invisibles
        malicious = "Hello\u200bWorld\u2060Hidden\ufeffText"
        cleaned = strip_invisible_chars(malicious)
        assert cleaned == "HelloWorldHiddenText"
        assert '\u200b' not in cleaned
        assert '\ufeff' not in cleaned
    
    def test_strip_invisible_handles_none(self):
        """Should handle None input."""
        from backend.services.grok_service import strip_invisible_chars
        assert strip_invisible_chars(None) is None
        assert strip_invisible_chars("") == ""
    
    def test_validate_social_url_allows_twitter(self):
        """Should allow twitter.com and x.com URLs."""
        from backend.services.grok_service import validate_social_url
        assert validate_social_url("https://twitter.com/user/status/123") is not None
        assert validate_social_url("https://x.com/user/status/123") is not None
    
    def test_validate_social_url_blocks_other_domains(self):
        """Should block non-social URLs (SSRF protection)."""
        from backend.services.grok_service import validate_social_url
        assert validate_social_url("https://evil.com/script") is None
        assert validate_social_url("https://internal-server.local/admin") is None
        assert validate_social_url("file:///etc/passwd") is None


class TestGrokService:
    """Test GrokService class."""
    
    def test_service_init_without_key(self):
        """Should warn if API key not set."""
        from backend.services.grok_service import GrokService
        with patch('backend.services.grok_service.Config') as mock_config:
            mock_config.GROK_API_KEY = None
            mock_config.GROK_TIMEOUT = 10
            service = GrokService()
            assert service.api_key is None
    
    def test_search_social_returns_not_found_without_key(self):
        """Should return error when API key missing."""
        from backend.services.grok_service import GrokService
        with patch('backend.services.grok_service.Config') as mock_config:
            mock_config.GROK_API_KEY = None
            mock_config.GROK_TIMEOUT = 10
            service = GrokService()
            result = service.search_social("test claim")
            assert result['found'] == False
            assert 'error' in result
    
    @patch('backend.services.grok_service.requests.post')
    def test_search_social_handles_timeout(self, mock_post):
        """Should handle timeout gracefully."""
        import requests
        from backend.services.grok_service import GrokService
        
        mock_post.side_effect = requests.Timeout("Connection timed out")
        
        with patch('backend.services.grok_service.Config') as mock_config:
            mock_config.GROK_API_KEY = 'test-key'
            mock_config.GROK_TIMEOUT = 10
            service = GrokService()
            result = service.search_social("test claim")
            
        assert result['found'] == False
        assert result['error'] == 'timeout'
    
    @patch('backend.services.grok_service.requests.post')
    def test_search_social_parses_response(self, mock_post):
        """Should parse valid Grok API response."""
        from backend.services.grok_service import GrokService
        
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'choices': [{
                'message': {
                    'content': '{"found": true, "source": "@test", "text": "Test tweet"}'
                }
            }]
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response
        
        with patch('backend.services.grok_service.Config') as mock_config:
            mock_config.GROK_API_KEY = 'test-key'
            mock_config.GROK_TIMEOUT = 10
            service = GrokService()
            result = service.search_social("test claim")
            
        assert result['found'] == True
        assert result['source'] == '@test'
