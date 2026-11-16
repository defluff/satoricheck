"""
Security response header tests.

Verifies the headers actually emitted by the running app (not a grep of the source), so a
broken middleware or refactor is caught.
"""
import pytest

from backend.config import Config

REQUIRED_HEADERS = {
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
}


@pytest.fixture
def production_response(client, monkeypatch):
    """A /health response as produced in production (HSTS is production-only)."""
    monkeypatch.setattr(Config, 'ENV', 'production')
    return client.get('/health')


@pytest.mark.parametrize('header,expected', sorted(REQUIRED_HEADERS.items()))
def test_static_security_headers_have_expected_value(production_response, header, expected):
    assert production_response.headers.get(header) == expected


def test_hsts_is_enabled_in_production(production_response):
    hsts = production_response.headers.get('Strict-Transport-Security', '')
    assert 'max-age=31536000' in hsts
    assert 'includeSubDomains' in hsts


def test_hsts_is_not_sent_outside_production(client):
    """HSTS on localhost/dev would pin browsers to HTTPS for a host that has no TLS."""
    assert 'Strict-Transport-Security' not in client.get('/health').headers


def test_csp_blocks_framing_and_defaults_to_self(production_response):
    csp = production_response.headers['Content-Security-Policy']
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_csp_workers_are_same_origin_only(production_response):
    """Workers are self-hosted (PDF.js); no third-party origin may supply worker code."""
    directives = dict(
        part.strip().split(' ', 1)
        for part in production_response.headers['Content-Security-Policy'].split(';')
        if part.strip()
    )
    assert directives['worker-src'] == "'self' blob:"


def test_cors_allows_pinned_chrome_extension_origin(client):
    """CORS must allow the published Chrome Web Store extension origin with credentials."""
    pinned_origin = 'chrome-extension://egamiabbbonhkjlhjbniocfpihpdcehl'
    response = client.get('/health', headers={'Origin': pinned_origin})

    assert response.status_code == 200
    assert response.headers.get('Access-Control-Allow-Origin') == pinned_origin
    assert response.headers.get('Access-Control-Allow-Credentials') == 'true'


def test_cors_rejects_unpinned_chrome_extension_origin(client):
    """CORS must not grant credentialed cross-origin access to arbitrary Chrome extensions."""
    rogue_origin = 'chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    response = client.get('/health', headers={'Origin': rogue_origin})

    assert response.status_code == 200
    assert 'Access-Control-Allow-Origin' not in response.headers
