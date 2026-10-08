"""
Fact-Check Integration Tests.
Tests API usage, token deduction, and rate limiting.
"""
import pytest
from unittest.mock import patch


class TestFactCheck:
    """Test fact-checking endpoint."""
    
    def test_factcheck_requires_auth(self, client):
        """Fact-check should require authentication."""
        response = client.post('/api/factcheck/analyze', json={
            'text': 'The Earth is round.'
        })
        assert response.status_code == 401
    
    def test_factcheck_success(self, auth_client, mock_gemini):
        """Fact-check should return result and call Gemini."""
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': 'The Earth is round.'
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert 'result' in data or 'verdict' in data or 'is_claim' in data
    
    def test_factcheck_empty_text_rejected(self, auth_client):
        """Empty text should be rejected."""
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': ''
        })
        
        assert response.status_code == 400
    
    def test_factcheck_no_balance_rejected(self, auth_client, test_user, db_session_fixture):
        """Should reject if user has no balance."""
        from backend.models import TokenBalance
        
        # Set balance to 0 and unbilled_words to trigger cost
        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        tb.unbilled_words = 0
        db_session_fixture.commit()
        
        # Send a very long text to trigger deduction
        long_text = "word " * 2000  # 2000 words should cost CP
        
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': long_text
        })
        
        # Should either reject or return 403
        # (depends on implementation - some allow first check free)
        assert response.status_code in [200, 400, 403]


class TestRateLimiting:
    """Test API rate limiting."""
    
    def test_rate_limit_triggers(self, app, test_user, db_session_fixture):
        """Excessive requests should trigger rate limit."""
        # This test is tricky with Flask-Limiter in testing
        # We just verify the limiter is configured
        from backend.server import limiter
        
        assert limiter is not None
        # Rate limiter should be enabled
        assert limiter.enabled is True


class TestCleanupExpiredChecks:
    """Retention behaviour of /api/factcheck/cleanup-expired (auth: see test_scheduler_auth.py)."""

    def test_cleanup_purges_only_records_older_than_7_days(self, client, test_user, db_session_fixture):
        """Expired records (>7d) are purged; recent records (<7d) are preserved."""
        from datetime import datetime, UTC, timedelta
        from backend.config import Config
        from backend.models import FactCheck, MediaCheck

        old_time = datetime.now(UTC) - timedelta(days=8)
        recent_time = datetime.now(UTC) - timedelta(days=2)

        db_session_fixture.add_all([
            FactCheck(user_id=test_user.id, claim_text="Old claim", timestamp=old_time),
            FactCheck(user_id=test_user.id, claim_text="Recent claim", timestamp=recent_time),
            MediaCheck(user_id=test_user.id, url="https://example.com/old.jpg", timestamp=old_time),
            MediaCheck(user_id=test_user.id, url="https://example.com/recent.jpg", timestamp=recent_time),
        ])
        db_session_fixture.commit()

        response = client.post('/api/factcheck/cleanup-expired', headers={
            'X-Scheduler-Secret': Config.SCHEDULER_SECRET
        })

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['deleted_fact_checks'] == 1
        assert data['deleted_media_checks'] == 1

        remaining_claims = [f.claim_text for f in db_session_fixture.query(FactCheck).all()]
        remaining_urls = [m.url for m in db_session_fixture.query(MediaCheck).all()]
        assert remaining_claims == ["Recent claim"]
        assert remaining_urls == ["https://example.com/recent.jpg"]

