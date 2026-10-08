"""
Scheduler authentication tests (shared by every Cloud Scheduler cron endpoint).
"""
import os
from unittest.mock import patch

import pytest

from backend.config import Config

SCHEDULER_ENDPOINTS = [
    '/api/billing/wizard-refill',
    '/api/factcheck/cleanup-expired',
]


@pytest.mark.parametrize('path', SCHEDULER_ENDPOINTS)
class TestSchedulerSecretRequired:
    """Behaviour of @scheduler_secret_required on each cron endpoint."""

    def test_accepts_correct_secret(self, client, path):
        response = client.post(path, headers={'X-Scheduler-Secret': Config.SCHEDULER_SECRET})
        assert response.status_code == 200

    def test_rejects_missing_header(self, client, path):
        assert client.post(path).status_code == 401

    def test_rejects_empty_header(self, client, path):
        assert client.post(path, headers={'X-Scheduler-Secret': ''}).status_code == 401

    def test_rejects_wrong_secret(self, client, path):
        assert client.post(path, headers={'X-Scheduler-Secret': 'wrong'}).status_code == 401

    def test_rejects_secret_prefix(self, client, path):
        """A truncated secret must not match (guards against startswith-style comparisons)."""
        prefix = Config.SCHEDULER_SECRET[:-1]
        assert client.post(path, headers={'X-Scheduler-Secret': prefix}).status_code == 401

    def test_non_ascii_header_is_401_not_500(self, client, path):
        """hmac.compare_digest raises TypeError on non-ASCII str; that must not become a 500."""
        response = client.post(path, headers={'X-Scheduler-Secret': 'sécret-ü'})
        assert response.status_code == 401

    def test_rejects_when_server_secret_is_unconfigured(self, client, path):
        """An empty server-side secret must never authenticate an empty/any header."""
        with patch.object(Config, 'SCHEDULER_SECRET', ''):
            response = client.post(path, headers={'X-Scheduler-Secret': ''})
        assert response.status_code == 401


class TestSchedulerSecretProductionGuard:
    """Config.validate() must fail fast when production lacks an explicit secret."""

    def _validate_in_production(self, environ):
        with patch.dict(os.environ, environ, clear=False), \
                patch.object(Config, 'ENV', 'production'), \
                patch.object(Config, 'TEST_MODE', False), \
                patch.object(Config, 'SECRET_KEY', 'x'), \
                patch.object(Config, 'GEMINI_API_KEY', 'x'), \
                patch.object(Config, 'STRIPE_SECRET_KEY', 'x'):
            Config.validate()

    def test_production_without_scheduler_secret_fails_startup(self):
        with patch.dict(os.environ):
            os.environ.pop('SCHEDULER_SECRET', None)
            with pytest.raises(ValueError, match='SCHEDULER_SECRET'):
                self._validate_in_production({})

    def test_production_with_scheduler_secret_passes(self):
        self._validate_in_production({'SCHEDULER_SECRET': 'configured'})

    def test_non_production_does_not_require_scheduler_secret(self):
        with patch.dict(os.environ):
            os.environ.pop('SCHEDULER_SECRET', None)
            with patch.object(Config, 'ENV', 'development'), \
                    patch.object(Config, 'TEST_MODE', False), \
                    patch.object(Config, 'SECRET_KEY', 'x'), \
                    patch.object(Config, 'GEMINI_API_KEY', 'x'), \
                    patch.object(Config, 'STRIPE_SECRET_KEY', 'x'):
                Config.validate()
