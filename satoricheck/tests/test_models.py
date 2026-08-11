"""
Unit tests for database model serialization methods and JWT cookie helpers.
"""
from datetime import datetime, UTC
import json
from flask import Response
from backend.models import FactCheck, Transaction
from backend.jwt_utils import (
    set_jwt_cookie, JWT_COOKIE_NAME, JWT_COOKIE_HTTPONLY,
    JWT_COOKIE_SECURE, JWT_COOKIE_SAMESITE
)


class TestModelToDict:
    """Test to_dict serialization methods on ORM models."""

    def test_fact_check_to_dict_full(self):
        fc = FactCheck(
            id=42,
            user_id=1,
            claim_text="The earth orbits the sun.",
            verdict="TRUE",
            explanation="Well-established scientific fact.",
            fallacy=None,
            sources=json.dumps(["https://nasa.gov"]),
            source_reliability="HIGH",
            tokens_used=1,
            source="factcheck",
            source_id="session-123",
            timestamp=datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
        )
        d = fc.to_dict()

        assert d['id'] == 42
        assert d['claim_text'] == "The earth orbits the sun."
        assert d['verdict'] == "TRUE"
        assert d['explanation'] == "Well-established scientific fact."
        assert d['fallacy'] is None
        assert d['sources'] == ["https://nasa.gov"]
        assert d['source_reliability'] == "HIGH"
        assert d['tokens_used'] == 1
        assert d['source'] == "factcheck"
        assert d['source_id'] == "session-123"
        assert d['timestamp'] == "2026-08-10T12:00:00+00:00"

    def test_fact_check_to_dict_null_sources_and_timestamp(self):
        fc = FactCheck(
            id=1,
            user_id=1,
            claim_text="Test claim",
            verdict="UNVERIFIED",
            sources=None,
            timestamp=None
        )
        d = fc.to_dict()

        assert d['sources'] == []
        assert d['timestamp'] is None

    def test_transaction_to_dict(self):
        tx = Transaction(
            id=10,
            user_id=5,
            type="purchase",
            amount=100,
            description="Purchased 100 CP",
            timestamp=datetime(2026, 8, 11, 15, 30, 0, tzinfo=UTC)
        )
        d = tx.to_dict()

        assert d['id'] == 10
        assert d['type'] == "purchase"
        assert d['amount'] == 100
        assert d['description'] == "Purchased 100 CP"
        assert d['timestamp'] == "2026-08-11T15:30:00+00:00"


class TestJWTCookieHelper:
    """Test set_jwt_cookie utility function."""

    def test_set_jwt_cookie_sets_correct_headers(self):
        response = Response()
        token_str = "sample.jwt.token"

        set_jwt_cookie(response, token_str)

        # Retrieve set cookie header
        set_cookie_headers = response.headers.getlist("Set-Cookie")
        assert len(set_cookie_headers) == 1
        cookie_header = set_cookie_headers[0]

        assert f"{JWT_COOKIE_NAME}={token_str}" in cookie_header
        assert "HttpOnly" in cookie_header
        assert f"SameSite={JWT_COOKIE_SAMESITE}" in cookie_header
        assert "Max-Age=604800" in cookie_header  # 7 days in seconds
