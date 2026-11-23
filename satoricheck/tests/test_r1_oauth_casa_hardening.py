"""
Milestone M1 (R1) Security, Privacy, Google OAuth & CASA Tier 2 Hardening Tests.

Adheres strictly to tests/test_suite_integrity.py and tests/conftest.py:
- Wire-only mocking on external SDK / network boundaries
- Exact HTTP status, payload, DB state, and caplog assertions
- RFC 2606 @example.com emails
"""
import base64
import io
import json
import logging
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import bcrypt
import pytest
import requests
import stripe

from backend.config import Config
from backend.models import AuditLog, DeletedUser, FactCheck, TokenBalance, User, record_audit_event


def _make_minimal_pdf_bytes() -> bytes:
    return (
        b'%PDF-1.4\n'
        b'1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n'
        b'2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n'
        b'3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n'
        b'xref\n0 4\n0000000000 65535 f \n'
        b'trailer << /Size 4 /Root 1 0 R >>\nstartxref\n0\n%%EOF\n'
    )


class TestPIILogScrubbing:
    """Verify user emails and claim/context snippets never appear in application logs."""

    SENTINEL_EMAIL = 'pii-sentinel@example.com'
    SENTINEL_CLAIM = 'SECRET_CLAIM_SENTINEL_XYZ_987'
    SENTINEL_CONTEXT = 'SECRET_CONTEXT_SNIPPET_ABC_654'

    def test_auth_lifecycle_logs_scrub_email_and_user_repr(self, client, db_session_fixture, caplog):
        caplog.set_level(logging.INFO)

        signup_resp = client.post('/api/auth/signup', json={
            'email': self.SENTINEL_EMAIL,
            'password': 'StrongPassword123!',
        })
        assert signup_resp.status_code == 200
        user = db_session_fixture.query(User).filter_by(email=self.SENTINEL_EMAIL).one()
        assert self.SENTINEL_EMAIL not in repr(user)
        assert repr(user) == f'<User id={user.id}>'

        # Bad password login + unknown user login + successful login
        bad_pw = client.post('/api/auth/login', json={
            'email': self.SENTINEL_EMAIL,
            'password': 'WrongPassword!',
        })
        assert bad_pw.status_code == 401

        unknown = client.post('/api/auth/login', json={
            'email': 'unknown-sentinel@example.com',
            'password': 'WrongPassword!',
        })
        assert unknown.status_code == 401

        good_login = client.post('/api/auth/login', json={
            'email': self.SENTINEL_EMAIL,
            'password': 'StrongPassword123!',
        })
        assert good_login.status_code == 200

        # Force missing api_token branch in /api/auth/extension-token
        user.api_token = None
        db_session_fixture.commit()
        ext_resp = client.get('/api/auth/extension-token')
        assert ext_resp.status_code == 200

        del_resp = client.post('/api/auth/delete-account')
        assert del_resp.status_code == 200

        assert self.SENTINEL_EMAIL not in caplog.text
        assert 'unknown-sentinel@example.com' not in caplog.text

    def test_factcheck_media_billing_and_export_logs_scrub_pii_and_content(
        self, auth_client, test_user, db_session_fixture, mock_gemini, caplog
    ):
        caplog.set_level(logging.INFO)

        # Ensure test_user has a distinct sentinel email
        test_user.email = self.SENTINEL_EMAIL
        db_session_fixture.commit()

        # 1. /api/factcheck/analyze with context
        fc_resp = auth_client.post('/api/factcheck/analyze', json={
            'text': self.SENTINEL_CLAIM,
            'context': self.SENTINEL_CONTEXT,
        })
        assert fc_resp.status_code == 200

        # 2. /api/factcheck/analyze-batch (cache hit on SENTINEL_CLAIM)
        batch_resp = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': [self.SENTINEL_CLAIM],
        })
        assert batch_resp.status_code == 200

        # 3. /api/factcheck/identify-claims
        from backend.services import get_gemini_service
        svc = get_gemini_service()
        claims_sdk_resp = MagicMock()
        claims_sdk_resp.text = json.dumps({'claims': [self.SENTINEL_CLAIM]})
        with patch.object(svc.client.models, 'generate_content', return_value=claims_sdk_resp):
            id_resp = auth_client.post('/api/factcheck/identify-claims', json={
                'text': self.SENTINEL_CLAIM,
            })
        assert id_resp.status_code == 200

        # 4. /api/factcheck/analyze-ai (>= 20 words)
        ai_text = (self.SENTINEL_CLAIM + ' ') + ' '.join(['word'] * 22)
        ai_sdk_resp = MagicMock()
        ai_sdk_resp.text = json.dumps({
            'ai_probability': 15,
            'confidence': 'HIGH',
            'detected_register': 'general',
            'is_short_text': True,
            'ai_indicators': [],
            'human_indicators': ['varied phrasing'],
            'explanation': 'Looks human.',
        })
        with patch.object(svc.client.models, 'generate_content', return_value=ai_sdk_resp):
            ai_resp = auth_client.post('/api/factcheck/analyze-ai', json={'text': ai_text})
        assert ai_resp.status_code == 200

        # 5. /api/media/analyze-upload (success + failure refund path)
        png_bytes = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
        media_sdk_resp = MagicMock()
        media_part = MagicMock()
        media_part.thought = False
        media_part.text = json.dumps({
            'verdict': 'authentic',
            'confidence': 90,
            'explanation': 'No artifacts.',
            'criteria': {},
            'claims': [],
        })
        media_cand = MagicMock()
        media_cand.content = MagicMock(parts=[media_part])
        media_sdk_resp.candidates = [media_cand]
        media_sdk_resp.text = media_part.text
        with patch.object(svc.client.models, 'generate_content', return_value=media_sdk_resp):
            media_ok = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(png_bytes), 'sample.png')},
                content_type='multipart/form-data',
            )
        assert media_ok.status_code == 200

        with patch.object(svc.client.models, 'generate_content', side_effect=RuntimeError('upstream error')):
            media_fail = auth_client.post(
                '/api/media/analyze-upload',
                data={'file': (io.BytesIO(png_bytes), 'sample.png')},
                content_type='multipart/form-data',
            )
        assert media_fail.status_code == 400

        # 6. /api/billing/checkout (success + Stripe error path)
        with patch('stripe.Customer.create', return_value=MagicMock(id='cus_log_1')), \
             patch('stripe.checkout.Session.create', return_value=MagicMock(id='cs_log_1', url='https://checkout.stripe.com/pay/cs_log_1')):
            bill_ok = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_small'})
        assert bill_ok.status_code == 200

        with patch('stripe.Customer.create', side_effect=stripe.error.APIConnectionError('network down')):
            bill_err = auth_client.post('/api/billing/checkout', json={'package_type': 'battery_small'})
        assert bill_err.status_code == 400

        # 7. /api/export/factchecks
        exp_resp = auth_client.get('/api/export/factchecks?format=csv')
        assert exp_resp.status_code == 200

        assert self.SENTINEL_EMAIL not in caplog.text
        assert self.SENTINEL_CLAIM not in caplog.text
        assert self.SENTINEL_CONTEXT not in caplog.text

    def test_media_analyze_url_ssrf_block_scrubs_query_string(
        self, auth_client, caplog
    ):
        caplog.set_level(logging.DEBUG)
        resp = auth_client.post('/api/media/analyze-url', json={
            'url': 'https://127.0.0.1/video.mp4?secret_token=SECRET_QS_999',
        })
        assert resp.status_code == 400
        assert resp.get_json()['error'] == (
            'Invalid or restricted URL. The address cannot be resolved or is not accessible.'
        )
        assert 'secret_token' not in caplog.text
        assert 'SECRET_QS_999' not in caplog.text
        assert 'https://127.0.0.1/video.mp4' in caplog.text


class TestGoogleLoginOAuthHardening:
    """Verify PKCE S256 on Google login OAuth and clean access_denied handling."""

    def test_google_login_issues_pkce_s256_and_preserves_ext_redirect(self, client):
        from backend.extensions import oauth

        # Ensure google client is registered with S256 if test env lacked GOOGLE_CLIENT_ID
        if not getattr(oauth, 'google', None):
            oauth.register(
                name='google',
                client_id='test-google-client-id',
                client_secret='test-google-client-secret',
                server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                client_kwargs={
                    'scope': 'openid email profile',
                    'code_challenge_method': 'S256',
                },
            )

        assert oauth.google.client_kwargs.get('code_challenge_method') == 'S256'
        metadata = {
            'authorization_endpoint': 'https://accounts.google.com/o/oauth2/v2/auth',
            'token_endpoint': 'https://oauth2.googleapis.com/token',
        }
        with patch.object(oauth.google, 'load_server_metadata', return_value=metadata):
            resp = client.get('/api/auth/google?ext=1')

        assert resp.status_code == 302
        location = resp.headers['Location']
        parsed = urlparse(location)
        assert parsed.netloc == 'accounts.google.com'
        qs = parse_qs(parsed.query)
        assert qs.get('code_challenge_method') == ['S256']
        assert len(qs.get('code_challenge', [''])[0]) >= 32
        assert len(qs.get('state', [''])[0]) >= 8

        with client.session_transaction() as sess:
            assert sess.get('ext_redirect') == '1'
            # Authlib stores code_verifier inside the session state entry
            state_val = qs['state'][0]
            state_key = f'_state_google_{state_val}'
            assert state_key in sess
            assert 'code_verifier' in sess[state_key]['data']

    def test_server_registers_google_oauth_with_pkce_s256(self):
        """Verify server.py configures code_challenge_method='S256' in oauth.register."""
        from pathlib import Path
        server_src = (Path(__file__).resolve().parents[1] / 'backend' / 'server.py').read_text(encoding='utf-8')
        assert "'code_challenge_method': 'S256'" in server_src

    def test_google_callback_access_denied_redirects_cleanly_and_clears_ext_redirect(self, client):
        with client.session_transaction() as sess:
            sess['ext_redirect'] = '1'

        resp = client.get('/api/auth/google/callback?error=access_denied&state=test-state')
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith('/?error=access_denied')

        with client.session_transaction() as sess:
            assert 'ext_redirect' not in sess

    def test_google_login_without_ext_clears_stale_ext_redirect(self, client):
        from backend.extensions import oauth

        if not getattr(oauth, 'google', None):
            oauth.register(
                name='google',
                client_id='test-google-client-id',
                client_secret='test-google-client-secret',
                server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
                client_kwargs={
                    'scope': 'openid email profile',
                    'code_challenge_method': 'S256',
                },
            )

        with client.session_transaction() as sess:
            sess['ext_redirect'] = '1'

        metadata = {
            'authorization_endpoint': 'https://accounts.google.com/o/oauth2/v2/auth',
            'token_endpoint': 'https://oauth2.googleapis.com/token',
        }
        with patch.object(oauth.google, 'load_server_metadata', return_value=metadata):
            resp = client.get('/api/auth/google')

        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert 'ext_redirect' not in sess

    def test_google_callback_sanitizes_and_truncates_oauth_error_in_logs(self, client, caplog):
        caplog.set_level(logging.WARNING)
        crafted_error = 'bad\nerror\r\tINJECT_' + ('X' * 120)
        resp = client.get('/api/auth/google/callback', query_string={'error': crafted_error, 'state': 's1'})
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith('/?error=google_auth_failed')
        assert 'INJECT_' in caplog.text
        assert '\nerror' not in caplog.text
        assert ('X' * 70) not in caplog.text

    def test_google_callback_other_error_redirects_to_google_auth_failed_and_clears_ext(self, client):
        with client.session_transaction() as sess:
            sess['ext_redirect'] = '1'

        resp = client.get('/api/auth/google/callback?error=server_error&state=test-state')
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith('/?error=google_auth_failed')

        with client.session_transaction() as sess:
            assert 'ext_redirect' not in sess


class TestTTPRateLimitsAndErrorSanitization:
    """Verify rate limits on sensitive routes and pitchdeck error sanitization."""

    def test_delete_account_rate_limit_enforces_5_per_hour(self, client, db_session_fixture):
        pw_hash = bcrypt.hashpw(b'testpass123', bcrypt.gensalt(rounds=4)).decode('utf-8')

        for idx in range(6):
            email = f'ratelimit-del-{idx}@example.com'
            user = User(email=email, password_hash=pw_hash)
            db_session_fixture.add(user)
            db_session_fixture.commit()
            db_session_fixture.add(TokenBalance(user_id=user.id, balance=5))
            db_session_fixture.commit()

            login_resp = client.post('/api/auth/login', json={
                'email': email,
                'password': 'testpass123',
            })
            assert login_resp.status_code == 200

            del_resp = client.post('/api/auth/delete-account')
            if idx < 5:
                assert del_resp.status_code == 200
            else:
                assert del_resp.status_code == 429

    def test_create_checkout_session_rate_limit_enforces_10_per_minute(self, auth_client):
        for _ in range(10):
            resp = auth_client.post('/api/billing/checkout', json={'package_type': 'nonexistent'})
            assert resp.status_code == 400
        blocked = auth_client.post('/api/billing/checkout', json={'package_type': 'nonexistent'})
        assert blocked.status_code == 429

    def test_create_portal_session_rate_limit_enforces_10_per_minute(self, auth_client):
        for _ in range(10):
            resp = auth_client.post('/api/billing/create-portal', json={})
            assert resp.status_code == 400
        blocked = auth_client.post('/api/billing/create-portal', json={})
        assert blocked.status_code == 429

    def test_pitchdeck_analyze_sanitizes_unexpected_value_error_and_preserves_allowlisted(
        self, auth_client, test_user, db_session_fixture
    ):
        from backend.services import get_pitchdeck_service

        svc = get_pitchdeck_service()
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = ValueError('internal /app/secret/key.json leaked')
        pdf_b64 = base64.b64encode(_make_minimal_pdf_bytes()).decode('utf-8')

        with patch.object(svc, 'client', mock_client), \
             patch('time.sleep'):
            resp = auth_client.post('/api/pitchdeck/analyze', json={'pdf_data': pdf_b64})

        assert resp.status_code == 400
        payload = resp.get_json()
        assert payload['error'] == 'Pitch deck could not be processed. Please verify the PDF and try again.'
        assert '/app/secret' not in payload['error']

        # Balance must be refunded back to 100 CP
        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.balance == 100

        # Allowlisted ValueError from _parse_vision_response (invalid JSON -> Failed to parse analysis response) is preserved
        invalid_json_resp = MagicMock()
        invalid_json_resp.text = 'not valid json {'
        mock_client.models.generate_content.side_effect = None
        mock_client.models.generate_content.return_value = invalid_json_resp
        with patch.object(svc, 'client', mock_client), \
             patch('time.sleep'):
            allowlisted_resp = auth_client.post('/api/pitchdeck/analyze', json={'pdf_data': pdf_b64})
        assert allowlisted_resp.status_code == 400
        assert allowlisted_resp.get_json()['error'] == 'Failed to parse analysis response'

        # TimeoutError maps cleanly to 504 without leaking internals
        mock_client.models.generate_content.side_effect = requests.exceptions.Timeout('socket read timed out')
        with patch.object(svc, 'client', mock_client), \
             patch('time.sleep'):
            timeout_resp = auth_client.post('/api/pitchdeck/analyze', json={'pdf_data': pdf_b64})
        assert timeout_resp.status_code == 504
        assert timeout_resp.get_json()['error'] == 'Analysis timed out. Please try again.'

    def test_pitchdeck_analyze_blocks_crafted_prefix_value_error_and_allows_exact_patterns(
        self, auth_client, test_user, db_session_fixture
    ):
        from backend.services import get_pitchdeck_service

        svc = get_pitchdeck_service()
        mock_client = MagicMock()
        pdf_b64 = base64.b64encode(_make_minimal_pdf_bytes()).decode('utf-8')

        for crafted in (
            'File too large. SECRET_PATH=/var/run/secret',
            'Pitch deck exceeds maximum allowed size SECRET_PATH=/var/run/secret',
        ):
            mock_client.models.generate_content.side_effect = ValueError(crafted)
            with patch.object(svc, 'client', mock_client), \
                 patch('time.sleep'):
                resp = auth_client.post('/api/pitchdeck/analyze', json={'pdf_data': pdf_b64})

            assert resp.status_code == 400
            payload = resp.get_json()
            assert payload['error'] == 'Pitch deck could not be processed. Please verify the PDF and try again.'
            assert 'SECRET_PATH' not in payload['error']

        # Exact size/slide error messages matching the compiled patterns are preserved
        for valid_msg in (
            'File too large. Maximum size is 25MB',
            'Pitch deck exceeds maximum allowed size (120 slides). Maximum allowed is 100 slides.',
        ):
            mock_client.models.generate_content.side_effect = ValueError(valid_msg)
            with patch.object(svc, 'client', mock_client), \
                 patch('time.sleep'):
                resp = auth_client.post('/api/pitchdeck/analyze', json={'pdf_data': pdf_b64})

            assert resp.status_code == 400
            assert resp.get_json()['error'] == valid_msg

        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.balance == 100


class TestAuditLogAndConfigFoundation:
    """Verify AuditLog persistence across GDPR delete and Config OAuth/scope helpers."""

    def test_audit_log_records_security_events_and_survives_user_deletion(
        self, client, db_session_fixture, mock_stripe_webhook_payload
    ):
        # 1. Failed login records auth.login_failed
        fail_resp = client.post('/api/auth/login', json={
            'email': 'audit-missing@example.com',
            'password': 'wrong',
        })
        assert fail_resp.status_code == 401
        failed_log = db_session_fixture.query(AuditLog).filter_by(event_type='auth.login_failed').first()
        assert failed_log is not None
        assert failed_log.detail == 'reason=user_not_found'

        # 2. Signup records auth.signup
        signup_resp = client.post('/api/auth/signup', json={
            'email': 'audit-user@example.com',
            'password': 'StrongPassword123!',
        })
        assert signup_resp.status_code == 200
        user = db_session_fixture.query(User).filter_by(email='audit-user@example.com').one()
        uid = user.id
        signup_log = db_session_fixture.query(AuditLog).filter_by(
            user_id=uid, event_type='auth.signup'
        ).first()
        assert signup_log is not None

        # 3. Login records auth.login
        login_resp = client.post('/api/auth/login', json={
            'email': 'audit-user@example.com',
            'password': 'StrongPassword123!',
        })
        assert login_resp.status_code == 200
        login_log = db_session_fixture.query(AuditLog).filter_by(
            user_id=uid, event_type='auth.login'
        ).first()
        assert login_log is not None

        # 4. Stripe webhook records billing.webhook_fulfilled
        body, sig = mock_stripe_webhook_payload(user_id=uid, session_id='cs_audit_1')
        wh_resp = client.post(
            '/api/billing/webhook',
            data=body,
            content_type='application/json',
            headers={'Stripe-Signature': sig},
        )
        assert wh_resp.status_code == 200
        wh_log = db_session_fixture.query(AuditLog).filter_by(
            user_id=uid, event_type='billing.webhook_fulfilled'
        ).first()
        assert wh_log is not None
        assert wh_log.provider == 'stripe'

        # 5. Cleanup scheduler records scheduler.cleanup_expired
        sched_resp = client.post(
            '/api/factcheck/cleanup-expired',
            headers={'X-Scheduler-Secret': Config.SCHEDULER_SECRET},
        )
        assert sched_resp.status_code == 200
        sched_log = db_session_fixture.query(AuditLog).filter_by(
            event_type='scheduler.cleanup_expired'
        ).first()
        assert sched_log is not None

        # 6. Delete account succeeds without FK conflict and AuditLog entries survive
        del_resp = client.post('/api/auth/delete-account')
        assert del_resp.status_code == 200
        assert db_session_fixture.query(User).filter_by(id=uid).first() is None
        assert db_session_fixture.query(DeletedUser).count() == 1
        surviving_logs = db_session_fixture.query(AuditLog).filter_by(user_id=uid).all()
        surviving_types = {entry.event_type for entry in surviving_logs}
        assert surviving_types == {
            'auth.signup',
            'auth.login',
            'billing.webhook_fulfilled',
            'auth.delete_account',
        }

    def test_record_audit_event_truncates_detail_to_255_chars(self, db_session_fixture):
        entry = record_audit_event(
            db_session_fixture,
            'test.truncation',
            user_id=999,
            provider='google_drive',
            detail='x' * 400,
            commit=True,
        )
        assert entry.id is not None
        assert len(entry.detail) == 255
        assert entry.provider == 'google_drive'

    def test_config_validate_passes_when_optional_storage_keys_unset(self, monkeypatch):
        monkeypatch.setattr(Config, 'ENV', 'development')
        monkeypatch.setattr(Config, 'TEST_MODE', False)
        monkeypatch.setattr(Config, 'SECRET_KEY', 'secret')
        monkeypatch.setattr(Config, 'GEMINI_API_KEY', 'gem-key')
        monkeypatch.setattr(Config, 'STRIPE_SECRET_KEY', 'sk_test_123')
        monkeypatch.setattr(Config, 'GOOGLE_TOKEN_ENCRYPTION_KEY', None)
        monkeypatch.setattr(Config, 'DROPBOX_CLIENT_ID', None)
        monkeypatch.setattr(Config, 'DROPBOX_CLIENT_SECRET', None)
        monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_ID', None)
        monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_SECRET', None)
        monkeypatch.setattr(Config, 'CLOUD_OAUTH_REDIRECT_BASE', None)

        assert Config.validate() is None

    def test_config_get_google_drive_scopes_always_excludes_restricted_drive_readonly(self, monkeypatch):
        default_scopes = Config.get_google_drive_scopes()
        assert 'https://www.googleapis.com/auth/drive.readonly' not in default_scopes
        assert 'https://www.googleapis.com/auth/drive.file' in default_scopes
        assert 'https://www.googleapis.com/auth/spreadsheets' in default_scopes

        # Even if env var includes drive.readonly, get_google_drive_scopes strips it (Zero-CASA Stage 1)
        monkeypatch.setattr(
            Config,
            'GOOGLE_DRIVE_SCOPES',
            'https://www.googleapis.com/auth/drive.readonly '
            'https://www.googleapis.com/auth/drive.file '
            'https://www.googleapis.com/auth/spreadsheets',
        )
        sanitized_scopes = Config.get_google_drive_scopes()
        assert 'https://www.googleapis.com/auth/drive.readonly' not in sanitized_scopes
        assert 'https://www.googleapis.com/auth/drive.file' in sanitized_scopes
        assert 'https://www.googleapis.com/auth/spreadsheets' in sanitized_scopes
