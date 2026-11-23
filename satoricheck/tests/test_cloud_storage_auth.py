"""
Milestone M2 tests for cloud storage OAuth, HKDF-Fernet token encryption,
refresh/InvalidToken recovery, disconnect revocation, and delete-account cascade.

Follows tests/conftest.py and tests/test_suite_integrity.py strictly:
- Wire-only mocking (`requests.Session.request`, host-scoped `socket.getaddrinfo`)
- Exact HTTP status, payload, DB state, and wire-call assertions
- RFC 2606 @example.com emails
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json
import logging
import socket
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import bcrypt
from cryptography.fernet import InvalidToken
import pytest
import requests

from backend.config import Config
from backend.jwt_utils import JWT_COOKIE_NAME, create_token
from backend.models import (
    AuditLog,
    CloudStorageConnection,
    DeletedUser,
    SyncedDeckRecord,
    TokenBalance,
    User,
)
from backend.services.cloud_tokens import (
    CloudTokenError,
    decrypt_token,
    encrypt_token,
    execute_with_token_refresh,
    get_valid_access_token,
)
from backend.services.safe_http import safe_api_request

PUBLIC_TEST_IP = '93.184.216.34'
PROVIDER_HOSTS = frozenset({
    'oauth2.googleapis.com',
    'www.googleapis.com',
    'api.dropboxapi.com',
    'login.microsoftonline.com',
    'graph.microsoft.com',
})


def _make_wire_resp(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    resp.headers = {'Content-Type': 'application/json'} if json_data is not None else {}
    if json_data is not None:
        raw = json.dumps(json_data).encode('utf-8')
        resp.content = raw
        resp.text = raw.decode('utf-8')
        resp.json.return_value = json_data
    else:
        resp.content = b''
        resp.text = ''
        resp.json.side_effect = ValueError('No JSON')
    resp.raw = MagicMock()
    resp.raw._connection = MagicMock()
    resp.raw._connection.sock = MagicMock()
    resp.raw._connection.sock.getpeername.return_value = (PUBLIC_TEST_IP, 443)
    return resp


class _OAuthWireStub:
    def __init__(self):
        self.calls: list[dict] = []
        self.tokens = {
            'google_drive': {
                'access_token': 'ya29.m2_access_1',
                'refresh_token': '1//m2_refresh_1',
                'expires_in': 3600,
                'scope': 'https://www.googleapis.com/auth/drive.file',
            },
            'dropbox': {
                'access_token': 'sl.m2_dbx_access_1',
                'refresh_token': 'dbx_m2_refresh_1',
                'expires_in': 14400,
            },
            'onedrive': {
                'access_token': 'eyJ.m2_od_access_1',
                'refresh_token': 'od_m2_refresh_1',
                'expires_in': 3600,
            },
        }
        self.omit_refresh_on_exchange: set[str] = set()
        self.refresh_responses: dict[str, list[tuple[int, dict]]] = {}
        self.revoke_behavior: dict[str, int | str] = {}

    def handle(self, _session, method, url, **kwargs):
        method_u = str(method).upper()
        self.calls.append({
            'method': method_u,
            'url': url,
            'headers': dict(kwargs.get('headers') or {}),
            'data': kwargs.get('data'),
            'timeout': kwargs.get('timeout'),
            'allow_redirects': kwargs.get('allow_redirects'),
        })
        parsed = urlparse(url)
        host = parsed.hostname or ''
        path = parsed.path

        token_routes = {
            ('oauth2.googleapis.com', '/token'): 'google_drive',
            ('api.dropboxapi.com', '/oauth2/token'): 'dropbox',
            ('login.microsoftonline.com', '/common/oauth2/v2.0/token'): 'onedrive',
        }
        if (host, path) in token_routes and method_u == 'POST':
            prov = token_routes[(host, path)]
            data = kwargs.get('data') or {}
            if data.get('grant_type') == 'refresh_token':
                queue = self.refresh_responses.get(prov)
                if queue:
                    st, body = queue.pop(0)
                    return _make_wire_resp(st, body)
                return _make_wire_resp(200, {
                    'access_token': f'{prov}_refreshed_access',
                    'expires_in': 3600,
                })
            payload = dict(self.tokens[prov])
            if prov in self.omit_refresh_on_exchange:
                payload.pop('refresh_token', None)
            return _make_wire_resp(200, payload)

        revoke_routes = {
            ('oauth2.googleapis.com', '/revoke'): 'google_drive',
            ('api.dropboxapi.com', '/2/auth/token/revoke'): 'dropbox',
        }
        if (host, path) in revoke_routes and method_u == 'POST':
            prov = revoke_routes[(host, path)]
            behavior = self.revoke_behavior.get(prov, 200)
            if behavior == 'timeout':
                raise requests.exceptions.Timeout(f'{prov} revoke timeout')
            return _make_wire_resp(int(behavior), {})

        return _make_wire_resp(404, {'error': 'unhandled'})


@pytest.fixture
def oauth_wire(monkeypatch):
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_ID', 'test-g-id', raising=False)
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_SECRET', 'test-g-sec', raising=False)
    monkeypatch.setattr(Config, 'DROPBOX_CLIENT_ID', 'test-d-id', raising=False)
    monkeypatch.setattr(Config, 'DROPBOX_CLIENT_SECRET', 'test-d-sec', raising=False)
    monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_ID', 'test-o-id', raising=False)
    monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_SECRET', 'test-o-sec', raising=False)
    monkeypatch.setattr(Config, 'CLOUD_OAUTH_REDIRECT_BASE', None, raising=False)

    stub = _OAuthWireStub()
    orig_getaddrinfo = socket.getaddrinfo

    def _dns(host, port, *args, **kwargs):
        host_str = host.decode() if isinstance(host, bytes) else str(host or '')
        if host_str.strip('[]').lower() in PROVIDER_HOSTS:
            port_i = int(port) if str(port).isdigit() else 443
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (PUBLIC_TEST_IP, port_i))]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    with patch('socket.getaddrinfo', side_effect=_dns), \
         patch('requests.Session.request', autospec=True, side_effect=stub.handle):
        yield stub


def _create_second_client(app, db_session_fixture, email: str):
    pw_hash = bcrypt.hashpw(b'testpass123', bcrypt.gensalt(rounds=4)).decode('utf-8')
    user = User(email=email, password_hash=pw_hash)
    db_session_fixture.add(user)
    db_session_fixture.commit()
    db_session_fixture.add(TokenBalance(user_id=user.id, balance=50))
    db_session_fixture.commit()
    c = app.test_client()
    c.set_cookie(JWT_COOKIE_NAME, create_token(user.id, user.email), domain='localhost')
    return user, c


def _connect_provider(client, provider: str, code: str = 'code_1'):
    conn_resp = client.get(f'/api/storage/{provider}/connect')
    assert conn_resp.status_code == 302
    params = {k: v[0] for k, v in parse_qs(urlparse(conn_resp.headers['Location']).query).items()}
    cb_resp = client.get(f"/api/storage/{provider}/callback?code={code}&state={params['state']}")
    assert cb_resp.status_code == 302
    assert cb_resp.headers['Location'] == f'/?storage_connected={provider}'
    return conn_resp, cb_resp


class TestStorageOAuthRoutes:
    """Verify OAuth connect, callback, status, disconnect, CSRF state, PKCE, and IDOR isolation."""

    def test_connections_returns_unconfigured_defaults_for_all_providers(
        self, auth_client, db_session_fixture, test_user, oauth_wire
    ):
        resp = auth_client.get('/api/storage/connections')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        by_prov = {c['provider']: c for c in data['connections']}
        assert set(by_prov.keys()) == {'google_drive', 'dropbox', 'onedrive'}
        for prov in ('google_drive', 'dropbox', 'onedrive'):
            assert by_prov[prov]['connected'] is False
            assert by_prov[prov]['daily_sync_enabled'] is False
            assert by_prov[prov]['memo_preset'] == 'brief'
            assert by_prov[prov]['auto_export_sheets'] is False
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0
        assert oauth_wire.calls == []

    def test_google_drive_connect_uses_non_restricted_scopes_and_pkce(
        self, auth_client, oauth_wire
    ):
        resp = auth_client.get('/api/storage/google_drive/connect')
        assert resp.status_code == 302
        location = resp.headers['Location']
        assert location.startswith('https://accounts.google.com/o/oauth2/v2/auth?')
        params = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        scopes = params['scope'].split()
        assert 'https://www.googleapis.com/auth/drive.file' in scopes
        assert 'https://www.googleapis.com/auth/spreadsheets' in scopes
        assert 'https://www.googleapis.com/auth/drive.readonly' not in scopes
        assert params['access_type'] == 'offline'
        assert params['prompt'] == 'consent'
        assert params['include_granted_scopes'] == 'true'
        assert params['code_challenge_method'] == 'S256'
        assert len(params['code_challenge']) >= 32
        assert len(params['state']) >= 16

    def test_connect_uses_cloud_oauth_redirect_base_when_configured(
        self, auth_client, monkeypatch, oauth_wire
    ):
        monkeypatch.setattr(Config, 'CLOUD_OAUTH_REDIRECT_BASE', 'https://authenix.ai/', raising=False)
        resp = auth_client.get('/api/storage/dropbox/connect')
        assert resp.status_code == 302
        params = {k: v[0] for k, v in parse_qs(urlparse(resp.headers['Location']).query).items()}
        assert params['redirect_uri'] == 'https://authenix.ai/api/storage/dropbox/callback'
        assert params['token_access_type'] == 'offline'

    def test_connect_rejects_invalid_and_unconfigured_providers(
        self, auth_client, monkeypatch, oauth_wire
    ):
        bad_prov = auth_client.get('/api/storage/box/connect')
        assert bad_prov.status_code == 400
        assert bad_prov.get_json()['success'] is False

        monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_ID', None, raising=False)
        unconf = auth_client.get('/api/storage/onedrive/connect')
        assert unconf.status_code == 400
        assert unconf.get_json()['success'] is False
        assert oauth_wire.calls == []

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_callback_stores_encrypted_tokens_and_preserves_user_identity(
        self, auth_client, db_session_fixture, test_user, oauth_wire, provider
    ):
        _, cb_resp = _connect_provider(auth_client, provider, code='auth_ok')
        assert JWT_COOKIE_NAME not in cb_resp.headers.get('Set-Cookie', '')

        db_session_fixture.expire_all()
        user = db_session_fixture.query(User).filter_by(id=test_user.id).one()
        assert user.email == 'test@example.com'

        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider=provider
        ).one()
        assert conn.encrypted_access_token.startswith('gAAAA')
        assert decrypt_token(conn.encrypted_access_token) == oauth_wire.tokens[provider]['access_token']
        assert decrypt_token(conn.encrypted_refresh_token) == oauth_wire.tokens[provider]['refresh_token']
        assert conn.token_expiry is not None

        audit = db_session_fixture.query(AuditLog).filter_by(
            user_id=test_user.id, provider=provider, event_type='storage.connected'
        ).first()
        assert audit is not None

        assert len(oauth_wire.calls) == 1
        assert oauth_wire.calls[0]['allow_redirects'] is False
        assert oauth_wire.calls[0]['data']['grant_type'] == 'authorization_code'
        assert oauth_wire.calls[0]['data']['code'] == 'auth_ok'
        assert len(oauth_wire.calls[0]['data']['code_verifier']) >= 32

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_reconsent_omitting_refresh_token_preserves_existing_refresh_token(
        self, auth_client, db_session_fixture, test_user, oauth_wire, provider
    ):
        _connect_provider(auth_client, provider, code='first_auth')
        original_refresh = oauth_wire.tokens[provider]['refresh_token']

        oauth_wire.tokens[provider]['access_token'] = f'{provider}_new_access_2'
        oauth_wire.omit_refresh_on_exchange.add(provider)
        _connect_provider(auth_client, provider, code='second_auth')

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider=provider
        ).one()
        assert decrypt_token(conn.encrypted_access_token) == f'{provider}_new_access_2'
        assert decrypt_token(conn.encrypted_refresh_token) == original_refresh

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_callback_handles_access_denied_and_invalid_state(
        self, auth_client, db_session_fixture, test_user, oauth_wire, provider
    ):
        conn_resp = auth_client.get(f'/api/storage/{provider}/connect')
        state = parse_qs(urlparse(conn_resp.headers['Location']).query)['state'][0]

        denied = auth_client.get(f'/api/storage/{provider}/callback?error=access_denied&state={state}')
        assert denied.status_code == 302
        assert denied.headers['Location'] == '/?storage_error=access_denied'

        tampered = auth_client.get(f'/api/storage/{provider}/callback?code=abc&state=wrong_state')
        assert tampered.status_code == 302
        assert tampered.headers['Location'] == '/?storage_error=invalid_state'

        missing = auth_client.get(f'/api/storage/{provider}/callback')
        assert missing.status_code == 302
        assert missing.headers['Location'] == '/?storage_error=invalid_state'

        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0
        assert oauth_wire.calls == []

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_callback_non_ascii_state_with_active_session_redirects_invalid_state(
        self, auth_client, db_session_fixture, test_user, oauth_wire, provider
    ):
        conn_resp = auth_client.get(f'/api/storage/{provider}/connect')
        assert conn_resp.status_code == 302

        non_ascii = auth_client.get(f'/api/storage/{provider}/callback?code=abc&state=%C3%A9')
        assert non_ascii.status_code == 302
        assert non_ascii.headers['Location'] == '/?storage_error=invalid_state'
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0
        assert oauth_wire.calls == []

    def test_status_omits_tokens_and_enforces_user_isolation(
        self, app, auth_client, db_session_fixture, test_user, oauth_wire
    ):
        _connect_provider(auth_client, 'google_drive')
        oauth_wire.calls.clear()

        own_status = auth_client.get('/api/storage/google_drive/status')
        assert own_status.status_code == 200
        body = own_status.get_json()
        assert body['connected'] is True
        assert body['provider'] == 'google_drive'
        assert 'encrypted_access_token' not in body
        assert 'encrypted_refresh_token' not in body
        assert 'access_token' not in body

        _, other_client = _create_second_client(app, db_session_fixture, 'other-user@example.com')
        other_status = other_client.get('/api/storage/google_drive/status')
        assert other_status.status_code == 200
        assert other_status.get_json()['connected'] is False

        other_disc = other_client.post('/api/storage/google_drive/disconnect')
        assert other_disc.status_code == 200
        assert other_disc.get_json()['connected'] is False
        assert oauth_wire.calls == []
        assert db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).count() == 1


class TestCloudTokenLifecycleAndRevocation:
    """Verify HKDF dynamic key fallback, InvalidToken recovery, 401 single-retry, and revocation."""

    def test_dynamic_hkdf_fallback_and_key_rotation_invalid_token(
        self, monkeypatch, db_session_fixture, test_user
    ):
        monkeypatch.setattr(Config, 'GOOGLE_TOKEN_ENCRYPTION_KEY', None, raising=False)
        ct_default = encrypt_token('tok_fallback')
        assert ct_default.startswith('gAAAA')
        assert decrypt_token(ct_default) == 'tok_fallback'

        monkeypatch.setattr(Config, 'GOOGLE_TOKEN_ENCRYPTION_KEY', 'explicit-key-0123456789abcdef', raising=False)
        with pytest.raises(InvalidToken):
            decrypt_token(ct_default)

        ct_explicit = encrypt_token('tok_explicit')
        assert decrypt_token(ct_explicit) == 'tok_explicit'

        conn = CloudStorageConnection(
            user_id=test_user.id,
            provider='google_drive',
            encrypted_access_token=ct_explicit,
            encrypted_refresh_token=encrypt_token('refresh_explicit'),
        )
        db_session_fixture.add(conn)
        db_session_fixture.commit()
        db_session_fixture.add(SyncedDeckRecord(
            user_id=test_user.id,
            connection_id=conn.id,
            external_file_id='f_rot_1',
            file_name='deck.pdf',
            file_etag_or_modified='v1',
            status='completed',
            cp_charged=3,
        ))
        db_session_fixture.commit()

        monkeypatch.setattr(Config, 'GOOGLE_TOKEN_ENCRYPTION_KEY', 'rotated-new-key-0123456789abc', raising=False)
        with pytest.raises(CloudTokenError) as exc_info:
            get_valid_access_token(db_session_fixture, conn)
        assert exc_info.value.event_type == 'storage.invalid_token'
        assert exc_info.value.status_code == 401

        db_session_fixture.expire_all()
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=test_user.id).count() == 0
        audit = db_session_fixture.query(AuditLog).filter_by(
            user_id=test_user.id, provider='google_drive', event_type='storage.invalid_token'
        ).first()
        assert audit is not None

    def test_naive_sqlite_expiry_triggers_proactive_refresh_and_missing_refresh_purges(
        self, db_session_fixture, test_user, oauth_wire
    ):
        conn = CloudStorageConnection(
            user_id=test_user.id,
            provider='dropbox',
            encrypted_access_token=encrypt_token('old_access'),
            encrypted_refresh_token=encrypt_token('keep_refresh'),
            token_expiry=(datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None),
        )
        db_session_fixture.add(conn)
        db_session_fixture.commit()

        token = get_valid_access_token(db_session_fixture, conn)
        assert token == 'dropbox_refreshed_access'
        assert decrypt_token(conn.encrypted_refresh_token) == 'keep_refresh'
        assert len(oauth_wire.calls) == 1
        assert oauth_wire.calls[0]['data']['grant_type'] == 'refresh_token'

        # Expired with missing refresh token -> purge without wire call
        oauth_wire.calls.clear()
        conn.encrypted_refresh_token = None
        conn.token_expiry = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None)
        db_session_fixture.commit()

        with pytest.raises(CloudTokenError) as exc_info:
            get_valid_access_token(db_session_fixture, conn)
        assert exc_info.value.event_type == 'storage.reauth_required'
        assert oauth_wire.calls == []
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0

    def test_execute_with_token_refresh_bounds_double_401_and_handles_invalid_grant(
        self, db_session_fixture, test_user, oauth_wire
    ):
        conn = CloudStorageConnection(
            user_id=test_user.id,
            provider='onedrive',
            encrypted_access_token=encrypt_token('access_1'),
            encrypted_refresh_token=encrypt_token('refresh_1'),
            token_expiry=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session_fixture.add(conn)
        db_session_fixture.commit()

        statuses = [401, 401]

        def _always_401(_tok):
            return _make_wire_resp(statuses.pop(0), {'error': 'unauthorized'})

        with pytest.raises(CloudTokenError) as exc_info:
            execute_with_token_refresh(db_session_fixture, conn, _always_401)
        assert exc_info.value.event_type == 'storage.reauth_required'
        assert len(oauth_wire.calls) == 1
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0

        # invalid_grant on refresh also purges and logs storage.reauth_required
        oauth_wire.calls.clear()
        conn2 = CloudStorageConnection(
            user_id=test_user.id,
            provider='google_drive',
            encrypted_access_token=encrypt_token('access_2'),
            encrypted_refresh_token=encrypt_token('refresh_2'),
            token_expiry=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session_fixture.add(conn2)
        db_session_fixture.commit()
        oauth_wire.refresh_responses['google_drive'] = [(400, {'error': 'invalid_grant'})]

        with pytest.raises(CloudTokenError) as exc_info2:
            execute_with_token_refresh(db_session_fixture, conn2, lambda _t: _make_wire_resp(401, {}))
        assert exc_info2.value.event_type == 'storage.reauth_required'
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0

    @pytest.mark.parametrize('revoke_outcome', [200, 503, 'timeout'])
    def test_disconnect_revokes_best_effort_and_deletes_records(
        self, auth_client, db_session_fixture, test_user, oauth_wire, revoke_outcome
    ):
        _connect_provider(auth_client, 'google_drive')
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        db_session_fixture.add(SyncedDeckRecord(
            user_id=test_user.id,
            connection_id=conn.id,
            external_file_id='f_disc_1',
            file_name='deck.pdf',
            file_etag_or_modified='v1',
            status='completed',
            cp_charged=3,
        ))
        db_session_fixture.commit()
        oauth_wire.calls.clear()
        oauth_wire.revoke_behavior['google_drive'] = revoke_outcome

        resp = auth_client.post('/api/storage/google_drive/disconnect')
        assert resp.status_code == 200
        assert resp.get_json() == {'success': True, 'provider': 'google_drive', 'connected': False}

        db_session_fixture.expire_all()
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=test_user.id).count() == 0
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=test_user.id).count() == 0
        audit = db_session_fixture.query(AuditLog).filter_by(
            user_id=test_user.id, provider='google_drive', event_type='storage.disconnected'
        ).first()
        assert audit is not None
        assert len(oauth_wire.calls) == 1
        assert oauth_wire.calls[0]['timeout'] == 5
        assert oauth_wire.calls[0]['data'] == {'token': '1//m2_refresh_1'}

    def test_delete_account_revokes_providers_cascades_and_logs_zero_pii(
        self, auth_client, db_session_fixture, test_user, oauth_wire, caplog
    ):
        for prov in ('google_drive', 'dropbox', 'onedrive'):
            _connect_provider(auth_client, prov)
            conn = db_session_fixture.query(CloudStorageConnection).filter_by(
                user_id=test_user.id, provider=prov
            ).one()
            db_session_fixture.add(SyncedDeckRecord(
                user_id=test_user.id,
                connection_id=conn.id,
                external_file_id=f'{prov}_f1',
                file_name='deck.pdf',
                file_etag_or_modified='v1',
                status='completed',
                cp_charged=3,
            ))
        db_session_fixture.commit()
        oauth_wire.calls.clear()
        oauth_wire.revoke_behavior['dropbox'] = 'timeout'
        user_id = test_user.id
        user_email = test_user.email

        with caplog.at_level(logging.INFO):
            resp = auth_client.post('/api/auth/delete-account')
        assert resp.status_code == 200
        assert resp.get_json()['success'] is True

        db_session_fixture.expire_all()
        assert db_session_fixture.query(User).filter_by(id=user_id).count() == 0
        assert db_session_fixture.query(CloudStorageConnection).filter_by(user_id=user_id).count() == 0
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=user_id).count() == 0
        assert db_session_fixture.query(DeletedUser).count() == 1

        urls = [c['url'] for c in oauth_wire.calls]
        assert 'https://oauth2.googleapis.com/revoke' in urls
        assert 'https://api.dropboxapi.com/2/auth/token/revoke' in urls
        assert len(urls) == 2

        assert user_email not in caplog.text
        assert 'ya29.m2_access_1' not in caplog.text
        assert '1//m2_refresh_1' not in caplog.text
        assert 'sl.m2_dbx_access_1' not in caplog.text

    def test_huge_expires_in_is_clamped_without_overflow(
        self, auth_client, db_session_fixture, test_user, oauth_wire
    ):
        oauth_wire.tokens['google_drive']['expires_in'] = 10**12
        before = datetime.now(UTC).replace(tzinfo=None)
        _connect_provider(auth_client, 'google_drive', code='huge_exp_1')

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        max_allowed = before + timedelta(seconds=10 * 365 * 86400 + 60)
        min_allowed = before + timedelta(seconds=10 * 365 * 86400 - 60)
        expiry_naive = conn.token_expiry.replace(tzinfo=None)
        assert min_allowed <= expiry_naive <= max_allowed

        # Also verify refresh path clamps huge expires_in without OverflowError
        conn.token_expiry = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None)
        db_session_fixture.commit()
        oauth_wire.refresh_responses['google_drive'] = [
            (200, {'access_token': 'ya29.clamped_refresh', 'expires_in': 10**12})
        ]
        token = get_valid_access_token(db_session_fixture, conn)
        assert token == 'ya29.clamped_refresh'
        db_session_fixture.expire_all()
        conn_after = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        expiry_after = conn_after.token_expiry.replace(tzinfo=None)
        assert min_allowed <= expiry_after <= max_allowed + timedelta(seconds=60)
