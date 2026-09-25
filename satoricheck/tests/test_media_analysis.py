"""
Media Analysis Service & API Tests.
Covers:
- URL Validation (Regex & SSRF)
- Token Deduction
- Gemini Analysis (Verdict + Criteria)
- Multimodal Embeddings (Fingerprinting)
"""
import pytest
from unittest.mock import patch, MagicMock
import json

class TestMediaAnalysisService:
    """Unit tests for MediaAnalysis logic."""

    def test_analyze_media_url_returns_structured_verdict(self, app):
        """
        Given: A valid image URL
        When: analyze_media_url() is called
        Then: Returns structured JSON matching UI requirements (verdict, confidence, criteria)
        """
        from backend.services.gemini_service import GeminiService
        
        test_url = "https://example.com/deepfake.jpg"
        
        # Mock Gemini response for analysis
        mock_analysis_text = json.dumps({
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
        })

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class, \
             patch('requests.get') as mock_get, \
             patch('backend.services.gemini_service.GeminiService._validate_url', return_value=True), \
             patch('backend.services.gemini_service.GeminiService.create_cache', return_value=None):
            
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            service = GeminiService()
            service.client = mock_client
            
            # Mock URL download
            mock_get.return_value = MagicMock(
                status_code=200,
                content=b'\x89PNG\r\n\x1a\n'  # Minimal PNG header bytes
            )
            mock_get.return_value.raise_for_status = MagicMock()

            # Mock generate_content response
            part = MagicMock()
            part.thought = False
            part.text = mock_analysis_text
            content = MagicMock()
            content.parts = [part]
            candidate = MagicMock()
            candidate.content = content
            response = MagicMock()
            response.candidates = [candidate]
            response.text = mock_analysis_text
            
            mock_client.models.generate_content.return_value = response

            # 1. Test Analysis
            result = service.analyze_media_authenticity(test_url, 'url')
            
            assert result['verdict'] == 'ai'
            assert result['confidence'] == 87
            assert 'criteria' in result
            
            # 2. Test Embedding
            mock_embedding_obj = MagicMock()
            mock_embedding_obj.values = [0.1, 0.2, 0.3]
            mock_embed_response = MagicMock()
            mock_embed_response.embeddings = [mock_embedding_obj]
            mock_client.models.embed_content.return_value = mock_embed_response
            
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
        """Should reject early."""
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

    def test_analyze_upload_audio_with_claims(self, auth_client, test_user, db_session_fixture, mocker):
        """Audio file upload should be accepted and return extracted spoken claims."""
        from backend.models import MediaCheck
        import io
        
        mock_claims = [
            {"timestamp": "00:15", "timestamp_seconds": 15, "claim": "Inflation dropped to 3.2% in 2024"},
            {"timestamp": "01:05", "timestamp_seconds": 65, "claim": "The legislation was passed unanimously"}
        ]
        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = {
            'verdict': 'Appears Authentic',
            'confidence': 94,
            'explanation': 'Natural vocal resonance.',
            'criteria': {
                'audio': {'tag': 'Clean', 'score': 5, 'detail': 'Natural speech rhythm.'},
                'temporal': {'tag': 'Clean', 'score': 0, 'detail': 'Clean.'}
            },
            'claims': mock_claims
        }
        mock_service.get_media_embedding.return_value = [0.1]*768
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)
        
        data = {
            'file': (io.BytesIO(b"fake audio mp3 bytes"), 'podcast_episode.mp3', 'audio/mpeg'),
        }
        
        response = auth_client.post('/api/media/analyze-upload', data=data, content_type='multipart/form-data')
        
        assert response.status_code == 200
        res_json = response.get_json()
        assert res_json['success'] is True
        assert len(res_json['result']['claims']) == 2
        assert res_json['result']['claims'][0]['timestamp'] == "00:15"
        
        # Verify persistence in DB
        check = db_session_fixture.query(MediaCheck).filter_by(user_id=test_user.id).order_by(MediaCheck.id.desc()).first()
        assert check is not None
        assert "Inflation dropped" in check.claims_json

    def test_analyze_youtube_url_success(self, auth_client, test_user, db_session_fixture, mocker):
        """Valid YouTube watch URL should be routed as video/mp4 without needing file download."""
        from backend.models import MediaCheck
        
        mock_claims = [
            {"timestamp": "00:30", "timestamp_seconds": 30, "claim": "Quantum computers broke RSA 2048 yesterday"}
        ]
        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = {
            'verdict': 'Suspicious / AI-Generated',
            'confidence': 88,
            'explanation': 'Synthetic voice cloning detected.',
            'criteria': {
                'audio': {'tag': 'Altered', 'score': 85, 'detail': 'Voice cloning artifacts.'},
                'temporal': {'tag': 'Clean', 'score': 10, 'detail': 'Consistent frames.'}
            },
            'claims': mock_claims
        }
        mock_service.get_media_embedding.return_value = []
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)
        
        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        })
        
        assert response.status_code == 200
        res = response.get_json()
        assert res['success'] is True
        assert res['result']['verdict'] == 'Suspicious / AI-Generated'
        assert len(res['result']['claims']) == 1
        assert res['result']['claims'][0]['claim'] == "Quantum computers broke RSA 2048 yesterday"
        
        # Verify call arguments
        mock_service.analyze_media_authenticity.assert_called_once_with(
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            input_type='url',
            mime_type='video/mp4'
        )

    def test_analyze_youtube_shortlink_success(self, auth_client, test_user, db_session_fixture, mocker):
        """Shortened youtu.be URL should be canonicalized and accepted."""
        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = {
            'verdict': 'Appears Authentic',
            'confidence': 92,
            'explanation': 'Authentic video stream.',
            'criteria': {},
            'claims': []
        }
        mock_service.get_media_embedding.return_value = []
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)
        
        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://youtu.be/dQw4w9WgXcQ?t=45'
        })
        
        assert response.status_code == 200
        mock_service.analyze_media_authenticity.assert_called_once_with(
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            input_type='url',
            mime_type='video/mp4'
        )

    def test_analyze_youtube_invalid_url_rejected(self, auth_client, test_user):
        """Channel or non-video YouTube link should return a 400 error."""
        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://www.youtube.com/channel/UC1234567890'
        })
        assert response.status_code == 400
        assert 'Invalid YouTube URL' in response.get_json()['error']

    def test_gemini_service_prepares_youtube_part_from_uri(self, app):
        """GeminiServiceMedia should construct Part.from_uri for YouTube and never call requests.get."""
        from backend.services.gemini_service import GeminiService
        
        service = GeminiService()
        youtube_url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        
        with patch.object(service, '_validate_url', return_value=True), \
             patch('requests.get') as mock_requests_get:
            part = service._prepare_media_part(youtube_url, 'video/mp4', input_type='url')
            
            # Must NOT call requests.get to download YouTube videos
            mock_requests_get.assert_not_called()
            
            # Must return Part with file_data pointing to YouTube URI
            assert hasattr(part, 'file_data')
            assert part.file_data.file_uri == youtube_url
            assert part.file_data.mime_type == 'video/mp4'


