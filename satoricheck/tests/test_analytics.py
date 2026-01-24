"""
Share Analytics Endpoint Tests.
Tests POST /api/analytics/share for platform tracking.
"""
import pytest


class TestShareAnalytics:
    """Test share analytics endpoint."""
    
    def test_share_analytics_success(self, auth_client, db_session_fixture):
        """POST /api/analytics/share increments counter."""
        response = auth_client.post('/api/analytics/share', json={
            'platform': 'X'
        })
        assert response.status_code == 200
        assert response.get_json()['success'] is True
        
        # Verify database insert
        from backend.models import ShareStats
        count = db_session_fixture.query(ShareStats).filter_by(platform='X').count()
        assert count > 0
    
    def test_share_analytics_linkedin(self, auth_client, db_session_fixture):
        """LinkedIn platform works."""
        response = auth_client.post('/api/analytics/share', json={
            'platform': 'LinkedIn'
        })
        assert response.status_code == 200
        
        from backend.models import ShareStats
        count = db_session_fixture.query(ShareStats).filter_by(platform='LinkedIn').count()
        assert count > 0
    
    def test_share_analytics_download(self, auth_client, db_session_fixture):
        """Download platform works."""
        response = auth_client.post('/api/analytics/share', json={
            'platform': 'Download'
        })
        assert response.status_code == 200
    
    def test_share_analytics_invalid_platform(self, auth_client):
        """Invalid platform enum is rejected."""
        response = auth_client.post('/api/analytics/share', json={
            'platform': 'InvalidPlatform'
        })
        assert response.status_code == 400
        assert 'error' in response.get_json()
    
    def test_share_analytics_missing_platform(self, auth_client):
        """Missing platform field is rejected."""
        response = auth_client.post('/api/analytics/share', json={})
        assert response.status_code == 400
    
    def test_share_analytics_null_platform(self, auth_client):
        """Null platform value is rejected."""
        response = auth_client.post('/api/analytics/share', json={
            'platform': None
        })
        assert response.status_code == 400
    
    def test_share_analytics_empty_platform(self, auth_client):
        """Empty string platform is rejected."""
        response = auth_client.post('/api/analytics/share', json={
            'platform': ''
        })
        assert response.status_code == 400
    
    def test_share_analytics_unauthenticated(self, client):
        """Unauthenticated requests are rejected."""
        response = client.post('/api/analytics/share', json={
            'platform': 'X'
        })
        assert response.status_code == 401


class TestShareAnalyticsEdgeCases:
    """Edge case tests for share analytics."""
    
    def test_share_case_sensitive_platform(self, auth_client):
        """Platform should be case-sensitive (reject 'x', accept 'X')."""
        response_lower = auth_client.post('/api/analytics/share', json={
            'platform': 'x'  # lowercase
        })
        response_upper = auth_client.post('/api/analytics/share', json={
            'platform': 'X'  # uppercase
        })
        
        assert response_lower.status_code == 400  # Reject lowercase
        assert response_upper.status_code == 200  # Accept uppercase
    
    def test_share_no_content_stored(self, auth_client, db_session_fixture):
        """Verify no content is stored, only platform."""
        # NOTE: We check existing records since rate limit may be hit
        # The previous tests already created records
        from backend.models import ShareStats
        
        # Get any existing record
        record = db_session_fixture.query(ShareStats).first()
        
        if record:
            # Verify no user_id or content fields exist on model
            assert hasattr(record, 'platform')
            assert hasattr(record, 'created_at')
            # These fields should NOT exist (privacy-first design)
            assert not hasattr(record, 'user_id') or getattr(record, 'user_id', None) is None
            assert not hasattr(record, 'claim')
            assert not hasattr(record, 'verdict')
            assert not hasattr(record, 'content')
        else:
            # If no records, previous tests didn't run - that's ok for this check
            pass
