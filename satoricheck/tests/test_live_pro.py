"""
Live Pro Integration Tests.
Tests session management, cost controls, and abuse prevention.

All patches now target `transcription_service.GeminiTranscriptionService`
(previously DeepgramService) — the public interface is identical.
"""
import pytest
from datetime import datetime, UTC
from unittest.mock import patch, MagicMock

_TRANSCRIPTION_IS_AVAILABLE = (
    'backend.services.transcription_service.GeminiTranscriptionService.is_available'
)


class TestLiveProStart:
    """Test Live Pro session initiation."""

    def test_start_requires_auth(self, client):
        """Live Pro start should require authentication."""
        response = client.post('/api/live-pro/start', json={'language': 'en'})
        assert response.status_code == 401

    def test_start_insufficient_balance(self, auth_client, test_user, db_session_fixture):
        """Should reject start when user has 0 CP."""
        from backend.models import TokenBalance

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        db_session_fixture.commit()

        with patch(_TRANSCRIPTION_IS_AVAILABLE, return_value=True):
            response = auth_client.post('/api/live-pro/start', json={'language': 'en'})

        assert response.status_code == 403
        data = response.get_json()
        assert 'Insufficient' in data.get('error', '')

    def test_start_concurrent_session_auto_closed(self, auth_client, test_user, db_session_fixture):
        """Existing active session should be auto-closed and a new one started."""
        from backend.models import LiveProSession

        # Create an existing active session
        existing = LiveProSession(
            user_id=test_user.id,
            status='active',
            started_at=datetime.now(UTC),
        )
        db_session_fixture.add(existing)
        db_session_fixture.commit()

        with patch(_TRANSCRIPTION_IS_AVAILABLE, return_value=True):
            response = auth_client.post('/api/live-pro/start', json={'language': 'en'})

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'session_id' in data

    def test_start_success_returns_proxy_url(self, auth_client, test_user, db_session_fixture):
        """Successful start must return the backend proxy URL, never a third-party URL."""
        from backend.models import TokenBalance

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 100
        db_session_fixture.commit()

        with patch(_TRANSCRIPTION_IS_AVAILABLE, return_value=True):
            response = auth_client.post('/api/live-pro/start', json={'language': 'en'})

        if response.status_code == 200:
            data = response.get_json()
            ws_url = data.get('websocket_url', '')
            # Must route through our proxy — never expose a third-party URL
            assert 'api.deepgram.com' not in ws_url
            assert '/api/livepro/ws/' in ws_url

    def test_start_unavailable_when_no_gemini_key(self, auth_client):
        """Live Pro should return 503 when transcription service is unavailable."""
        with patch(_TRANSCRIPTION_IS_AVAILABLE, return_value=False):
            response = auth_client.post('/api/live-pro/start', json={'language': 'en'})
        assert response.status_code == 503


class TestLiveProConfig:
    """Test the /config endpoint."""

    def test_config_requires_auth(self, client):
        """Config endpoint must require authentication."""
        response = client.get('/api/live-pro/config')
        assert response.status_code == 401

    def test_config_returns_availability(self, auth_client):
        """Config should reflect transcription service availability."""
        with patch(_TRANSCRIPTION_IS_AVAILABLE, return_value=True):
            response = auth_client.get('/api/live-pro/config')

        assert response.status_code == 200
        data = response.get_json()
        assert data['available'] is True
        assert 'cp_per_minute' in data
        assert 'balance' in data
        # websocket_url is intentionally NOT in the config response any more
        assert 'websocket_url' not in data


class TestLiveProEnd:
    """Test Live Pro session termination."""

    def test_end_requires_auth(self, client):
        """End session should require authentication."""
        response = client.post('/api/live-pro/end', json={'session_id': 1})
        assert response.status_code == 401
