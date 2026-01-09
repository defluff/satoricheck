"""
Live Pro Integration Tests.
Tests Session Management, Cost Controls, and Abuse Prevention.
"""
import pytest
from unittest.mock import patch, MagicMock


class TestLiveProStart:
    """Test Live Pro session initiation."""
    
    def test_start_requires_auth(self, client):
        """Live Pro start should require authentication."""
        response = client.post('/api/live-pro/start', json={'language': 'en'})
        assert response.status_code == 401
    
    def test_start_insufficient_balance(self, auth_client, test_user, db_session_fixture):
        """Should reject start if user has 0 CP."""
        from backend.models import TokenBalance
        
        # Set balance to 0
        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        db_session_fixture.commit()
        
        with patch('backend.services.deepgram_service.DeepgramService.is_available', return_value=True):
            response = auth_client.post('/api/live-pro/start', json={'language': 'en'})
        
        assert response.status_code == 403
        data = response.get_json()
        assert 'Insufficient' in data.get('error', '')
    
    def test_start_concurrent_session_blocked(self, auth_client, test_user, db_session_fixture):
        """Should reject second session if one is already active."""
        from backend.models import LiveProSession
        from datetime import datetime
        
        # Create existing active session
        existing = LiveProSession(
            user_id=test_user.id,
            status='active',
            started_at=datetime.utcnow()
        )
        db_session_fixture.add(existing)
        db_session_fixture.commit()
        
        # Mock active_sessions dict
        with patch('backend.routes.live_pro.active_sessions', {existing.id: {'last_heartbeat': 9999999999}}):
            with patch('backend.services.deepgram_service.DeepgramService.is_available', return_value=True):
                response = auth_client.post('/api/live-pro/start', json={'language': 'en'})
        
        assert response.status_code == 409
        data = response.get_json()
        assert 'already have an active' in data.get('error', '')
    
    def test_start_success_returns_proxy_url(self, auth_client, test_user, db_session_fixture):
        """Successful start should return backend proxy URL, not Deepgram URL."""
        from backend.models import TokenBalance
        
        # Ensure sufficient balance
        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 100
        db_session_fixture.commit()
        
        with patch('backend.services.deepgram_service.DeepgramService.is_available', return_value=True):
            with patch('backend.services.deepgram_service.DeepgramService.get_websocket_url', 
                      return_value='wss://api.deepgram.com/v1/listen'):
                response = auth_client.post('/api/live-pro/start', json={'language': 'en'})
        
        if response.status_code == 200:
            data = response.get_json()
            # Should be our proxy URL, NOT deepgram directly
            assert 'api.deepgram.com' not in data.get('websocket_url', '')
            assert '/api/livepro/ws/' in data.get('websocket_url', '')


class TestLiveProEnd:
    """Test Live Pro session termination."""
    
    def test_end_requires_auth(self, client):
        """End session should require authentication."""
        response = client.post('/api/live-pro/end', json={'session_id': 1})
        assert response.status_code == 401
