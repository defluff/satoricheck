"""
Fact-Check Integration Tests.
Tests API usage, token deduction, and rate limiting.
"""


class TestFactCheck:
    """Test fact-checking endpoint."""
    
    def test_factcheck_requires_auth(self, client):
        """Fact-check should require authentication."""
        response = client.post('/api/factcheck/analyze', json={
            'text': 'The Earth is round.'
        })
        assert response.status_code == 401
    
    def test_factcheck_success(self, auth_client, test_user, db_session_fixture, mock_gemini):
        """Fact-check should return structured verdict payload and persist FactCheck record."""
        from backend.models import FactCheck

        response = auth_client.post('/api/factcheck/analyze', json={
            'text': 'The Earth is round.'
        })
        
        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True

        result = data['result']
        assert result['is_claim'] is True
        assert result['verdict'] == 'TRUE'
        assert result['explanation'] == 'Test explanation'
        assert result['sources'] == ['https://example.com']
        assert result['source_reliability'] == 'HIGH'
        assert result['word_count'] == 4

        saved = db_session_fixture.query(FactCheck).filter_by(user_id=test_user.id).one()
        assert result['id'] == saved.id
        assert saved.claim_text == 'The Earth is round.'
        assert saved.verdict == 'TRUE'
    
    def test_factcheck_empty_text_rejected(self, auth_client):
        """Empty text should be rejected."""
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': ''
        })
        
        assert response.status_code == 400
    
    def test_factcheck_no_balance_rejected(self, auth_client, test_user, db_session_fixture, mocker):
        """User with 0 CP must be rejected with 403 before calling Gemini."""
        from backend.models import FactCheck, TokenBalance
        
        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        tb.unbilled_words = 0
        db_session_fixture.commit()
        
        mock_analyze = mocker.patch('backend.services.gemini_service.GeminiService.analyze_claim')
        long_text = "word " * 2000  # 2000 words costs 1 CP
        
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': long_text
        })
        
        assert response.status_code == 403
        assert 'Insufficient tokens' in response.get_json()['error']
        mock_analyze.assert_not_called()
        assert db_session_fixture.query(FactCheck).filter_by(user_id=test_user.id).count() == 0


class TestRateLimiting:
    """Test API rate limiting."""
    
    def test_rate_limit_triggers(self, auth_client, mock_gemini):
        """Exceeding 30 requests/minute on /api/factcheck/analyze must return 429."""
        for _ in range(30):
            ok_resp = auth_client.post('/api/factcheck/analyze', json={
                'text': 'The Earth is round.'
            })
            assert ok_resp.status_code == 200

        throttled_resp = auth_client.post('/api/factcheck/analyze', json={
            'text': 'The Earth is round.'
        })
        assert throttled_resp.status_code == 429
        assert 'Too many requests' in throttled_resp.get_json()['error']


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

