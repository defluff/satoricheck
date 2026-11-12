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
    
    password_hash = bcrypt.hashpw(b'testpass123', bcrypt.gensalt(rounds=4))
    
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
    """Mock Gemini SDK wire boundary (`models.generate_content`) with typed SDK response."""
    import json
    from unittest.mock import MagicMock
    from google.genai import types
    from backend.services import get_gemini_service

    mock_payload = {
        'is_claim': True,
        'verdict': 'TRUE',
        'explanation': 'Test explanation',
        'fallacy': None,
        'sources': ['https://example.com'],
        'source_reliability': 'HIGH',
    }
    sdk_response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role='model',
                    parts=[types.Part.from_text(text=json.dumps(mock_payload))],
                )
            )
        ]
    )

    svc = get_gemini_service()
    mocker.patch.object(svc.client.models, 'generate_content', return_value=sdk_response)

    # Wire-level DNS + HEAD mocks for source reachability check so _validate_url
    # and _is_private_ip execute their real SSRF validation in memory.
    prev_getaddrinfo = socket.getaddrinfo

    def _resolve_example_com(host, port, *args, **kwargs):
        if host == 'example.com':
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port or 443))]
        return prev_getaddrinfo(host, port, *args, **kwargs)

    mocker.patch.object(socket, 'getaddrinfo', side_effect=_resolve_example_com)
    mocker.patch(
        'backend.services.gemini.client.requests.head',
        return_value=MagicMock(status_code=200, headers={}),
    )
    return mock_payload


@pytest.fixture
def mock_stripe_webhook_payload():
    """Build a raw Stripe webhook JSON payload and real HMAC-SHA256 Stripe-Signature header."""
    import hashlib
    import hmac
    import json
    import time

    def _generate(
        user_id,
        package_type='battery_small',
        tokens=86,
        session_id='cs_test_123',
        secret='whsec_test_fake',
        timestamp=None,
    ) -> tuple[bytes, str]:
        payload_str = json.dumps({
            'id': f'evt_{session_id}',
            'object': 'event',
            'type': 'checkout.session.completed',
            'data': {
                'object': {
                    'id': session_id,
                    'object': 'checkout.session',
                    'customer': 'cus_test_123',
                    'metadata': {
                        'user_id': str(user_id),
                        'package_type': package_type,
                        'tokens': str(tokens),
                    },
                }
            },
        })
        ts = timestamp if timestamp is not None else int(time.time())
        signed_payload = f'{ts}.{payload_str}'.encode('utf-8')
        signature = hmac.new(secret.encode('utf-8'), signed_payload, hashlib.sha256).hexdigest()
        return payload_str.encode('utf-8'), f't={ts},v1={signature}'

    return _generate
