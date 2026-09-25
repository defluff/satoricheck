"""
Tests for live audio stream verification route (/api/factcheck/stream-audio).
Validates authentication, token billing, single-pass transcription + claim extraction,
session deduplication, Flash triage filtering, and cross-user global cache hits.
"""
import io
import json
import pytest
from unittest.mock import patch
from datetime import datetime, UTC
from backend.models import FactCheck, TokenBalance, User


class TestStreamAudioRoute:
    """Test suite for POST /api/factcheck/stream-audio."""

    def test_stream_audio_requires_auth(self, client):
        """Unauthenticated requests must be rejected with 401."""
        response = client.post(
            '/api/factcheck/stream-audio',
            data={'audio': (io.BytesIO(b'fake audio bytes'), 'test.webm', 'audio/webm')},
            content_type='multipart/form-data'
        )
        assert response.status_code == 401

    def test_stream_audio_insufficient_balance(self, auth_client, test_user, db_session_fixture):
        """Users with balance < 1 CP should receive 403 INSUFFICIENT_FUNDS."""
        tb = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).first()
        tb.balance = 0
        db_session_fixture.commit()

        response = auth_client.post(
            '/api/factcheck/stream-audio',
            data={'audio': (io.BytesIO(b'fake audio bytes'), 'test.webm', 'audio/webm')},
            content_type='multipart/form-data'
        )
        assert response.status_code == 403
        data = response.get_json()
        assert data['success'] is False
        assert data.get('code') == 'INSUFFICIENT_FUNDS' or 'Insufficient tokens' in data.get('error', '')

    def test_stream_audio_no_data_rejected(self, auth_client):
        """Requests without audio data should return 400."""
        response = auth_client.post(
            '/api/factcheck/stream-audio',
            data={},
            content_type='multipart/form-data'
        )
        assert response.status_code == 400

    def test_stream_audio_unsupported_mime_rejected(self, auth_client):
        """Non-audio mime types must return 400."""
        response = auth_client.post(
            '/api/factcheck/stream-audio',
            data={'audio': (io.BytesIO(b'image data'), 'image.png', 'image/png')},
            content_type='multipart/form-data'
        )
        assert response.status_code == 400
        assert 'Unsupported audio format' in response.get_json().get('error', '')

    @patch('backend.routes.factcheck.get_gemini_service')
    def test_stream_audio_success_pipeline(self, mock_get_gemini, auth_client, test_user, db_session_fixture):
        """End-to-end audio chunk ingestion, word billing, claim extraction, triage, and verification."""
        mock_gemini = mock_get_gemini.return_value
        mock_gemini.transcribe_and_extract_claims_from_audio.return_value = {
            'transcript': 'The governor claimed that violent crime dropped by 45 percent last year across the state.',
            'claims': [
                {
                    'claim': 'Violent crime dropped by 45 percent last year across the state',
                    'timestamp': '00:08',
                    'quote': 'violent crime dropped by 45 percent last year'
                }
            ],
            'word_count': 16
        }
        mock_gemini.triage_for_stream.return_value = [
            {'priority': 'NORMAL', 'strategy': 'SEARCH_VERIFY', 'is_hyperbole': False}
        ]
        mock_gemini.analyze_claims_batch.return_value = [
            {
                'is_claim': True,
                'verdict': 'FALSE',
                'explanation': 'Official FBI statistics show violent crime dropped by 3.2%, not 45%.',
                'fallacy': None,
                'sources': ['https://fbi.gov/ucr/stats'],
                'source_reliability': 'HIGH'
            }
        ]

        response = auth_client.post(
            '/api/factcheck/stream-audio',
            data={
                'audio': (io.BytesIO(b'dummy audio bytes'), 'stream_01.webm', 'audio/webm'),
                'session_id': 'test_stream_session_1',
                'context': 'State of the State Address'
            },
            content_type='multipart/form-data'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert data['success'] is True
        assert 'violent crime dropped' in data['transcript'].lower()
        assert data['word_count'] == 16
        assert len(data['candidate_claims']) == 1
        assert data['candidate_claims'][0]['priority'] == 'normal'
        assert len(data['verified_claims']) == 1
        assert data['verified_claims'][0]['verdict'] == 'FALSE'
        assert data['verified_claims'][0]['is_cached'] is False

        # Verify factcheck was stored in DB
        fc = db_session_fixture.query(FactCheck).filter(
            FactCheck.claim_text.like('%Violent crime%')
        ).first()
        assert fc is not None
        assert fc.verdict == 'FALSE'

    @patch('backend.routes.factcheck.get_gemini_service')
    def test_stream_audio_deduplication(self, mock_get_gemini, auth_client):
        """Overlapping chunks in the same session must deduplicate repeated claims."""
        mock_gemini = mock_get_gemini.return_value
        mock_gemini.transcribe_and_extract_claims_from_audio.return_value = {
            'transcript': 'Unemployment is down to 3.5 percent.',
            'claims': [
                {'claim': 'Unemployment is down to 3.5 percent', 'timestamp': '00:14'}
            ],
            'word_count': 7
        }
        mock_gemini.triage_for_stream.return_value = [
            {'priority': 'NORMAL', 'strategy': 'SEARCH_VERIFY', 'is_hyperbole': False}
        ]
        mock_gemini.analyze_claims_batch.return_value = [
            {'verdict': 'TRUE', 'explanation': 'BLS reports 3.5% rate.', 'sources': []}
        ]

        # Chunk 1
        resp1 = auth_client.post(
            '/api/factcheck/stream-audio',
            data={
                'audio': (io.BytesIO(b'audio slice 1'), 'slice1.webm', 'audio/webm'),
                'session_id': 'dedupe_session_99'
            },
            content_type='multipart/form-data'
        )
        assert resp1.status_code == 200
        assert len(resp1.get_json()['candidate_claims']) == 1

        # Chunk 2 (same session, identical claim from overlapping audio boundary)
        resp2 = auth_client.post(
            '/api/factcheck/stream-audio',
            data={
                'audio': (io.BytesIO(b'audio slice 2'), 'slice2.webm', 'audio/webm'),
                'session_id': 'dedupe_session_99'
            },
            content_type='multipart/form-data'
        )
        assert resp2.status_code == 200
        # Should be deduplicated away!
        assert len(resp2.get_json()['candidate_claims']) == 0
        assert len(resp2.get_json()['verified_claims']) == 0

    @patch('backend.routes.factcheck.get_gemini_service')
    def test_stream_audio_hyperbole_filtered(self, mock_get_gemini, auth_client):
        """Rhetorical hyperbole triaged as SKIP or is_hyperbole should not be sent to agentic verification."""
        mock_gemini = mock_get_gemini.return_value
        mock_gemini.transcribe_and_extract_claims_from_audio.return_value = {
            'transcript': 'This is the greatest policy ever invented in human history!',
            'claims': [
                {'claim': 'This is the greatest policy ever invented in human history', 'timestamp': '00:03'}
            ],
            'word_count': 10
        }
        # Triage flags as hyperbole and SKIP
        mock_gemini.triage_for_stream.return_value = [
            {'priority': 'SKIP', 'strategy': 'KNOWLEDGE_CHECK', 'is_hyperbole': True}
        ]

        response = auth_client.post(
            '/api/factcheck/stream-audio',
            data={
                'audio': (io.BytesIO(b'audio slice hyperbole'), 'hyperbole.webm', 'audio/webm'),
                'session_id': 'hyperbole_session'
            },
            content_type='multipart/form-data'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert len(data['candidate_claims']) == 1
        assert data['candidate_claims'][0]['priority'] == 'skip'
        assert data['candidate_claims'][0]['is_hyperbole'] is True
        # Verified claims must be empty (filtered out from verification)
        assert len(data['verified_claims']) == 0
        # analyze_claims_batch must NOT have been called
        mock_gemini.analyze_claims_batch.assert_not_called()

    @patch('backend.routes.factcheck.get_gemini_service')
    def test_stream_audio_global_cache_hit(self, mock_get_gemini, auth_client, db_session_fixture):
        """If a claim was already verified for ANY user, stream-audio serves the cached verdict with 0 API calls."""
        # 1. Pre-seed a verified FactCheck in the database
        prior_user = User(email='prior_user@example.com', password_hash='hash', api_token='token123')
        db_session_fixture.add(prior_user)
        db_session_fixture.commit()

        cached_claim = "The solar plant produces 500 megawatts of electricity"
        existing_fc = FactCheck(
            user_id=prior_user.id,
            claim_text=cached_claim,
            is_claim=True,
            verdict='TRUE',
            explanation='Public utility records confirm the facility operates at 500 MW capacity.',
            sources=json.dumps(['https://energy.gov/solar-records']),
            source_reliability='HIGH',
            timestamp=datetime.now(UTC)
        )
        db_session_fixture.add(existing_fc)
        db_session_fixture.commit()

        # 2. Mock audio ingestion extracting the exact same claim
        mock_gemini = mock_get_gemini.return_value
        mock_gemini.transcribe_and_extract_claims_from_audio.return_value = {
            'transcript': 'The solar plant produces 500 megawatts of electricity in peak summer.',
            'claims': [
                {'claim': cached_claim, 'timestamp': '00:05'}
            ],
            'word_count': 11
        }
        mock_gemini.triage_for_stream.return_value = [
            {'priority': 'NORMAL', 'strategy': 'SEARCH_VERIFY', 'is_hyperbole': False}
        ]

        response = auth_client.post(
            '/api/factcheck/stream-audio',
            data={
                'audio': (io.BytesIO(b'audio slice'), 'test.webm', 'audio/webm'),
                'session_id': 'session_with_cache_hit'
            },
            content_type='multipart/form-data'
        )

        assert response.status_code == 200
        data = response.get_json()
        assert len(data['verified_claims']) == 1
        verified = data['verified_claims'][0]
        assert verified['claim'] == cached_claim
        assert verified['verdict'] == 'TRUE'
        assert verified['is_cached'] is True
        # analyze_claims_batch should NOT be called because it was resolved via Global Cache
        mock_gemini.analyze_claims_batch.assert_not_called()
