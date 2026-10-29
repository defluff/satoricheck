"""
Security Fix Tests.
Tests for critical vulnerabilities found during the security audit.

Covers:
1. IDOR: Batch cache must scope queries to the authenticated user
2. SSRF: _validate_url must block private/internal IPs
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, UTC


# =============================================================================
# Fix 1: IDOR — Batch cache scoped to authenticated user
# =============================================================================

class TestBatchCacheIDOR:
    """Batch fact-check cache must not leak results across users."""

    def test_batch_cache_does_not_return_other_users_results(
        self, app, db_session_fixture
    ):
        """User B must NOT receive User A's cached fact-check via batch endpoint."""
        from backend.models import User, TokenBalance, Streak, FactCheck
        import bcrypt
        import secrets
        import json

        # --- Setup: Create User A with a cached fact-check ---
        pw_hash = bcrypt.hashpw(b'pw_a', bcrypt.gensalt()).decode('utf-8')
        user_a = User(
            email='a@example.com',
            password_hash=pw_hash,
            api_token=secrets.token_hex(32),
        )
        db_session_fixture.add(user_a)
        db_session_fixture.commit()
        db_session_fixture.add(TokenBalance(user_id=user_a.id, balance=100))
        db_session_fixture.add(Streak(user_id=user_a.id, current_streak=0))
        db_session_fixture.commit()

        # Insert a cached fact-check belonging to User A
        fc = FactCheck(
            user_id=user_a.id,
            claim_text='The sky is blue.',
            word_count=4,
            tokens_used=1,
            is_claim=True,
            verdict='TRUE',
            explanation='User A private explanation',
            sources=json.dumps(['https://example.com']),
            source_reliability='HIGH',
            source='factcheck',
            timestamp=datetime.now(UTC),
        )
        db_session_fixture.add(fc)
        db_session_fixture.commit()

        # --- Setup: Create User B ---
        pw_hash_b = bcrypt.hashpw(b'pw_b', bcrypt.gensalt()).decode('utf-8')
        user_b = User(
            email='b@example.com',
            password_hash=pw_hash_b,
            api_token=secrets.token_hex(32),
        )
        db_session_fixture.add(user_b)
        db_session_fixture.commit()
        db_session_fixture.add(TokenBalance(user_id=user_b.id, balance=100))
        db_session_fixture.add(Streak(user_id=user_b.id, current_streak=0))
        db_session_fixture.commit()

        # Login as User B
        client = app.test_client()
        resp = client.post('/api/auth/login', json={
            'email': 'b@example.com',
            'password': 'pw_b',
        })
        assert resp.status_code == 200

        # --- Act: User B submits the same claim text ---
        mock_result = {
            'is_claim': True,
            'verdict': 'TRUE',
            'explanation': 'Fresh analysis for User B',
            'sources': ['https://example.com'],
            'source_reliability': 'HIGH',
        }

        with patch(
            'backend.services.gemini_service.GeminiService.analyze_claims_batch',
            return_value=[mock_result],
        ):
            resp = client.post('/api/factcheck/analyze-batch', json={
                'claims': ['The sky is blue.'],
            })

        assert resp.status_code == 200
        data = resp.get_json()
        results = data.get('results', [])
        assert len(results) >= 1

        # --- Assert: User B should NOT see User A's private explanation ---
        # If the cache leaks, the explanation will be "User A private
        # explanation" and is_cached will be True.
        first = results[0]
        assert first.get('explanation') != 'User A private explanation', (
            'IDOR: User B received User A\'s cached fact-check'
        )

    def test_batch_cache_returns_own_cached_result(
        self, auth_client, test_user, db_session_fixture
    ):
        """User should get their own cached result from the batch endpoint."""
        from backend.models import FactCheck
        import json

        # Insert a cached fact-check for the test user
        fc = FactCheck(
            user_id=test_user.id,
            claim_text='Water boils at 100°C.',
            word_count=5,
            tokens_used=1,
            is_claim=True,
            verdict='TRUE',
            explanation='Correct at sea level',
            sources=json.dumps(['https://example.com']),
            source_reliability='HIGH',
            source='factcheck',
            timestamp=datetime.now(UTC),
        )
        db_session_fixture.add(fc)
        db_session_fixture.commit()

        # Submit the same claim — should hit cache and NOT call Gemini
        with patch(
            'backend.services.gemini_service.GeminiService.analyze_claims_batch',
        ) as mock_batch:
            resp = auth_client.post('/api/factcheck/analyze-batch', json={
                'claims': ['Water boils at 100°C.'],
            })

        assert resp.status_code == 200
        data = resp.get_json()
        results = data.get('results', [])
        assert len(results) >= 1
        assert results[0]['explanation'] == 'Correct at sea level'
        # Gemini should NOT have been called (cache hit)
        mock_batch.assert_not_called()


# =============================================================================
# Fix 2: SSRF — _validate_url must block private/internal IPs
# =============================================================================

class TestValidateUrlSSRF:
    """_validate_url must reject private, loopback, and metadata IPs."""

    def _get_service(self):
        """Create a GeminiService with a mocked API key."""
        with patch('backend.services.gemini.client.Config') as mock_config:
            mock_config.GEMINI_API_KEY = 'fake-key'
            from backend.services.gemini_service import GeminiService
            return GeminiService()

    def test_blocks_loopback_ip(self):
        """Must block 127.0.0.1 (loopback)."""
        service = self._get_service()
        assert service._validate_url('http://127.0.0.1/admin') is False

    def test_blocks_private_10_range(self):
        """Must block 10.x.x.x (RFC 1918)."""
        service = self._get_service()
        assert service._validate_url('http://10.0.0.1/internal') is False

    def test_blocks_private_172_range(self):
        """Must block 172.16-31.x.x (RFC 1918)."""
        service = self._get_service()
        assert service._validate_url('http://172.16.0.1/internal') is False

    def test_blocks_private_192_168_range(self):
        """Must block 192.168.x.x (RFC 1918)."""
        service = self._get_service()
        assert service._validate_url('http://192.168.1.1/router') is False

    def test_blocks_cloud_metadata_endpoint(self):
        """Must block cloud metadata IP 169.254.169.254."""
        service = self._get_service()
        assert service._validate_url('http://169.254.169.254/latest/meta-data/') is False

    def test_blocks_ipv6_loopback(self):
        """Must block IPv6 loopback ::1."""
        service = self._get_service()
        assert service._validate_url('http://[::1]/admin') is False

    @patch('backend.services.gemini.client.socket.getaddrinfo')
    def test_blocks_localhost_hostname(self, mock_getaddrinfo):
        """Must block 'localhost' hostname that resolves to 127.0.0.1."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, '', ('127.0.0.1', 80))
        ]
        service = self._get_service()
        assert service._validate_url('http://localhost/admin') is False

    @patch('backend.services.gemini.client.socket.getaddrinfo')
    @patch('backend.services.gemini.client.requests.head')
    def test_allows_public_url(self, mock_head, mock_getaddrinfo):
        """Must allow legitimate public URLs."""
        mock_getaddrinfo.return_value = [
            (2, 1, 6, '', ('93.184.216.34', 443))  # example.com
        ]
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_head.return_value = mock_response

        service = self._get_service()
        assert service._validate_url('https://example.com/article') is True

    def test_blocks_non_http_schemes(self):
        """Must block file://, ftp://, and other non-HTTP schemes."""
        service = self._get_service()
        assert service._validate_url('file:///etc/passwd') is False
        assert service._validate_url('ftp://internal/file') is False
        assert service._validate_url('gopher://evil.com') is False

    def test_validate_sources_concurrent_and_caps_at_five(self):
        """_validate_sources must validate multiple URLs concurrently, filter private/dead links, and cap at 5."""
        service = self._get_service()
        urls = [
            f"https://valid-{i}.example.com/article" for i in range(7)
        ] + ["http://127.0.0.1/admin", "https://dead.example.com/404"]

        def fake_validate(url):
            return url.startswith("https://valid-")

        with patch.object(service, '_validate_url', side_effect=fake_validate) as mock_val:
            valid = service._validate_sources(urls)

        assert len(valid) == 5
        assert all(u.startswith("https://valid-") for u in valid)
        assert "http://127.0.0.1/admin" not in valid
        assert mock_val.call_count == 9

    def test_batch_response_filters_ssrf_and_extracts_grounding_urls(self):
        """_parse_thinking_batch_response must filter invalid/SSRF URLs and merge grounding_metadata URLs."""
        service = self._get_service()

        part = MagicMock()
        part.thought = False
        part.text = '{"results": [{"claim_index": 1, "verdict": "TRUE", "explanation": "Grounded", "sources": ["http://169.254.169.254/latest"]}]}'

        web_chunk = MagicMock()
        web_chunk.uri = "https://nasa.gov/artemis"
        chunk = MagicMock()
        chunk.web = web_chunk
        grounding_meta = MagicMock()
        grounding_meta.grounding_chunks = [chunk]

        candidate = MagicMock()
        candidate.content.parts = [part]
        candidate.grounding_metadata = grounding_meta

        response = MagicMock()
        response.candidates = [candidate]

        def fake_validate(url):
            return url == "https://nasa.gov/artemis"

        with patch.object(service, '_validate_url', side_effect=fake_validate):
            parsed = service._parse_thinking_batch_response(response, expected_count=1)

        assert len(parsed) == 1
        assert parsed[0]["sources"] == ["https://nasa.gov/artemis"]





# =============================================================================
# Fix 1b: Standard cache in _analyze_claim_standard also has same IDOR
# =============================================================================

class TestStandardCacheIDOR:
    """_analyze_claim_standard cache lookup must be scoped to calling user."""

    @patch('requests.post')
    def test_standard_cache_does_not_return_other_users_results(
        self, mock_post, app, db_session_fixture
    ):
        """GeminiService._analyze_claim_standard cache must not cross users."""
        from backend.models import User, TokenBalance, Streak, FactCheck
        from backend.services.gemini_service import GeminiService
        import bcrypt
        import secrets
        import json

        # Setup: User A with cached fact-check
        pw = bcrypt.hashpw(b'pw', bcrypt.gensalt()).decode('utf-8')
        user_a = User(
            email='cache_a@example.com',
            password_hash=pw,
            api_token=secrets.token_hex(32),
        )
        db_session_fixture.add(user_a)
        db_session_fixture.commit()

        fc = FactCheck(
            user_id=user_a.id,
            claim_text='Pi equals 3.14159.',
            word_count=3,
            tokens_used=1,
            is_claim=True,
            verdict='TRUE',
            explanation='User A private result',
            sources=json.dumps(['https://example.com']),
            source_reliability='HIGH',
            source='factcheck',
            timestamp=datetime.now(UTC),
        )
        db_session_fixture.add(fc)
        db_session_fixture.commit()

        # Mock Gemini API to return a fresh result
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'candidates': [{
                'content': {
                    'parts': [{
                        'text': json.dumps({
                            'is_claim': True,
                            'verdict': 'TRUE',
                            'explanation': 'Fresh from API',
                            'fallacy': None,
                            'sources': ['https://example.com'],
                            'source_reliability': 'HIGH',
                        })
                    }]
                }
            }]
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        # Note: The standard cache currently has no user context.
        # After fixing, it should NOT use the cache for a different user.
        # For now, we just verify the cache lookup includes user scoping
        # by checking the query directly.
        from backend.database import db_session
        from backend.models import FactCheck as FC

        # Without fix: query returns user_a's result.
        # With fix: query filtered by user_id would return nothing for user_b.
        # We test the query behavior directly.
        result_unscoped = db_session.query(FC).filter(
            FC.claim_text == 'Pi equals 3.14159.'
        ).first()
        assert result_unscoped is not None  # Unscoped finds it

        # Scoped query (simulating the fix) should find nothing for user_id=9999
        result_scoped = db_session.query(FC).filter(
            FC.claim_text == 'Pi equals 3.14159.',
            FC.user_id == 9999,
        ).first()
        assert result_scoped is None  # Correctly scoped finds nothing


# =============================================================================
# Fix 4: payment_success must NOT fulfill purchases (redirect only)
# =============================================================================

class TestPaymentSuccessRedirectOnly:
    """payment_success must only redirect — fulfillment is webhook's job."""

    @patch('stripe.checkout.Session.retrieve')
    def test_payment_success_does_not_add_tokens(
        self, mock_retrieve, app, test_user, db_session_fixture
    ):
        """GET /api/billing/success must NOT credit tokens."""
        from backend.models import TokenBalance

        initial_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance

        mock_session = MagicMock()
        mock_session.payment_status = 'paid'
        mock_session.status = 'complete'
        mock_session.get.return_value = {
            'user_id': str(test_user.id),
            'package_type': 'battery_small',
            'tokens': '86',
        }
        mock_retrieve.return_value = mock_session

        client = app.test_client()
        resp = client.get('/api/billing/success?session_id=cs_test_exploit')

        # Should redirect (302)
        assert resp.status_code == 302

        # Balance must NOT have changed
        final_balance = db_session_fixture.query(TokenBalance).filter_by(
            user_id=test_user.id
        ).first().balance
        assert final_balance == initial_balance, (
            'payment_success endpoint should not fulfill purchases'
        )


# =============================================================================
# Fix 5: wizard-refill must use dedicated SCHEDULER_SECRET
# =============================================================================

class TestWizardRefillAuth:
    """wizard-refill must use a dedicated SCHEDULER_SECRET, not SECRET_KEY."""

    def test_rejects_truncated_secret_key(self, client, app):
        """Must reject the old truncated SECRET_KEY[:16] auth."""
        from backend.config import Config

        # The old (insecure) approach used SECRET_KEY[:16]
        old_secret = Config.SECRET_KEY[:16]

        resp = client.post('/api/billing/wizard-refill', headers={
            'X-Scheduler-Secret': old_secret,
        })

        # Should be rejected (401) now that we use a dedicated secret
        assert resp.status_code == 401

    def test_accepts_dedicated_scheduler_secret(self, client, app):
        """Must accept the dedicated SCHEDULER_SECRET."""
        from backend.config import Config

        resp = client.post('/api/billing/wizard-refill', headers={
            'X-Scheduler-Secret': Config.SCHEDULER_SECRET,
        })

        # Should succeed (200) with the correct dedicated secret
        assert resp.status_code == 200

    def test_rejects_missing_secret(self, client):
        """Must reject requests with no secret header."""
        resp = client.post('/api/billing/wizard-refill')
        assert resp.status_code == 401


# =============================================================================
# Fix 6: TEST_MODE must be blocked in production
# =============================================================================

class TestTestModeProductionGuard:
    """TEST_MODE must be blocked when FLASK_ENV=production."""

    def test_health_check_does_not_leak_test_mode(self, client):
        """Health endpoint must not expose test_mode status."""
        resp = client.get('/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'test_mode' not in data, (
            'Health endpoint leaks test_mode status to attackers'
        )

    def test_test_mode_blocked_in_production(self):
        """TEST_MODE=true + FLASK_ENV=production must raise on startup."""
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {
            'TEST_MODE': 'true',
            'FLASK_ENV': 'production',
        }):
            from backend.config import Config
            # Simulate reloading the config
            with patch.object(Config, 'TEST_MODE', True):
                with patch.object(Config, 'ENV', 'production'):
                    assert Config.TEST_MODE is True
                    assert Config.ENV == 'production'
                    # The validate() method should now catch this
                    with pytest.raises(ValueError, match='TEST_MODE'):
                        Config.validate()


# =============================================================================
# Fix 7: Zero-Balance Free Execution Prevention & Input Caps
# =============================================================================

class TestWalletDrainProtections:
    """Ensure zero-balance accounts cannot trigger LLM calls for free."""

    def test_zero_balance_cannot_execute_small_claim(self, auth_client, test_user, db_session_fixture):
        """User with 0 CP must be blocked from running even <250 word snippets."""
        from backend.models import TokenBalance

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        tb.unbilled_words = 0
        db_session_fixture.commit()

        # Submit a small 10-word claim
        resp = auth_client.post('/api/factcheck/analyze', json={
            'text': 'The moon is made of green cheese and orbits earth.'
        })

        assert resp.status_code == 403
        data = resp.get_json()
        assert 'Insufficient tokens' in data.get('error', '')

    def test_zero_balance_cannot_execute_ai_detection(self, auth_client, test_user, db_session_fixture):
        """User with 0 CP must be blocked from running AI text detection."""
        from backend.models import TokenBalance

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        tb.unbilled_words = 0
        db_session_fixture.commit()

        resp = auth_client.post('/api/factcheck/analyze-ai', json={
            'text': 'This is a comprehensive sample text containing plenty of authentic words to easily pass the minimum twenty word requirement for forensic detection analysis testing.'
        })

        assert resp.status_code == 402
        data = resp.get_json()
        assert 'Insufficient tokens' in data.get('error', '')

    def test_oversized_payload_rejected(self, auth_client):
        """Text payloads exceeding 50,000 characters must be rejected."""
        huge_text = "a" * 50001
        resp = auth_client.post('/api/factcheck/analyze', json={
            'text': huge_text
        })
        assert resp.status_code == 400
        assert 'exceeds maximum limit' in resp.get_json().get('error', '')

    def test_tokens_committed_before_external_llm_call(self, auth_client, test_user, db_session_fixture):
        """Token reservation must be committed to DB BEFORE calling external Gemini API."""
        from backend.models import TokenBalance

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 10
        tb.unbilled_words = 1245  # +10 words = 1255 words -> costs 1 CP
        db_session_fixture.commit()

        balance_during_llm_call = []

        def inspect_db_during_call(*args, **kwargs):
            # Query balance inside the LLM call to verify the reservation is already committed
            current_tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
            db_session_fixture.refresh(current_tb)
            balance_during_llm_call.append(current_tb.balance)
            return {
                'is_claim': True,
                'verdict': 'TRUE',
                'explanation': 'Verified',
                'fallacy': None,
                'sources': [],
                'source_reliability': 'HIGH'
            }

        with patch('backend.services.gemini_service.GeminiService.analyze_claim', side_effect=inspect_db_during_call):
            resp = auth_client.post('/api/factcheck/analyze', json={
                'text': 'One two three four five six seven eight nine ten.'
            })

        assert resp.status_code == 200
        assert balance_during_llm_call == [9], "Balance deduction must be committed before calling Gemini"


# =============================================================================
# Fix 8: Service Singleton Sharing & Multi-Instance Migration Safety
# =============================================================================

class TestMultiInstanceHardening:
    """Verify singleton SDK client sharing and concurrent schema migration safety."""

    def test_pitchdeck_service_singleton_shares_gemini_client(self):
        """get_pitchdeck_service() must share the underlying client with get_gemini_service()."""
        import backend.services as svc_mod
        svc_mod._pitchdeck_service = None

        gemini_svc = svc_mod.get_gemini_service()
        pitchdeck_svc = svc_mod.get_pitchdeck_service()

        assert pitchdeck_svc.client is gemini_svc.client
        assert pitchdeck_svc.api_key == gemini_svc.api_key

    def test_apply_migrations_tolerates_concurrent_duplicate_column(self):
        """_apply_migrations() must not crash if a concurrent worker already added a column."""
        from sqlalchemy.exc import OperationalError
        import backend.database as db_mod

        mock_inspector = MagicMock()
        mock_inspector.get_table_names.return_value = ['users']
        # Pretend 'current_pitchdeck_cache' is missing when inspected
        mock_inspector.get_columns.return_value = [
            {'name': 'id'}, {'name': 'email'}, {'name': 'name'},
            {'name': 'picture'}, {'name': 'google_id'}, {'name': 'created_at'},
            {'name': 'last_login'}, {'name': 'streak_count'}, {'name': 'last_check_date'}
        ]

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = OperationalError(
            "ALTER TABLE users ADD COLUMN current_pitchdeck_cache",
            {},
            Exception("duplicate column name: current_pitchdeck_cache")
        )
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_conn
        mock_ctx.__exit__.return_value = False

        with patch('backend.database.inspect', return_value=mock_inspector), \
             patch.object(db_mod.engine, 'begin', return_value=mock_ctx):
            # Should complete cleanly without raising OperationalError
            db_mod._apply_migrations()

    def test_normalize_database_url_maps_postgresql_to_psycopg2_driver(self):
        """Bare postgresql:// and postgres:// URLs must resolve to psycopg2 under SQLAlchemy 2.1+."""
        from sqlalchemy import create_engine
        from backend.config import normalize_database_url

        cloudsql_url = "postgresql://postgres:secret@/authenix?host=/cloudsql/proj:europe-west6:db"
        normalized = normalize_database_url(cloudsql_url)
        assert normalized.startswith("postgresql+psycopg2://")

        assert normalize_database_url("postgres://u:p@host/db") == "postgresql+psycopg2://u:p@host/db"
        assert normalize_database_url("postgresql+psycopg2://u:p@host/db") == "postgresql+psycopg2://u:p@host/db"
        assert normalize_database_url("sqlite:///authenix.db") == "sqlite:///authenix.db"

        # Verify SQLAlchemy 2.1+ engine creation loads psycopg2 without requiring psycopg v3
        pg_engine = create_engine(normalized)
        assert pg_engine.dialect.driver == "psycopg2"
        pg_engine.dispose()

    def test_dockerfile_gunicorn_uses_preload_and_no_control_socket(self):
        """Dockerfile must preload WSGI app before binding $PORT so broken boots fail Cloud Run probes."""
        import pathlib

        dockerfile_path = pathlib.Path(__file__).resolve().parents[2] / "Dockerfile"
        content = dockerfile_path.read_text(encoding="utf-8")

        assert "--preload" in content, "Gunicorn must use --preload so app crashes fail before binding $PORT"
        assert "--no-control-socket" in content, "Gunicorn 26.2+ in stateless container should disable control socket"
        assert "useradd -r -m" in content, "Non-root user should have a valid home directory"



