"""
Media Analysis Service & API Tests.

Follows TDD approach - tests written before backend implementation.
Covers:
- URL Validation (Regex & SSRF)
- Token Deduction
- Gemini Analysis (Verdict + Criteria)
- Multimodal Embeddings (Fingerprinting)
"""
import pytest
from unittest.mock import patch, MagicMock
import json
import base64

class TestMediaAnalysisService:
    """Unit tests for MediaAnalysis logic."""

    def test_analyze_media_url_returns_structured_verdict(self, app):
        """
        Given: A valid image URL
        When: analyze_media_url() is called
        Then: Returns structured JSON matching UI requirements (verdict, confidence, criteria)
        """
        from backend.services.gemini_service import GeminiService
        service = GeminiService()
        
        test_url = "https://example.com/deepfake.jpg"
        
        # Mock Gemini response for analysis
        mock_analysis_response = {
            'candidates': [{
                'content': {
                    'parts': [{'text': json.dumps({
                        'verdict': 'ai',
                        'confidence': 87,
                        'explanation': 'Biological anomalies detected.',
                        'criteria': {
                            'physics': {'signal': 'suspicious', 'fill': 82, 'desc': 'Inconsistent shadows.'},
                            'bio': {'signal': 'suspicious', 'fill': 91, 'desc': 'Finger count anomaly.'},
                            'context': {'signal': 'uncertain', 'fill': 55, 'desc': 'Plausible composition.'},
                            'compression': {'signal': 'suspicious', 'fill': 78, 'desc': 'Noise distribution anomaly.'},
                            'metadata': {'signal': 'uncertain', 'fill': 40, 'desc': 'No EXIF metadata.'}
                        }
                    })}]
                }
            }]
        }

        # Mock embedding response
        mock_embedding_response = {
            'embeddings': [{'values': [0.1, 0.2, 0.3]}]
        }

        with patch('requests.post') as mock_post, \
             patch('requests.get') as mock_get, \
             patch('backend.services.gemini_service.GeminiService._validate_url', return_value=True), \
             patch('backend.services.gemini_service.GeminiService.create_cache', return_value=None):
            # Mock URL download (B2 fix: URLs are now downloaded and inlined as base64)
            mock_get.return_value = MagicMock(
                status_code=200,
                content=b'\x89PNG\r\n\x1a\n'  # Minimal PNG header bytes
            )
            mock_get.return_value.raise_for_status = MagicMock()

            # 1. Test Analysis
            mock_post.return_value = MagicMock(status_code=200, json=lambda: mock_analysis_response)
            result = service.analyze_media_authenticity(test_url, 'url')
            
            assert result['verdict'] == 'ai'
            assert result['confidence'] == 87
            assert 'criteria' in result
            
            # 2. Test Embedding
            mock_post.return_value = MagicMock(status_code=200, json=lambda: mock_embedding_response)
            emb = service.get_media_embedding(test_url, "image/jpeg", input_type='url')
            assert len(emb) == 3

    def test_analyze_media_url_rejects_malicious_urls(self, app):
        """
        Given: A private IP URL (SSRF vector)
        When: analyze_media_url() is called
        Then: Raises ValueError (SSRF protection)
        """
        from backend.services.gemini_service import GeminiService
        service = GeminiService()
        
        malicious_url = "http://169.254.169.254/latest/meta-data/"
        
        with pytest.raises(ValueError) as exc:
            service.analyze_media_authenticity(malicious_url, 'url')
        
        # Should be caught by service layer validation
        assert "Invalid or restricted URL" in str(exc.value)

class TestMediaAnalysisAPI:
    """Integration tests for /api/media/analyze-url endpoint."""

    def test_analyze_url_requires_auth(self, client):
        """Unauthenticated requests should return 401."""
        response = client.post('/api/media/analyze-url', json={'url': 'https://example.com/img.jpg'})
        assert response.status_code == 401

    def test_analyze_url_deducts_tokens(self, auth_client, test_user, db_session_fixture, mocker):
        """Successful analysis should deduct 1 CP from user balance."""
        from backend.models import TokenBalance
        
        # Initial balance is 100 from test_user fixture
        
        # Mock the service to avoid real API calls
        mock_res = {'verdict': 'authentic', 'confidence': 99}
        
        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = mock_res
        mock_service.get_media_embedding.return_value = [0.1]*768
        
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)

        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://example.com/authentic.jpg'
        })
        
        assert response.status_code == 200
        
        # Check balance
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 99

    def test_analyze_url_rejects_invalid_regex(self, auth_client):
        """Should reject obvious non-URLs early."""
        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'not-a-url'
        })
        assert response.status_code == 400
        assert "Invalid URL format" in response.get_json()['error']

    def test_analyze_upload_success(self, auth_client, test_user, db_session_fixture, mocker):
        """Successful file upload should work and deduct CP."""
        from backend.models import TokenBalance
        import io
        
        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = {'verdict': 'authentic', 'confidence': 95}
        mock_service.get_media_embedding.return_value = [0.1]*768
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)
        
        data = {
            'file': (io.BytesIO(b"test file content"), 'test.jpg'),
        }
        
        response = auth_client.post('/api/media/analyze-upload', data=data, content_type='multipart/form-data')
        
        assert response.status_code == 200
        assert response.get_json()['success'] is True
        
        # Check balance
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 99

    def test_analyze_url_token_refund_on_failure(self, auth_client, test_user, db_session_fixture, mocker):
        """If analysis fails (generic exception), token should be refunded."""
        from backend.models import TokenBalance
        
        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.side_effect = Exception("API Down")
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)
        
        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://example.com/fail.jpg'
        })
        
        assert response.status_code == 503
        
        # Balance should still be 100 (refunded)
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 100

    def test_analyze_url_insufficient_tokens(self, auth_client, test_user, db_session_fixture):
        """Should return 403 if balance is 0."""
        from backend.models import TokenBalance
        
        # Set balance to 0
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        bal.balance = 0
        db_session_fixture.commit()
        
        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://example.com/no-tokens.jpg'
        })
        
        assert response.status_code == 403
        assert "Insufficient tokens" in response.get_json()['error']
