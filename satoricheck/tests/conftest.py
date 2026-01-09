"""
Pytest configuration and fixtures for SatoriCheck integration tests.
Uses isolated SQLite database and mocked external services.
"""
import pytest
import os
import sys

# Ensure backend is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set test environment BEFORE importing app
os.environ['TEST_MODE'] = 'false'  # MUST be false to test auth requirements
os.environ['FLASK_ENV'] = 'testing'
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-for-testing-only'
os.environ['GEMINI_API_KEY'] = 'test-gemini-key'
os.environ['STRIPE_SECRET_KEY'] = 'sk_test_fake'
os.environ['STRIPE_WEBHOOK_SECRET'] = 'whsec_test_fake'


@pytest.fixture(scope='function')
def app():
    """Create application for testing with fresh database."""
    from backend.server import app as flask_app
    from backend.database import init_db, db_session, engine
    from backend.models import Base
    
    # Configure for testing
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    
    # Create all tables fresh
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    
    yield flask_app
    
    # Cleanup
    db_session.remove()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(app):
    """Test client for making requests."""
    return app.test_client()


@pytest.fixture
def db_session_fixture(app):
    """Direct access to database session."""
    from backend.database import db_session
    yield db_session
    db_session.rollback()


@pytest.fixture
def test_user(app, db_session_fixture):
    """Create a test user with token balance."""
    from backend.models import User, TokenBalance, Streak
    import bcrypt
    import secrets
    
    password_hash = bcrypt.hashpw(b'testpass123', bcrypt.gensalt())
    
    user = User(
        email='test@example.com',
        password_hash=password_hash.decode('utf-8'),
        api_token=secrets.token_hex(32)
    )
    db_session_fixture.add(user)
    db_session_fixture.commit()
    
    # Add token balance
    token_balance = TokenBalance(user_id=user.id, balance=100)
    db_session_fixture.add(token_balance)
    
    # Add streak
    streak = Streak(user_id=user.id, current_streak=0)
    db_session_fixture.add(streak)
    
    db_session_fixture.commit()
    
    return user


@pytest.fixture
def auth_client(client, test_user):
    """Authenticated test client."""
    # Login to get JWT cookie
    response = client.post('/api/auth/login', json={
        'email': 'test@example.com',
        'password': 'testpass123'
    })
    assert response.status_code == 200
    return client


@pytest.fixture
def mock_gemini(mocker):
    """Mock Gemini API responses."""
    mock_response = {
        'is_claim': True,
        'verdict': 'TRUE',
        'explanation': 'Test explanation',
        'fallacy': None,
        'sources': ['https://example.com'],
        'source_reliability': 'HIGH'
    }
    
    mocker.patch(
        'backend.services.gemini_service.GeminiService.analyze_claim',
        return_value=mock_response
    )
    return mock_response


@pytest.fixture
def mock_stripe_webhook_payload():
    """Generate mock Stripe webhook payload."""
    def _generate(user_id, package_type='battery_small', tokens=86, session_id='cs_test_123'):
        return {
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': session_id,
                    'customer': 'cus_test_123',
                    'metadata': {
                        'user_id': str(user_id),
                        'package_type': package_type,
                        'tokens': str(tokens)
                    }
                }
            }
        }
    return _generate
