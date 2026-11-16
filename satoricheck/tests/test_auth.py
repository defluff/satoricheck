"""
Authentication Integration Tests.
Tests Signup, Login, Deleted User Prevention, and Token Security.
"""


class TestSignup:
    """Test user registration flow."""
    
    def test_signup_creates_user_with_bonus(self, client, db_session_fixture):
        """New signup should create user with bonus tokens."""
        from backend.models import User, TokenBalance
        
        response = client.post('/api/auth/signup', json={
            'email': 'newuser@example.com',
            'password': 'SecurePass123!'
        })
        
        assert response.status_code == 200
        
        # Verify user created
        user = db_session_fixture.query(User).filter_by(email='newuser@example.com').first()
        assert user is not None
        
        # Verify bonus tokens
        balance = db_session_fixture.query(TokenBalance).filter_by(user_id=user.id).first()
        assert balance is not None
        assert balance.balance > 0  # Should have signup bonus
    
    def test_deleted_user_no_bonus(self, client, db_session_fixture):
        """User who deleted account should not get bonus on re-signup."""
        from backend.models import DeletedUser, User, TokenBalance
        from backend.config import Config
        import hashlib
        
        email = 'returning@example.com'
        email_hash = hashlib.sha256(email.lower().strip().encode()).hexdigest()
        
        # Record as previously deleted
        deleted = DeletedUser(email_hash=email_hash)
        db_session_fixture.add(deleted)
        db_session_fixture.commit()
        
        # Now try to signup
        response = client.post('/api/auth/signup', json={
            'email': email,
            'password': 'SecurePass123!'
        })
        
        assert response.status_code == 200
        
        # Verify no bonus
        user = db_session_fixture.query(User).filter_by(email=email).first()
        balance = db_session_fixture.query(TokenBalance).filter_by(user_id=user.id).first()
        
        assert balance.balance == 0  # No bonus for returning user

    def test_signup_sets_jwt_cookie(self, client):
        """Signup should issue a stateless HttpOnly authenix_token JWT cookie."""
        response = client.post('/api/auth/signup', json={
            'email': 'jwt_signup@example.com',
            'password': 'SecurePass123!'
        })

        assert response.status_code == 200

        cookies = response.headers.getlist('Set-Cookie')
        jwt_cookies = [c for c in cookies if 'authenix_token=' in c]
        assert len(jwt_cookies) == 1
        assert 'HttpOnly' in jwt_cookies[0]

        # Clear legacy Flask session cookie to prove stateless JWT auth succeeds alone
        client.delete_cookie('session', domain='localhost')
        me_response = client.get('/api/auth/me')
        assert me_response.status_code == 200
        assert me_response.get_json()['user']['email'] == 'jwt_signup@example.com'


class TestLogin:
    """Test login flow."""
    
    def test_login_success_sets_cookie(self, client, test_user):
        """Successful login should set JWT cookie."""
        response = client.post('/api/auth/login', json={
            'email': 'test@example.com',
            'password': 'testpass123'
        })
        
        assert response.status_code == 200
        
        # Check for cookie
        cookies = response.headers.getlist('Set-Cookie')
        assert any('authenix_token' in c for c in cookies)
    
    def test_login_wrong_password(self, client, test_user):
        """Wrong password should fail."""
        response = client.post('/api/auth/login', json={
            'email': 'test@example.com',
            'password': 'wrongpassword'
        })
        
        assert response.status_code == 401

    def test_login_unknown_user_still_executes_bcrypt_checkpw_timing_guard(self, client, mocker):
        """Finding 11: login for a non-existent email must still run bcrypt.checkpw to prevent timing enumeration."""
        import bcrypt
        from backend.routes import auth as auth_mod

        spy = mocker.spy(bcrypt, 'checkpw')
        response = client.post('/api/auth/login', json={
            'email': 'does-not-exist@example.com',
            'password': 'candidate-password-123',
        })

        assert response.status_code == 401
        assert response.get_json()['error'] == 'Invalid email or password'
        spy.assert_called_once_with(b'candidate-password-123', auth_mod._DUMMY_BCRYPT_HASH)


class TestTokenRefresh:
    """Test JWT sliding window refresh."""

    def test_near_expiry_token_gets_refreshed_cookie(self, client, test_user):
        """A JWT with <1 day remaining should get a refreshed Set-Cookie."""
        from backend.jwt_utils import TOKEN_EXPIRY_DAYS
        from backend.config import Config
        from datetime import datetime, timedelta, UTC
        import jwt as pyjwt

        # Craft a token that expires in 30 minutes (< 1 day threshold)
        payload = {
            'user_id': test_user.id,
            'email': test_user.email,
            'iat': datetime.now(UTC) - timedelta(days=TOKEN_EXPIRY_DAYS),
            'exp': datetime.now(UTC) + timedelta(minutes=30),
        }
        near_expiry_token = pyjwt.encode(payload, Config.SECRET_KEY, algorithm='HS256')

        # Call a protected endpoint with the near-expiry cookie
        client.set_cookie('authenix_token', near_expiry_token, domain='localhost')
        response = client.get('/api/auth/me')

        assert response.status_code == 200

        # The response should contain a refreshed authenix_token cookie
        cookies = response.headers.getlist('Set-Cookie')
        assert any('authenix_token' in c for c in cookies), (
            'Expected a refreshed authenix_token cookie but none was set'
        )

    def test_fresh_token_not_refreshed(self, client, test_user):
        """A JWT with plenty of time left should NOT get a refreshed cookie."""
        from backend.jwt_utils import create_token

        fresh_token = create_token(test_user.id, test_user.email)

        client.set_cookie('authenix_token', fresh_token, domain='localhost')
        response = client.get('/api/auth/me')

        assert response.status_code == 200

        # No authenix_token should be re-set
        cookies = response.headers.getlist('Set-Cookie')
        assert not any('authenix_token' in c for c in cookies), (
            'Fresh token should not trigger a refresh'
        )


class TestAuthRequired:
    """Test authentication requirement on protected endpoints."""
    
    def test_me_requires_auth(self, client):
        """Get current user requires authentication."""
        response = client.get('/api/auth/me')
        assert response.status_code == 401
    
    def test_factcheck_requires_auth(self, client):
        """Fact-check requires authentication."""
        response = client.post('/api/factcheck/analyze', json={'text': 'test'})
        assert response.status_code == 401
    
    def test_tokens_requires_auth(self, client):
        """Token balance requires authentication."""
        response = client.get('/api/tokens/balance')
        assert response.status_code == 401


class TestExtensionToken:
    """Test extension token endpoint for browser extension auth."""

    def test_extension_token_requires_auth(self, client):
        """Extension token endpoint requires authentication."""
        response = client.get('/api/auth/extension-token')
        assert response.status_code == 401

    def test_extension_token_returns_valid_token(self, auth_client, test_user):
        """Authenticated user receives their api_token."""
        response = auth_client.get('/api/auth/extension-token')
        assert response.status_code == 200

        data = response.get_json()
        assert data['success'] is True
        assert data['api_token'] == test_user.api_token
        assert len(data['api_token']) == 64  # 32-byte hex

    def test_extension_token_generates_if_missing(self, auth_client, test_user, db_session_fixture):
        """If api_token is None (pre-migration user), one is generated on the fly."""
        test_user.api_token = None
        db_session_fixture.commit()

        response = auth_client.get('/api/auth/extension-token')
        assert response.status_code == 200

        data = response.get_json()
        assert data['success'] is True
        assert len(data['api_token']) == 64

        # Verify it was persisted
        db_session_fixture.refresh(test_user)
        assert test_user.api_token == data['api_token']

    def test_extension_token_accessible_via_bearer(self, client, test_user):
        """Extension token endpoint is accessible via Bearer token auth."""
        response = client.get('/api/auth/extension-token', headers={
            'Authorization': f'Bearer {test_user.api_token}'
        })
        assert response.status_code == 200

        data = response.get_json()
        assert data['api_token'] == test_user.api_token

