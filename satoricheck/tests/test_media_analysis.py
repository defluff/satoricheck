"""
Media Analysis Service & API Tests.
Covers:
- URL Validation (Regex & SSRF)
- Token Deduction
- Gemini Analysis (Verdict + Criteria + Claims Cap)
- YouTube Timestamp Windowing & oEmbed Pre-Flight
"""
import io
import json
import pathlib
import socket
from unittest.mock import patch, MagicMock
import pytest

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

        public_dns = [(2, 1, 6, '', ('93.184.216.34', 443))]  # example.com
        png_bytes = b'\x89PNG\r\n\x1a\n'  # Minimal PNG header bytes

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class, \
             patch('backend.services.safe_http.socket.getaddrinfo', return_value=public_dns), \
             patch('backend.services.safe_http.requests.head') as mock_head, \
             patch('backend.services.safe_http.requests.get') as mock_get, \
             patch('backend.services.gemini_service.GeminiService.create_cache', return_value=None):
            
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            
            service = GeminiService()
            service.client = mock_client
            
            # Wire-level mocks: HEAD reachability probe + streamed GET download
            mock_head.return_value = MagicMock(status_code=200, headers={})
            mock_get.return_value = MagicMock(
                status_code=200,
                headers={'Content-Length': str(len(png_bytes))},
            )
            mock_get.return_value.iter_content.return_value = [png_bytes]

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
            mock_get.assert_called_once_with(
                test_url,
                timeout=30,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; Authenix/1.0)'},
                stream=True,
                allow_redirects=False,
            )

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
        """Successful image URL analysis should deduct 4 CP (MEDIA_CP_IMAGE) and return cost_incurred."""
        from backend.models import TokenBalance

        mock_res = {'verdict': 'authentic', 'confidence': 99}

        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = mock_res

        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)

        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://example.com/authentic.jpg'
        })

        assert response.status_code == 200
        res_json = response.get_json()
        assert res_json['cost_incurred'] == 4
        assert res_json['new_balance'] == 96

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 96

    def test_analyze_url_rejects_invalid_regex(self, auth_client):
        """Should reject early."""
        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'not-a-url'
        })
        assert response.status_code == 400
        assert "Invalid URL format" in response.get_json()['error']

    def test_analyze_upload_success(self, auth_client, test_user, db_session_fixture, mocker):
        """Successful image file upload should work and deduct 4 CP (MEDIA_CP_IMAGE)."""
        from backend.models import TokenBalance

        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = {'verdict': 'authentic', 'confidence': 95}
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)

        data = {
            'file': (io.BytesIO(b"test file content"), 'test.jpg'),
        }

        response = auth_client.post('/api/media/analyze-upload', data=data, content_type='multipart/form-data')

        assert response.status_code == 200
        res_json = response.get_json()
        assert res_json['success'] is True
        assert res_json['cost_incurred'] == 4
        assert res_json['new_balance'] == 96

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 96

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
        """Audio file upload should be accepted, deduct 5 CP, and return extracted spoken claims."""
        from backend.models import MediaCheck, TokenBalance

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
                'temporal': {'tag': 'N/A', 'score': 0, 'detail': 'Not applicable to audio-only media.'}
            },
            'claims': mock_claims
        }
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)

        data = {
            'file': (io.BytesIO(b"fake audio mp3 bytes"), 'podcast_episode.mp3', 'audio/mpeg'),
        }

        response = auth_client.post('/api/media/analyze-upload', data=data, content_type='multipart/form-data')

        assert response.status_code == 200
        res_json = response.get_json()
        assert res_json['success'] is True
        assert res_json['cost_incurred'] == 5
        assert res_json['new_balance'] == 95
        assert len(res_json['result']['claims']) == 2
        assert res_json['result']['claims'][0]['timestamp'] == "00:15"

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 95

        check = db_session_fixture.query(MediaCheck).filter_by(user_id=test_user.id).order_by(MediaCheck.id.desc()).first()
        assert check is not None
        assert "Inflation dropped" in check.claims_json

    def test_analyze_youtube_url_success(self, auth_client, test_user, db_session_fixture, mocker):
        """Valid YouTube watch URL should bill the full-window 40 CP tier and route as video/mp4 without download."""
        from backend.models import TokenBalance

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
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)

        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        })

        assert response.status_code == 200
        res = response.get_json()
        assert res['success'] is True
        assert res['cost_incurred'] == 40
        assert res['new_balance'] == 60
        assert res['result']['verdict'] == 'Suspicious / AI-Generated'
        assert len(res['result']['claims']) == 1
        assert res['result']['claims'][0]['claim'] == "Quantum computers broke RSA 2048 yesterday"

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 60

        mock_service.validate_youtube_video.assert_called_once_with(
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        )
        mock_service.analyze_media_authenticity.assert_called_once_with(
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            input_type='url',
            mime_type='video/mp4'
        )

    def test_analyze_youtube_shortlink_success(self, auth_client, test_user, db_session_fixture, mocker):
        """Shortened youtu.be URL with ?t=45 should forward start_seconds=45, bill 40 CP, and record timestamp."""
        from backend.models import MediaCheck, TokenBalance

        mock_service = MagicMock()
        mock_service.analyze_media_authenticity.return_value = {
            'verdict': 'Appears Authentic',
            'confidence': 92,
            'explanation': 'Authentic video stream.',
            'criteria': {},
            'claims': []
        }
        mocker.patch('backend.routes.media.get_gemini_service', return_value=mock_service)

        response = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://youtu.be/dQw4w9WgXcQ?t=45'
        })

        assert response.status_code == 200
        res = response.get_json()
        assert res['cost_incurred'] == 40
        assert res['new_balance'] == 60

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        assert bal.balance == 60

        check = db_session_fixture.query(MediaCheck).filter_by(user_id=test_user.id).order_by(MediaCheck.id.desc()).first()
        assert check.url == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=45s'
        assert check.embedding_json is None

        mock_service.validate_youtube_video.assert_called_once_with(
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        )
        mock_service.analyze_media_authenticity.assert_called_once_with(
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            input_type='url',
            mime_type='video/mp4',
            start_seconds=45,
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

    def test_analyze_media_authenticity_small_inline_video_uses_4_fps(self, app):
        """Small video payloads (<= 5 MB) must use MEDIA_RESOLUTION_HIGH, 4.0 FPS, and a 30s window."""
        from backend.services.gemini_service import GeminiService
        from google.genai import types

        mock_llm_json = json.dumps({
            "verdict": "AI Generated",
            "confidence": 85,
            "explanation": "Temporal text convergence and structural anomalies detected.",
            "criteria": {
                "physics": {"tag": "High Signal", "score": 80, "detail": "Structural topology anomaly."},
                "temporal": {"tag": "High Signal", "score": 78, "detail": "Text convergence across motion."}
            },
            "claims": []
        })

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client

            service = GeminiService()
            service.client = mock_client

            mock_resp = MagicMock()
            mock_resp.text = mock_llm_json
            mock_client.models.generate_content.return_value = mock_resp

            result = service.analyze_media_authenticity(
                b"video_payload_bytes", input_type='bytes', mime_type='video/mp4'
            )

            assert result["verdict"] == "AI Generated"
            assert result["confidence"] == 85

            mock_client.models.generate_content.assert_called_once()
            call_kwargs = mock_client.models.generate_content.call_args.kwargs
            assert call_kwargs["model"] == service.MODEL_MEDIA
            assert call_kwargs["model"] == "gemini-4-argon"
            assert call_kwargs["config"].media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
            sent_part = call_kwargs["contents"][0]
            assert sent_part.video_metadata.fps == 4.0
            assert sent_part.video_metadata.start_offset == "0s"
            assert sent_part.video_metadata.end_offset == "30s"

    def test_analyze_media_authenticity_file_size_fps_and_youtube_uri(self, app):
        """FPS follows file size: <= 5 MB keeps 4.0, > 5 MB uses 2.0, YouTube URI uses 2.0; all route to MODEL_MEDIA."""
        from backend.services.gemini_service import GeminiService

        mock_llm_json = json.dumps({
            "verdict": "Appears Authentic",
            "confidence": 90,
            "explanation": "Consistent motion.",
            "criteria": {},
            "claims": []
        })

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            service = GeminiService()
            service.client = mock_client

            mock_resp = MagicMock()
            mock_resp.text = mock_llm_json
            mock_client.models.generate_content.return_value = mock_resp

            # <= 5 MB -> 4.0 FPS
            service.analyze_media_authenticity(
                b"video_small", input_type='bytes', mime_type='video/mp4'
            )
            kw_small = mock_client.models.generate_content.call_args.kwargs
            assert kw_small["model"] == "gemini-4-argon"
            assert kw_small["contents"][0].video_metadata.fps == 4.0
            assert kw_small["contents"][0].video_metadata.end_offset == "30s"

            # > 5 MB (6 MB) -> 2.0 FPS
            service.analyze_media_authenticity(
                b"0" * (6 * 1024 * 1024), input_type='bytes', mime_type='video/mp4'
            )
            kw_med = mock_client.models.generate_content.call_args.kwargs
            assert kw_med["model"] == "gemini-4-argon"
            assert kw_med["contents"][0].video_metadata.fps == 2.0
            assert kw_med["contents"][0].video_metadata.end_offset == "30s"

            # YouTube URI -> 2.0 FPS + end_offset='30s'
            with patch.object(service, '_validate_url', return_value=True):
                service.analyze_media_authenticity(
                    'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                    input_type='url',
                    mime_type='video/mp4',
                )
            kw_yt = mock_client.models.generate_content.call_args.kwargs
            assert kw_yt["model"] == "gemini-4-argon"
            assert kw_yt["contents"][0].file_data.file_uri == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
            assert kw_yt["contents"][0].video_metadata.fps == 2.0
            assert kw_yt["contents"][0].video_metadata.start_offset == "0s"
            assert kw_yt["contents"][0].video_metadata.end_offset == "30s"

    def test_analyze_media_authenticity_uploads_large_bytes_via_files_api_and_cleans_up(self, app):
        """input_type='bytes' >14 MB must upload via Files API (BytesIO) and delete the file in finally."""
        from backend.services.gemini_service import GeminiService

        mock_llm_json = json.dumps({
            "verdict": "Appears Authentic",
            "confidence": 88,
            "explanation": "Clean capture.",
            "criteria": {},
            "claims": []
        })

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            service = GeminiService()
            service.client = mock_client

            uploaded = MagicMock()
            uploaded.name = 'files/large-remote-bytes-1'
            uploaded.uri = 'https://generativelanguage.googleapis.com/v1beta/files/large-remote-bytes-1'
            uploaded.mime_type = 'video/mp4'
            uploaded.state = MagicMock(name='ACTIVE')
            uploaded.state.name = 'ACTIVE'
            mock_client.files.upload.return_value = uploaded

            mock_resp = MagicMock()
            mock_resp.text = mock_llm_json
            mock_client.models.generate_content.return_value = mock_resp

            large_payload = b'0' * ((15 * 1024 * 1024) + 1024)
            res = service.analyze_media_authenticity(
                large_payload, input_type='bytes', mime_type='video/mp4'
            )

            assert res["verdict"] == "Appears Authentic"
            mock_client.files.upload.assert_called_once()
            upload_kwargs = mock_client.files.upload.call_args.kwargs
            assert isinstance(upload_kwargs['file'], io.BytesIO)
            sent_part = mock_client.models.generate_content.call_args.kwargs["contents"][0]
            assert sent_part.file_data.file_uri == uploaded.uri
            assert sent_part.video_metadata.fps == 2.0
            assert sent_part.video_metadata.end_offset == "30s"
            mock_client.files.delete.assert_called_once_with(name='files/large-remote-bytes-1')


def _make_wire_media_client(verdict: str = 'Appears Authentic', confidence: int = 91) -> MagicMock:
    """Create a wire-mocked genai.Client returning valid forensic JSON."""
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        'verdict': verdict,
        'confidence': confidence,
        'explanation': 'Valid physical and acoustic signals.',
        'criteria': {},
        'claims': [],
    })
    mock_client.models.generate_content.return_value = mock_resp
    mock_client.files.upload.side_effect = RuntimeError('force inline fallback for small fixture')
    return mock_client


class TestMediaFileSizeTieredBilling:
    """Wire-boundary tests for file-size tiered media billing, 30s window, URL parity, and multi-CP refunds."""

    @pytest.mark.parametrize(
        'mime_type,size_bytes,expected_cp',
        [
            ('video/mp4', 1 * 1024 * 1024, 15),
            ('video/mp4', 5 * 1024 * 1024, 15),
            ('video/mp4', 6 * 1024 * 1024, 25),
            ('video/mp4', 14 * 1024 * 1024, 25),
            ('video/mp4', 15 * 1024 * 1024, 40),
            ('video/mp4', None, 40),
            ('audio/mpeg', 10 * 1024 * 1024, 5),
            ('image/png', 10 * 1024 * 1024, 4),
        ],
    )
    def test_video_cp_tier_follows_file_size(self, mime_type, size_bytes, expected_cp):
        """Media CP must map cleanly from MIME type and file size thresholds."""
        from backend.routes.media import _calculate_media_cp_cost

        assert _calculate_media_cp_cost(mime_type, size_bytes) == expected_cp

    @pytest.mark.parametrize(
        'filename,mime_type,payload,expected_cost,expected_fps',
        [
            ('small.mp4', 'video/mp4', b'0' * (1 * 1024 * 1024), 15, 4.0),
            ('medium.mp4', 'video/mp4', b'0' * (6 * 1024 * 1024), 25, 2.0),
            ('clip.wav', 'audio/wav', b'0' * (1 * 1024 * 1024), 5, None),
        ],
        ids=['small-1mb-mp4', 'medium-6mb-mp4', 'audio-1mb-wav'],
    )
    def test_upload_tiered_billing_adaptive_fps_and_no_media_cache(
        self, auth_client, test_user, db_session_fixture, filename, mime_type, payload, expected_cost, expected_fps
    ):
        """Uploads must bill exact file-size tier CP, route to MODEL_MEDIA/MODEL with right FPS, and skip media caches."""
        from backend.models import MediaCheck, TokenBalance, User
        from backend.services import get_gemini_service

        # Seed a stale DB value to confirm runtime route no longer flushes/writes current_media_cache
        user_row = db_session_fixture.query(User).filter_by(id=test_user.id).one()
        user_row.current_media_cache = 'cachedContents/legacy-unused'
        db_session_fixture.commit()

        svc = get_gemini_service()
        mock_client = _make_wire_media_client()

        with patch.object(svc, 'client', mock_client):
            resp = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(payload), filename, mime_type)},
                content_type='multipart/form-data',
            )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['cost_incurred'] == expected_cost
        assert body['new_balance'] == 100 - expected_cost

        # Triple state: DB TokenBalance & MediaCheck + SDK call args
        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.balance == 100 - expected_cost
        check = db_session_fixture.query(MediaCheck).filter_by(user_id=test_user.id).order_by(MediaCheck.id.desc()).first()
        assert check is not None
        assert check.mime_type == mime_type

        mock_client.caches.delete.assert_not_called()
        mock_client.caches.create.assert_not_called()
        mock_client.files.upload.assert_not_called()
        mock_client.files.delete.assert_not_called()
        user_after = db_session_fixture.query(User).filter_by(id=test_user.id).one()
        assert user_after.current_media_cache == 'cachedContents/legacy-unused'

        from google.genai import types
        gen_kwargs = mock_client.models.generate_content.call_args.kwargs
        if mime_type.startswith('audio/'):
            assert gen_kwargs['model'] == 'gemini-3.8-flash'
            assert gen_kwargs['config'].media_resolution is None
        else:
            assert gen_kwargs['model'] == 'gemini-4-argon'
            assert gen_kwargs['config'].media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
        if expected_fps is not None:
            sent_part = gen_kwargs['contents'][0]
            assert sent_part.video_metadata.fps == expected_fps
            assert sent_part.video_metadata.start_offset == '0s'
            assert sent_part.video_metadata.end_offset == '30s'

    def test_direct_video_url_downloads_once_bills_file_size_tier_and_sets_window(
        self, auth_client, test_user, db_session_fixture
    ):
        """Direct remote video URLs download once via safe_http, bill the size tier, and set the 30s window."""
        from backend.models import MediaCheck, TokenBalance
        from backend.services import get_gemini_service

        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
        mp4_small = b'0' * (1 * 1024 * 1024)
        mp4_medium = b'0' * (6 * 1024 * 1024)

        svc = get_gemini_service()
        mock_client = _make_wire_media_client(verdict='AI Generated', confidence=89)

        def _fake_get_small(*args, **kwargs):
            r = MagicMock(status_code=200, headers={'Content-Length': str(len(mp4_small))})
            r.iter_content.return_value = [mp4_small]
            return r

        with patch.object(svc, 'client', mock_client), \
             patch('backend.services.safe_http.socket.getaddrinfo', return_value=public_dns), \
             patch('backend.services.safe_http.requests.get', side_effect=_fake_get_small) as mock_get:
            resp = auth_client.post('/api/media/analyze-url', json={
                'url': 'https://cdn.example.com/clip_small.mp4'
            })

        assert resp.status_code == 200
        body = resp.get_json()
        assert body['cost_incurred'] == 15
        assert body['new_balance'] == 85
        assert mock_get.call_count == 1

        gen_kwargs = mock_client.models.generate_content.call_args.kwargs
        assert gen_kwargs['model'] == 'gemini-4-argon'
        assert gen_kwargs['contents'][0].video_metadata.fps == 4.0
        assert gen_kwargs['contents'][0].video_metadata.start_offset == '0s'
        assert gen_kwargs['contents'][0].video_metadata.end_offset == '30s'

        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.balance == 85
        saved = db_session_fixture.query(MediaCheck).filter_by(user_id=test_user.id).one()
        assert saved.url == 'https://cdn.example.com/clip_small.mp4'

        # 6 MB direct URL -> 25 CP tier at 2.0 FPS
        def _fake_get_medium(*args, **kwargs):
            r = MagicMock(status_code=200, headers={'Content-Length': str(len(mp4_medium))})
            r.iter_content.return_value = [mp4_medium]
            return r

        mock_client.models.generate_content.reset_mock()
        with patch.object(svc, 'client', mock_client), \
             patch('backend.services.safe_http.socket.getaddrinfo', return_value=public_dns), \
             patch('backend.services.safe_http.requests.get', side_effect=_fake_get_medium):
            resp_med = auth_client.post('/api/media/analyze-url', json={
                'url': 'https://cdn.example.com/clip_medium.mp4'
            })

        assert resp_med.status_code == 200
        assert resp_med.get_json()['cost_incurred'] == 25
        med_part = mock_client.models.generate_content.call_args.kwargs['contents'][0]
        assert med_part.video_metadata.fps == 2.0
        assert med_part.video_metadata.start_offset == '0s'
        assert med_part.video_metadata.end_offset == '30s'
        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 60

    def test_direct_video_url_checks_minimum_balance_before_download(
        self, auth_client, test_user, db_session_fixture
    ):
        """User below 15 CP minimum must be rejected with 403 before downloading remote video bytes."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 10
        db_session_fixture.commit()

        with patch('backend.services.safe_http.requests.get') as mock_get:
            resp = auth_client.post('/api/media/analyze-url', json={
                'url': 'https://cdn.example.com/clip.mp4'
            })

        assert resp.status_code == 403
        assert 'Insufficient tokens' in resp.get_json()['error']
        mock_get.assert_not_called()

    def test_upload_refunds_multi_cp_charge_on_gemini_failure(
        self, auth_client, test_user, db_session_fixture
    ):
        """If 25 CP video analysis fails in Gemini, all 25 CP are refunded and raw errors are not echoed."""
        from backend.models import TokenBalance
        from backend.services import get_gemini_service

        svc = get_gemini_service()
        mock_client = _make_wire_media_client()
        mock_client.models.generate_content.side_effect = RuntimeError('Argon unavailable')

        with patch.object(svc, 'client', mock_client):
            resp = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(b'0' * (6 * 1024 * 1024)), 'medium.mp4', 'video/mp4')},
                content_type='multipart/form-data',
            )

        assert resp.status_code == 400
        error_msg = resp.get_json()['error']
        assert error_msg == 'Failed to process uploaded media. Please try again or use a different file.'
        assert 'Argon unavailable' not in error_msg
        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.balance == 100

    def test_media_js_surfaces_cost_incurred_in_completion_toast(self):
        """frontend/js/media.js must surface response.cost_incurred in the completion toast."""
        js_path = pathlib.Path(__file__).resolve().parents[1] / 'frontend' / 'js' / 'media.js'
        js_source = js_path.read_text(encoding='utf-8')
        assert 'response.cost_incurred' in js_source
        assert 'CP' in js_source

    def test_extract_youtube_start_seconds_parses_query_formats(self):
        """extract_youtube_start_seconds must parse ?t=45, ?t=1m30s, ?start=120, and default to 0."""
        from backend.services.gemini.media import GeminiServiceMedia

        assert GeminiServiceMedia.extract_youtube_start_seconds('https://youtu.be/dQw4w9WgXcQ?t=45') == 45
        assert GeminiServiceMedia.extract_youtube_start_seconds('https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1m30s') == 90
        assert GeminiServiceMedia.extract_youtube_start_seconds('https://www.youtube.com/watch?v=dQw4w9WgXcQ&start=120') == 120
        assert GeminiServiceMedia.extract_youtube_start_seconds('https://www.youtube.com/watch?v=dQw4w9WgXcQ') == 0

    def test_attach_video_sampling_uses_start_seconds_window(self):
        """_attach_video_sampling must shift start_offset and end_offset by start_seconds."""
        from google.genai import types
        from backend.services.gemini.media import GeminiServiceMedia

        part = types.Part.from_uri(file_uri='https://www.youtube.com/watch?v=dQw4w9WgXcQ', mime_type='video/mp4')
        sampled = GeminiServiceMedia._attach_video_sampling(part, 'video/mp4', fps=2.0, start_seconds=45)

        assert sampled.video_metadata.fps == 2.0
        assert sampled.video_metadata.start_offset == '45s'
        assert sampled.video_metadata.end_offset == '75s'

    def test_youtube_oembed_404_rejects_with_400_and_zero_deduction(
        self, auth_client, test_user, db_session_fixture
    ):
        """Private/deleted YouTube video returning oEmbed 404 must fail with HTTP 400 and 0 CP deducted."""
        from backend.models import MediaCheck, TokenBalance
        from backend.services import get_gemini_service

        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('142.250.185.78', 443))]
        svc = get_gemini_service()
        mock_client = _make_wire_media_client()

        with patch.object(svc, 'client', mock_client), \
             patch('backend.services.safe_http.socket.getaddrinfo', return_value=public_dns), \
             patch('backend.services.safe_http.requests.get', return_value=MagicMock(status_code=404, headers={})) as mock_oembed:
            resp = auth_client.post('/api/media/analyze-url', json={
                'url': 'https://www.youtube.com/watch?v=privatevid1'
            })

        assert resp.status_code == 400
        assert 'unavailable' in resp.get_json()['error'].lower()
        mock_oembed.assert_called_once()
        mock_client.models.generate_content.assert_not_called()

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 100
        assert db_session_fixture.query(MediaCheck).filter_by(user_id=test_user.id).count() == 0

    def test_video_ogg_rejected_while_audio_ogg_opus_succeeds(
        self, auth_client, test_user, db_session_fixture
    ):
        """Obsolete video/ogg (Theora) must be rejected with 400 while audio/ogg (Opus) passes."""
        from backend.models import TokenBalance
        from backend.services import get_gemini_service

        svc = get_gemini_service()
        mock_client = _make_wire_media_client()

        with patch.object(svc, 'client', mock_client):
            resp_vid = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(b'OggS\x00\x02' + (b'\x00' * 40)), 'clip.ogv', 'video/ogg')},
                content_type='multipart/form-data',
            )
            assert resp_vid.status_code == 400
            assert 'Unsupported media format' in resp_vid.get_json()['error']
            mock_client.models.generate_content.assert_not_called()

            resp_aud = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(b'OggS\x00\x02' + (b'\x00' * 40)), 'voice.ogg', 'audio/ogg')},
                content_type='multipart/form-data',
            )
            assert resp_aud.status_code == 200
            assert resp_aud.get_json()['cost_incurred'] == 5

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 95

    def test_analyze_media_authenticity_truncates_claims_to_20_and_sets_schema(self, app):
        """analyze_media_authenticity must pass response_schema and cap extracted claims at 20."""
        from backend.services.gemini_service import GeminiService

        many_claims = [
            {"timestamp": f"00:{i:02d}", "timestamp_seconds": i, "claim": f"Claim {i}"}
            for i in range(25)
        ]
        mock_llm_json = json.dumps({
            "verdict": "Appears Authentic",
            "confidence": 90,
            "explanation": "Consistent audio and frames.",
            "criteria": {},
            "claims": many_claims,
        })

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            service = GeminiService()
            service.client = mock_client

            mock_resp = MagicMock()
            mock_resp.text = mock_llm_json
            mock_client.models.generate_content.return_value = mock_resp

            result = service.analyze_media_authenticity(
                b"video_payload", input_type='bytes', mime_type='video/mp4'
            )

        assert len(result['claims']) == 20
        assert result['claims'][0]['claim'] == 'Claim 0'
        assert result['claims'][-1]['claim'] == 'Claim 19'
        cfg = mock_client.models.generate_content.call_args.kwargs['config']
        assert cfg.response_schema is not None
        assert cfg.response_schema.properties['verdict'].enum == [
            'AI Generated',
            'Likely Manipulated',
            'Appears Authentic',
        ]

    def test_direct_image_url_downloads_once_and_embedding_pipeline_removed(
        self, auth_client, test_user, db_session_fixture
    ):
        """Direct image URLs must download once via safe_http and never call deleted embedding pipeline."""
        from backend.models import MediaCheck, TokenBalance
        from backend.services import get_gemini_service
        from backend.services.gemini.client import GeminiServiceClient
        from backend.services.gemini.media import GeminiServiceMedia

        assert not hasattr(GeminiServiceMedia, 'get_media_embedding')
        assert not hasattr(GeminiServiceClient, 'MODEL_EMBEDDING')

        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
        png_bytes = b'\x89PNG\r\n\x1a\n'
        svc = get_gemini_service()
        mock_client = _make_wire_media_client()

        def _fake_get(*args, **kwargs):
            r = MagicMock(status_code=200, headers={'Content-Length': str(len(png_bytes))})
            r.iter_content.return_value = [png_bytes]
            return r

        with patch.object(svc, 'client', mock_client), \
             patch('backend.services.safe_http.socket.getaddrinfo', return_value=public_dns), \
             patch('backend.services.safe_http.requests.head', return_value=MagicMock(status_code=200, headers={})), \
             patch('backend.services.safe_http.requests.get', side_effect=_fake_get) as mock_get:
            resp = auth_client.post('/api/media/analyze-url', json={
                'url': 'https://cdn.example.com/photo.png'
            })

        assert resp.status_code == 200
        body = resp.get_json()
        assert body['cost_incurred'] == 4
        assert body['new_balance'] == 96
        assert mock_get.call_count == 1
        mock_client.models.embed_content.assert_not_called()

        prompt_sent = mock_client.models.generate_content.call_args.kwargs['contents'][1]
        assert '"tag": "N/A"' in prompt_sent
        assert "set 'claims' to []" in prompt_sent

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 96
        saved = db_session_fixture.query(MediaCheck).filter_by(user_id=test_user.id).one()
        assert saved.embedding_json is None

    def test_analyze_media_retries_once_on_transient_429_or_503(self, app):
        """analyze_media_authenticity must retry once on transient 429/503 capacity errors."""
        from backend.services.gemini_service import GeminiService

        mock_llm_json = json.dumps({
            "verdict": "Appears Authentic",
            "confidence": 91,
            "explanation": "Clean capture.",
            "criteria": {},
            "claims": [],
        })

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class, \
             patch('backend.services.gemini.media.time.sleep') as mock_sleep:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            service = GeminiService()
            service.client = mock_client

            mock_resp = MagicMock()
            mock_resp.text = mock_llm_json
            mock_client.models.generate_content.side_effect = [
                RuntimeError('503 UNAVAILABLE'),
                mock_resp,
            ]

            result = service.analyze_media_authenticity(
                b'\x89PNG\r\n\x1a\n', input_type='bytes', mime_type='image/png'
            )

        assert result['verdict'] == 'Appears Authentic'
        assert mock_client.models.generate_content.call_count == 2
        mock_sleep.assert_called_once_with(1)

    def test_frontend_includes_cost_notice_modal_and_youtube_window_hint(self):
        """Frontend HTML/JS must include #ma-cost-modal acknowledgement, #ma-yt-window-hint, and N/A tag support."""
        frontend_dir = pathlib.Path(__file__).resolve().parents[1] / 'frontend'
        html_src = (frontend_dir / 'index.html').read_text(encoding='utf-8')
        js_src = (frontend_dir / 'js' / 'media.js').read_text(encoding='utf-8')

        assert 'id="ma-cost-modal"' in html_src
        assert 'id="ma-cost-ack-btn"' in html_src
        assert 'id="ma-yt-window-hint"' in html_src
        assert 'authenix_media_cost_ack' in js_src
        assert "tagLower === 'n/a'" in js_src

    def test_analyze_media_routes_small_file_inline_and_large_file_via_files_api(self, app, tmp_path):
        """Files <= 14 MB go straight inline (no files.upload); files > 14 MB use Files API + cleanup."""
        from backend.services.gemini_service import GeminiService

        small_file = tmp_path / 'small.jpg'
        small_file.write_bytes(b'\xff\xd8\xff\xe0small-image')

        large_file = tmp_path / 'large.mp4'
        large_file.write_bytes(b'\x00' * ((14 * 1024 * 1024) + 1024))

        mock_llm_json = json.dumps({
            "verdict": "Appears Authentic",
            "confidence": 92,
            "explanation": "Clean capture.",
            "criteria": {},
            "claims": [],
        })

        with patch('backend.services.gemini.client.genai.Client') as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            service = GeminiService()
            service.client = mock_client

            mock_resp = MagicMock()
            mock_resp.text = mock_llm_json
            mock_client.models.generate_content.return_value = mock_resp

            # 1. Small file (<= 14 MB): inline only, zero Files API calls
            res_small = service.analyze_media_authenticity(
                str(small_file), input_type='file', mime_type='image/jpeg'
            )
            assert res_small['verdict'] == 'Appears Authentic'
            mock_client.files.upload.assert_not_called()
            mock_client.files.delete.assert_not_called()

            # 2. Large file (> 14 MB): Files API upload + delete
            uploaded = MagicMock()
            uploaded.name = 'files/large-upload-1'
            uploaded.uri = 'https://generativelanguage.googleapis.com/v1beta/files/large-upload-1'
            uploaded.mime_type = 'video/mp4'
            uploaded.state = MagicMock()
            uploaded.state.name = 'ACTIVE'
            mock_client.files.upload.return_value = uploaded

            res_large = service.analyze_media_authenticity(
                str(large_file), input_type='file', mime_type='video/mp4'
            )
            assert res_large['verdict'] == 'Appears Authentic'
            mock_client.files.upload.assert_called_once()
            mock_client.files.delete.assert_called_once_with(name='files/large-upload-1')

    def test_frontend_api_does_not_retry_429_and_pitchdeck_batch_aligned_to_8(self):
        """api.js must not retry 429 rate limits (retries=1 on 503 only) and pitchdeck.js batches by 8."""
        js_dir = pathlib.Path(__file__).resolve().parents[1] / 'frontend' / 'js'
        api_js = (js_dir / 'api.js').read_text(encoding='utf-8')
        pitchdeck_js = (js_dir / 'pitchdeck.js').read_text(encoding='utf-8')

        assert 'retries = 1' in api_js
        assert 'error.status === 429' not in api_js
        assert 'const BATCH_SIZE = 8;' in pitchdeck_js

    def test_analyze_upload_insufficient_balance_fails_before_saving_temp_file(
        self, auth_client, test_user, db_session_fixture
    ):
        """Underfunded users must be rejected with 403 before writing uploaded media to disk."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 5  # Less than Config.MEDIA_CP_VIDEO_SHORT (15 CP)
        db_session_fixture.commit()

        with patch('backend.routes.media.tempfile.mkstemp') as mock_mkstemp:
            resp = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(b'\x00' * 1024), 'clip.mp4', 'video/mp4')},
                content_type='multipart/form-data',
            )

        assert resp.status_code == 403
        assert 'Insufficient tokens' in resp.get_json()['error']
        mock_mkstemp.assert_not_called()
        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 5

    def test_render_claims_desk_escapes_timestamp_and_coerces_seconds(self):
        """renderClaimsDesk must HTML-escape timestamp and coerce seconds to a finite non-negative integer."""
        import re

        js_path = pathlib.Path(__file__).resolve().parents[1] / 'frontend' / 'js' / 'media.js'
        js_source = js_path.read_text(encoding='utf-8')

        desk_match = re.search(
            r'renderClaimsDesk\(claims\)\s*\{(?P<body>.*?)^\s*seekToTimestamp\(seconds\)\s*\{',
            js_source,
            re.DOTALL | re.MULTILINE,
        )
        assert desk_match is not None
        desk_body = desk_match.group('body')

        card_match = re.search(r'card\.innerHTML\s*=\s*`(?P<html>.*?)`;', desk_body, re.DOTALL)
        assert card_match is not None
        card_html = card_match.group('html')

        assert 'const escapedTimestamp = this._escapeHtml(timestamp);' in desk_body
        assert 'title="Click to seek player to ${escapedTimestamp}"' in card_html
        assert '▶ ${escapedTimestamp}' in card_html
        assert '${timestamp}' not in card_html
        assert 'this._escapeHtml(item.claim)' in card_html
        assert 'Number.isFinite(' in desk_body
        assert 'Math.max(0, Math.floor(' in desk_body
        assert 'data-seconds="${seconds}"' in card_html

        seek_match = re.search(
            r'seekToTimestamp\(seconds\)\s*\{(?P<body>.*?)^\s*async verifyAllClaims\(\)',
            js_source,
            re.DOTALL | re.MULTILINE,
        )
        assert seek_match is not None
        assert '!Number.isFinite(seconds)' in seek_match.group('body')
        assert 'seconds < 0' in seek_match.group('body')







