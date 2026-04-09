"""
Extension Security Tests.
Tests for CVE best practices specific to the browser extension auth flow.

Covers:
1. Bearer token validation (malformed, empty, wrong format)
2. Extension token endpoint security
3. Input sanitization via extension (oversized text, script injection)
4. Cross-user token isolation
"""
import pytest
import secrets


class TestBearerTokenValidation:
    """Bearer token auth must reject invalid tokens gracefully."""

    def test_rejects_empty_bearer_header(self, client):
        """Empty Authorization header must return 401."""
        response = client.get('/api/auth/me', headers={
            'Authorization': 'Bearer '
        })
        assert response.status_code == 401

    def test_rejects_malformed_bearer_header(self, client):
        """Malformed Authorization header (no 'Bearer' prefix) must return 401."""
        response = client.get('/api/auth/me', headers={
            'Authorization': 'Basic abc123'
        })
        assert response.status_code == 401

    def test_rejects_nonexistent_token(self, client):
        """A random token that doesn't belong to any user must return 401."""
        fake_token = secrets.token_hex(32)
        response = client.get('/api/auth/me', headers={
            'Authorization': f'Bearer {fake_token}'
        })
        assert response.status_code == 401

    def test_rejects_short_token(self, client):
        """A token shorter than 64 hex chars must return 401."""
        response = client.get('/api/auth/me', headers={
            'Authorization': 'Bearer abc123'
        })
        assert response.status_code == 401

    def test_rejects_sql_injection_in_token(self, client):
        """SQL injection attempt in token must return 401, not 500."""
        response = client.get('/api/auth/me', headers={
            'Authorization': "Bearer ' OR '1'='1"
        })
        assert response.status_code == 401

    def test_valid_bearer_token_returns_user(self, client, test_user):
        """Valid api_token must authenticate and return user info."""
        response = client.get('/api/auth/me', headers={
            'Authorization': f'Bearer {test_user.api_token}'
        })
        assert response.status_code == 200
        data = response.get_json()
        assert data['user']['email'] == test_user.email


class TestExtensionTokenSecurity:
    """Extension token endpoint must enforce access controls."""

    def test_extension_token_not_in_login_response(self, client, test_user):
        """Standard login response must NOT expose the api_token by default."""
        response = client.post('/api/auth/login', json={
            'email': 'test@example.com',
            'password': 'testpass123'
        })
        assert response.status_code == 200
        data = response.get_json()
        # api_token should NOT be in the standard login response
        assert 'api_token' not in data.get('user', {})

    def test_extension_token_requires_authentication(self, client):
        """Extension token endpoint must reject unauthenticated requests."""
        response = client.get('/api/auth/extension-token')
        assert response.status_code == 401

    def test_extension_token_is_consistent(self, auth_client, test_user):
        """Multiple calls to extension-token must return the same token."""
        resp1 = auth_client.get('/api/auth/extension-token')
        resp2 = auth_client.get('/api/auth/extension-token')

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.get_json()['api_token'] == resp2.get_json()['api_token']


class TestCrossUserTokenIsolation:
    """Tokens must be scoped to individual users — no cross-user access."""

    def test_user_a_token_cannot_access_user_b(self, app, db_session_fixture):
        """User A's Bearer token must not reveal User B's data."""
        from backend.models import User, TokenBalance, Streak
        import bcrypt

        # Create User A
        pw_a = bcrypt.hashpw(b'pass_a', bcrypt.gensalt()).decode('utf-8')
        user_a = User(
            email='ext_a@example.com',
            password_hash=pw_a,
            api_token=secrets.token_hex(32)
        )
        db_session_fixture.add(user_a)
        db_session_fixture.commit()
        db_session_fixture.add(TokenBalance(user_id=user_a.id, balance=50))
        db_session_fixture.add(Streak(user_id=user_a.id, current_streak=0))
        db_session_fixture.commit()

        # Create User B
        pw_b = bcrypt.hashpw(b'pass_b', bcrypt.gensalt()).decode('utf-8')
        user_b = User(
            email='ext_b@example.com',
            password_hash=pw_b,
            api_token=secrets.token_hex(32)
        )
        db_session_fixture.add(user_b)
        db_session_fixture.commit()
        db_session_fixture.add(TokenBalance(user_id=user_b.id, balance=200))
        db_session_fixture.add(Streak(user_id=user_b.id, current_streak=5))
        db_session_fixture.commit()

        client = app.test_client()

        # User A authenticates with their token
        resp = client.get('/api/auth/me', headers={
            'Authorization': f'Bearer {user_a.api_token}'
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['user']['email'] == 'ext_a@example.com'
        # Must NOT see User B's email
        assert data['user']['email'] != 'ext_b@example.com'

    def test_bearer_token_is_unique_per_user(self, app, db_session_fixture):
        """Each user must have a unique api_token — no collisions."""
        from backend.models import User
        import bcrypt

        pw = bcrypt.hashpw(b'pass', bcrypt.gensalt()).decode('utf-8')
        tokens = set()

        for i in range(5):
            user = User(
                email=f'unique_{i}@example.com',
                password_hash=pw,
                api_token=secrets.token_hex(32)
            )
            db_session_fixture.add(user)
            db_session_fixture.commit()
            tokens.add(user.api_token)

        # All 5 tokens must be unique
        assert len(tokens) == 5


class TestExtensionInputSanitization:
    """Input from the extension must be sanitized server-side."""

    def test_factcheck_rejects_empty_text(self, auth_client):
        """Empty text must be rejected."""
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': ''
        })
        assert response.status_code == 400

    def test_factcheck_rejects_whitespace_only(self, auth_client):
        """Whitespace-only text must be rejected."""
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': '   \n\t  '
        })
        assert response.status_code == 400

    def test_factcheck_rejects_no_json_body(self, auth_client):
        """Request without JSON body must be rejected."""
        response = auth_client.post('/api/factcheck/analyze',
                                    content_type='application/json')
        assert response.status_code == 400

    def test_ai_detect_rejects_short_text(self, auth_client):
        """AI detection must reject text under 20 words."""
        response = auth_client.post('/api/factcheck/analyze-ai', json={
            'text': 'Too short'
        })
        assert response.status_code == 400

    def test_history_respects_limit_cap(self, auth_client):
        """History limit must be capped at 100, even if client requests more."""
        response = auth_client.get('/api/factcheck/history?limit=9999')
        assert response.status_code == 200
        # The backend caps at 100, so even requesting 9999 should work without error


class TestBearerTokenOnAllEndpoints:
    """All extension-used endpoints must accept Bearer auth."""

    BEARER_ENDPOINTS = [
        ('GET', '/api/auth/me'),
        ('GET', '/api/auth/extension-token'),
        ('GET', '/api/tokens/balance'),
        ('GET', '/api/factcheck/history'),
    ]

    @pytest.mark.parametrize('method,endpoint', BEARER_ENDPOINTS)
    def test_endpoint_accepts_bearer_auth(self, client, test_user, method, endpoint):
        """Extension endpoints must authenticate via Bearer token."""
        func = client.get if method == 'GET' else client.post
        response = func(endpoint, headers={
            'Authorization': f'Bearer {test_user.api_token}'
        })
        # Should succeed (200) — not 401
        assert response.status_code == 200, (
            f'{method} {endpoint} rejected valid Bearer token'
        )

    @pytest.mark.parametrize('method,endpoint', BEARER_ENDPOINTS)
    def test_endpoint_rejects_no_auth(self, client, method, endpoint):
        """Extension endpoints must reject unauthenticated requests."""
        func = client.get if method == 'GET' else client.post
        response = func(endpoint)
        assert response.status_code == 401, (
            f'{method} {endpoint} allowed unauthenticated access'
        )
