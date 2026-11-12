"""
Pytest configuration and fixtures for Authenix integration tests.
Uses isolated SQLite database and mocked external services.
"""
import ipaddress
import pytest
import os
import socket
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
os.environ['SCHEDULER_SECRET'] = 'test-scheduler-secret-for-tests'


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'live: test talks to a real external API; exempt from the network guard',
    )


class ExternalNetworkBlocked(RuntimeError):
    """Raised when a unit test attempts a non-loopback network connection."""


def _normalize_host(host):
    return host.decode() if isinstance(host, bytes) else host


def _is_ip_literal(host) -> bool:
    try:
        ipaddress.ip_address(_normalize_host(host))
        return True
    except (TypeError, ValueError):
        return False


def _is_loopback(host) -> bool:
    host = _normalize_host(host)
    if host is None or host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_external_network(request, monkeypatch):
    """Fail any unit test that reaches a non-loopback host.

    Why: an unmocked Gemini/Stripe/Grok path otherwise hits the real API with the fake
    key, and the route's broad `except Exception` turns that into a 503 that a lenient
    assertion can accept as a pass. Raising alone is not enough for the same reason,
    so attempts are also recorded and the test is failed at teardown.

    Exempt: tests marked `@pytest.mark.live` and the Playwright suite in tests/e2e.
    """
    if request.node.get_closest_marker('live') or 'e2e' in request.node.path.parts:
        yield
        return

    attempts = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _deny(target):
        attempts.append(target)
        raise ExternalNetworkBlocked(
            f'Unit test attempted a real network call to {target!r}. '
            'Mock the external boundary or mark the test @pytest.mark.live.'
        )

    def _target_host(sock, address):
        # AF_UNIX addresses are filesystem paths, not hosts.
        return None if sock.family == getattr(socket, 'AF_UNIX', None) else address[0]

    def guarded_connect(sock, address):
        if not _is_loopback(_target_host(sock, address)):
            _deny(address)
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _is_loopback(_target_host(sock, address)):
            _deny(address)
        return real_connect_ex(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        # Resolving an IP literal performs no DNS lookup, so only hostnames are
        # gated here; connecting to a non-loopback IP is still blocked above.
        if not (_is_loopback(host) or _is_ip_literal(host)):
            _deny(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', guarded_getaddrinfo)

    yield

    if attempts:
        pytest.fail(
            f'Blocked {len(attempts)} external network attempt(s): {attempts[:3]}',
            pytrace=False,
        )


@pytest.fixture(scope='function')
def app():
    """Create application for testing with fresh database."""
    from backend.server import app as flask_app
    from backend.database import init_db, db_session, engine
    from backend.models import Base
    
    # Configure for testing
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    
    from backend.extensions import limiter
    limiter.reset()
    
    # Create all tables fresh
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    
    yield flask_app
    
    # Cleanup
    limiter.reset()
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
    """Generate mock Stripe webhook payload simulating stripe-python v15+."""
    from types import SimpleNamespace

    def _generate(user_id, package_type='battery_small', tokens=86, session_id='cs_test_123'):
        # The 'object' is now a SimpleNamespace for dot-notation access
        obj = SimpleNamespace(
            id=session_id,
            customer='cus_test_123',
            metadata={
                'user_id': str(user_id),
                'package_type': package_type,
                'tokens': str(tokens)
            }
        )
        # Top level is still a dict
        return {
            'type': 'checkout.session.completed',
            'data': {
                'object': obj
            }
        }
    return _generate
