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
        from backend.services import get_gemini_service
        
        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        db_session_fixture.commit()
        
        mock_generate = mocker.patch.object(
            get_gemini_service().client.models, 'generate_content'
        )
        long_text = "word " * 2000  # Long text still requires 1 CP flat
        
        response = auth_client.post('/api/factcheck/analyze', json={
            'text': long_text
        })
        
        assert response.status_code == 403
        assert 'Insufficient tokens' in response.get_json()['error']
        mock_generate.assert_not_called()
        assert db_session_fixture.query(FactCheck).filter_by(user_id=test_user.id).count() == 0

    def test_factcheck_single_claim_deducts_flat_1_cp(self, auth_client, test_user, db_session_fixture, mock_gemini):
        """Standalone /api/factcheck/analyze must deduct 1 CP flat even for short claims."""
        from backend.models import TokenBalance

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 10
        db_session_fixture.commit()

        response = auth_client.post('/api/factcheck/analyze', json={
            'text': 'The Earth is round.'
        })

        assert response.status_code == 200
        data = response.get_json()
        assert data['result']['tokens_used'] == 1
        assert data['new_balance'] == 9

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == 9

    def test_analyze_ai_refunds_tokens_on_upstream_gemini_error(
        self, auth_client, test_user, db_session_fixture, mocker
    ):
        """Upstream Gemini errors in /api/factcheck/analyze-ai must propagate and refund reserved CP."""
        from backend.models import TokenBalance
        from backend.services import get_gemini_service

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 10
        db_session_fixture.commit()

        mock_generate = mocker.patch.object(
            get_gemini_service().client.models,
            'generate_content',
            side_effect=RuntimeError('503 UNAVAILABLE'),
        )

        sample_text = ' '.join(['sentence'] * 25)
        response = auth_client.post('/api/factcheck/analyze-ai', json={'text': sample_text})

        assert response.status_code == 503
        assert response.get_json()['success'] is False
        mock_generate.assert_called_once()

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == 10

    def test_analyze_ai_short_text_deducts_flat_1_cp(
        self, auth_client, test_user, db_session_fixture, mocker
    ):
        """Short valid text (25 words) on /analyze-ai must deduct exact 1 CP flat."""
        import json
        from google.genai import types
        from backend.models import TokenBalance
        from backend.services import get_gemini_service

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 10
        db_session_fixture.commit()

        ai_payload = {
            'ai_probability': 15,
            'confidence': 'MEDIUM',
            'detected_register': 'general',
            'is_short_text': True,
            'ai_indicators': [],
            'human_indicators': ['varied phrasing'],
            'explanation': 'Likely human-written text.',
        }
        sdk_response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role='model',
                        parts=[types.Part.from_text(text=json.dumps(ai_payload))],
                    )
                )
            ]
        )
        mock_generate = mocker.patch.object(
            get_gemini_service().client.models,
            'generate_content',
            return_value=sdk_response,
        )

        sample_text = ' '.join(['sentence'] * 25)
        response = auth_client.post('/api/factcheck/analyze-ai', json={'text': sample_text})

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['tokens_used'] == 1
        assert data['new_balance'] == 9
        mock_generate.assert_called_once()

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == 9

    def test_analyze_batch_short_uncached_claims_deducts_flat_1_cp(
        self, auth_client, test_user, db_session_fixture, mocker
    ):
        """1-8 short uncached claims on /analyze-batch must deduct exact 1 CP flat."""
        import json
        from google.genai import types
        from backend.models import FactCheck, TokenBalance
        from backend.services import get_gemini_service

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 10
        db_session_fixture.commit()

        batch_payload = {
            'results': [
                {
                    'claim_index': i + 1,
                    'is_claim': True,
                    'verdict': 'TRUE',
                    'explanation': f'Verified claim {i + 1}.',
                    'sources': [],
                }
                for i in range(3)
            ]
        }
        sdk_response = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role='model',
                        parts=[types.Part.from_text(text=json.dumps(batch_payload))],
                    )
                )
            ]
        )
        mock_generate = mocker.patch.object(
            get_gemini_service().client.models,
            'generate_content',
            return_value=sdk_response,
        )

        claims = ['The Earth orbits the Sun.', 'Water freezes at 0C.', 'Oxygen has atomic number 8.']
        response = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': claims,
            'pre_triaged': True,
        })

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['cost_incurred'] == 1
        assert data['new_balance'] == 9
        assert len(data['results']) == 3
        mock_generate.assert_called_once()

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == 9

        saved = db_session_fixture.query(FactCheck).filter_by(user_id=test_user.id).all()
        assert len(saved) == 3
        assert all(isinstance(fc.tokens_used, int) for fc in saved)
        assert sum(fc.tokens_used for fc in saved) == 1

    def test_analyze_batch_nine_uncached_claims_deducts_2_cp(
        self, auth_client, test_user, db_session_fixture, mocker
    ):
        """9 uncached claims (> MAX_CLAIMS_PER_PROMPT=8) must deduct exact 2 CP flat."""
        import json
        from google.genai import types
        from backend.models import FactCheck, TokenBalance
        from backend.services import get_gemini_service

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 10
        db_session_fixture.commit()

        sub_batch_1 = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role='model',
                        parts=[types.Part.from_text(text=json.dumps({
                            'results': [
                                {'claim_index': i + 1, 'is_claim': True, 'verdict': 'TRUE', 'explanation': 'Ok', 'sources': []}
                                for i in range(8)
                            ]
                        }))],
                    )
                )
            ]
        )
        sub_batch_2 = types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role='model',
                        parts=[types.Part.from_text(text=json.dumps({
                            'results': [
                                {'claim_index': 1, 'is_claim': True, 'verdict': 'TRUE', 'explanation': 'Ok', 'sources': []}
                            ]
                        }))],
                    )
                )
            ]
        )
        mock_generate = mocker.patch.object(
            get_gemini_service().client.models,
            'generate_content',
            side_effect=[sub_batch_1, sub_batch_2],
        )

        claims = [f'Fact claim number {i}.' for i in range(1, 10)]
        response = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': claims,
            'pre_triaged': True,
        })

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['cost_incurred'] == 2
        assert data['new_balance'] == 8
        assert len(data['results']) == 9
        assert mock_generate.call_count == 2

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == 8

        saved = db_session_fixture.query(FactCheck).filter_by(user_id=test_user.id).all()
        assert len(saved) == 9
        assert sum(fc.tokens_used for fc in saved) == 2

    def test_analyze_batch_all_cached_deducts_0_cp(
        self, auth_client, test_user, db_session_fixture, mocker
    ):
        """When all claims hit the DB cache, /analyze-batch must deduct 0 CP and skip Gemini."""
        import json
        from datetime import datetime, UTC
        from backend.models import FactCheck, TokenBalance
        from backend.services import get_gemini_service

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 10
        db_session_fixture.commit()

        db_session_fixture.add(FactCheck(
            user_id=test_user.id,
            claim_text='Cached claim A.',
            word_count=3,
            tokens_used=1,
            is_claim=True,
            verdict='TRUE',
            explanation='Already verified.',
            sources=json.dumps([]),
            source_reliability='HIGH',
            source='factcheck',
            timestamp=datetime.now(UTC),
        ))
        db_session_fixture.commit()

        mock_generate = mocker.patch.object(
            get_gemini_service().client.models,
            'generate_content',
        )

        response = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': ['Cached claim A.'],
            'pre_triaged': True,
        })

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert data['cost_incurred'] == 0
        assert data['new_balance'] == 10
        assert data['results'][0]['tokens_used'] == 0
        mock_generate.assert_not_called()

        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == 10

    def test_analyze_batch_rejects_oversized_context_and_invalid_claims(
        self, auth_client, test_user, db_session_fixture, mocker
    ):
        """/analyze-batch must reject oversized context/claims and non-string claims before billing or calling Gemini."""
        from backend.models import TokenBalance
        from backend.services import get_gemini_service

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 10
        db_session_fixture.commit()

        mock_generate = mocker.patch.object(
            get_gemini_service().client.models,
            'generate_content',
        )

        resp_ctx = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': ['Valid short claim.'],
            'context': 'x' * 50001,
        })
        assert resp_ctx.status_code == 400

        resp_type = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': [12345],
        })
        assert resp_type.status_code == 400

        resp_total = auth_client.post('/api/factcheck/analyze-batch', json={
            'claims': ['a' * 30000, 'b' * 30000],
        })
        assert resp_total.status_code == 400

        mock_generate.assert_not_called()
        db_session_fixture.expire_all()
        refreshed = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert refreshed.balance == 10

    def test_reserve_flat_tokens_respects_custom_error_status(
        self, app, test_user, db_session_fixture
    ):
        """reserve_flat_tokens must raise APIError with requested error_status and formatted balance."""
        import pytest
        from backend.error_handlers import APIError
        from backend.models import TokenBalance
        from backend.routes.tokens import reserve_flat_tokens

        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        tb.balance = 0
        db_session_fixture.commit()

        with pytest.raises(APIError) as exc_info:
            reserve_flat_tokens(test_user.id, 1, error_status=402)

        assert exc_info.value.status_code == 402
        assert exc_info.value.message == 'Insufficient tokens. Need 1 CP, have 0 CP'

    def test_frontend_handles_402_and_opens_buy_tokens_modal(self):
        """Frontend API batch loop and factcheck handlers must treat HTTP 402 like 403 and open buy-tokens-modal."""
        import pathlib
        import re

        js_dir = pathlib.Path(__file__).resolve().parents[1] / 'frontend' / 'js'
        index_html = (pathlib.Path(__file__).resolve().parents[1] / 'frontend' / 'index.html').read_text(encoding='utf-8')
        api_js = (js_dir / 'api.js').read_text(encoding='utf-8')
        factcheck_js = (js_dir / 'factcheck.js').read_text(encoding='utf-8')

        assert 'id="buy-tokens-modal"' in index_html

        batch_fn = re.search(
            r'async analyzeBatch\(.*?\)\s*\{(?P<body>.*?)^\s*async identifyClaims\(',
            api_js,
            re.DOTALL | re.MULTILINE,
        )
        assert batch_fn is not None
        batch_body = batch_fn.group('body')
        assert 'error.status === 402' in batch_body
        assert 'error.status === 403' in batch_body
        assert 'error.status === 401' in batch_body
        assert 'throw error;' in batch_body

        step1_match = re.search(
            r'if \(!identifiedClaims\)\s*\{(?P<body>.*?)^\s*// Step 2:',
            factcheck_js,
            re.DOTALL | re.MULTILINE,
        )
        assert step1_match is not None
        step1_body = step1_match.group('body')
        assert 'lastError.status === 402' in step1_body
        assert 'lastError.status === 403' in step1_body
        assert "ui.showModal('buy-tokens-modal')" in step1_body

        chunk_catch_match = re.search(
            r'// Stop on auth/payment errors(?P<body>.*?)Verified \$\{processedCount\} claims',
            factcheck_js,
            re.DOTALL,
        )
        assert chunk_catch_match is not None
        chunk_catch_body = chunk_catch_match.group('body')
        assert 'error.status === 402' in chunk_catch_body
        assert 'error.status === 403' in chunk_catch_body
        assert 'error.status === 401' in chunk_catch_body
        assert "ui.showModal('buy-tokens-modal')" in chunk_catch_body
        assert 'break;' in chunk_catch_body

        ai_check_match = re.search(
            r'async handleAICheckWithText\(.*?\)\s*\{(?P<body>.*?)export default',
            factcheck_js,
            re.DOTALL,
        )
        assert ai_check_match is not None
        ai_check_body = ai_check_match.group('body')
        assert 'error.status === 402' in ai_check_body
        assert 'error.status === 403' in ai_check_body
        assert "ui.showModal('buy-tokens-modal')" in ai_check_body


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

