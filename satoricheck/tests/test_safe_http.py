"""
SSRF hardening tests for backend.services.safe_http and its call sites.

Only wire boundaries are mocked (DNS via socket.getaddrinfo, HTTP via requests.get/head);
the real is_private_ip / redirect-walking logic executes.
"""
import socket
from unittest.mock import MagicMock, patch

import pytest

from backend.services import safe_http

PUBLIC_IP = '93.184.216.34'
DNS_TABLE = {
    'public.example.com': PUBLIC_IP,
    'cdn.example.com': '93.184.216.35',
    'internal.example.com': '10.0.0.5',
    '169.254.169.254': '169.254.169.254',
}


def _fake_getaddrinfo(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (DNS_TABLE[host], port))]


def _response(status=200, headers=None, chunks=()):
    resp = MagicMock(status_code=status, headers=headers or {})
    resp.iter_content.return_value = list(chunks)
    return resp


@pytest.fixture
def fake_dns():
    with patch('backend.services.safe_http.socket.getaddrinfo', side_effect=_fake_getaddrinfo) as dns:
        yield dns


class TestSafeRequestRedirects:

    @pytest.mark.parametrize('target', [
        'http://internal.example.com/secret',
        'http://169.254.169.254/computeMetadata/v1/',
    ])
    def test_redirect_to_private_address_is_blocked_before_request(self, fake_dns, target):
        redirect = _response(302, {'Location': target})
        with patch('backend.services.safe_http.requests.get', return_value=redirect) as mock_get:
            with pytest.raises(safe_http.UnsafeURLError, match='restricted address'):
                safe_http.safe_request('GET', 'https://public.example.com/img.jpg', timeout=5)

        mock_get.assert_called_once_with(
            'https://public.example.com/img.jpg',
            timeout=5,
            headers=safe_http.DEFAULT_HEADERS,
            stream=False,
            allow_redirects=False,
        )
        redirect.close.assert_called_once_with()

    def test_relative_redirect_to_public_host_is_followed(self, fake_dns):
        final = _response(200)
        responses = [_response(301, {'Location': '/moved.jpg'}), final]
        with patch('backend.services.safe_http.requests.get', side_effect=responses) as mock_get:
            resp = safe_http.safe_request('GET', 'https://public.example.com/img.jpg', timeout=5)

        assert resp is final
        assert [c.args[0] for c in mock_get.call_args_list] == [
            'https://public.example.com/img.jpg',
            'https://public.example.com/moved.jpg',
        ]

    def test_redirect_loop_is_capped(self, fake_dns):
        loop = _response(302, {'Location': 'https://public.example.com/again'})
        with patch('backend.services.safe_http.requests.head', return_value=loop) as mock_head:
            with pytest.raises(safe_http.UnsafeURLError, match='Exceeded 2 redirects'):
                safe_http.safe_request('HEAD', 'https://public.example.com/', timeout=3, max_redirects=2)

        assert mock_head.call_count == 3

    @pytest.mark.parametrize('url', ['file:///etc/passwd', 'gopher://public.example.com', 'http://', ''])
    def test_non_http_or_hostless_urls_are_rejected_without_dns(self, fake_dns, url):
        with pytest.raises(safe_http.UnsafeURLError):
            safe_http.assert_public_url(url)
        fake_dns.assert_not_called()

    def test_dns_rebinding_pins_initial_resolution_during_connect(self):
        """Finding 8b: if an attacker DNS server returns a public IP on lookup 1 and
        169.254.169.254 on lookup 2, safe_request pins lookup 1 across the HTTP send
        so urllib3 never queries the attacker DNS a second time."""
        rebind_answers = [
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (PUBLIC_IP, 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('169.254.169.254', 443))],
        ]
        observed_connect_ips = []

        def transport_get(url, **kwargs):
            resolved = socket.getaddrinfo('rebind.example.com', 443, proto=socket.IPPROTO_TCP)
            observed_connect_ips.append(resolved[0][4][0])
            return _response(200)

        with patch('backend.services.safe_http.socket.getaddrinfo', side_effect=rebind_answers) as mock_dns, \
             patch('backend.services.safe_http.requests.get', side_effect=transport_get):
            resp = safe_http.safe_request('GET', 'https://rebind.example.com/img.png', timeout=5)

        assert resp.status_code == 200
        assert observed_connect_ips == [PUBLIC_IP]
        assert mock_dns.call_count == 1

    def test_peer_ip_check_aborts_if_socket_connected_to_private_ip(self, fake_dns):
        """Defense-in-depth: if the underlying socket peer IP is private, abort and close."""
        resp = _response(200)
        resp.raw._connection.sock.getpeername.return_value = ('169.254.169.254', 80)

        with patch('backend.services.safe_http.requests.get', return_value=resp):
            with pytest.raises(safe_http.UnsafeURLError, match='Connected peer address is restricted'):
                safe_http.safe_request('GET', 'https://public.example.com/img.png', timeout=5)

        resp.close.assert_called_once_with()


class TestDownloadBounded:

    def test_returns_body_within_limits(self, fake_dns):
        resp = _response(200, {'Content-Length': '6'}, chunks=[b'abc', b'def'])
        with patch('backend.services.safe_http.requests.get', return_value=resp):
            body = safe_http.download_bounded(
                'https://cdn.example.com/a.png', max_bytes=10, timeout=5, total_timeout=10
            )

        assert body == b'abcdef'
        resp.close.assert_called_once_with()

    def test_declared_oversize_is_rejected_without_reading_body(self, fake_dns):
        resp = _response(200, {'Content-Length': '11'}, chunks=[b'x' * 11])
        with patch('backend.services.safe_http.requests.get', return_value=resp):
            with pytest.raises(safe_http.ResponseTooLargeError, match='11 bytes'):
                safe_http.download_bounded(
                    'https://cdn.example.com/a.png', max_bytes=10, timeout=5, total_timeout=10
                )

        resp.iter_content.assert_not_called()
        resp.close.assert_called_once_with()

    def test_undeclared_oversize_stream_is_aborted(self, fake_dns):
        resp = _response(200, {}, chunks=[b'x' * 6, b'x' * 6, b'x' * 6])
        with patch('backend.services.safe_http.requests.get', return_value=resp):
            with pytest.raises(safe_http.ResponseTooLargeError, match='exceeds 10 bytes'):
                safe_http.download_bounded(
                    'https://cdn.example.com/a.png', max_bytes=10, timeout=5, total_timeout=10
                )

        resp.close.assert_called_once_with()

    def test_slow_drip_stream_hits_total_deadline(self, fake_dns, monkeypatch):
        clock = iter([0.0, 5.0, 11.0])
        monkeypatch.setattr(safe_http.time, 'monotonic', lambda: next(clock))
        resp = _response(200, {}, chunks=[b'a', b'b', b'c'])
        with patch('backend.services.safe_http.requests.get', return_value=resp):
            with pytest.raises(safe_http.ResponseTooLargeError, match='exceeded 10s'):
                safe_http.download_bounded(
                    'https://cdn.example.com/a.png', max_bytes=100, timeout=5, total_timeout=10
                )


class TestAnalyzeUrlHeadProbe:

    def test_probe_redirect_to_private_host_is_not_followed_and_no_tokens_charged(
        self, auth_client, test_user, db_session_fixture, fake_dns
    ):
        """TOCTOU: the reachability check sees 200, the MIME probe then gets a redirect
        to an internal host. The probe must not follow it, and the user is not charged."""
        from backend.models import TokenBalance

        responses = [
            _response(200),  # _validate_url reachability check
            _response(302, {'Location': 'http://internal.example.com/admin'}),  # MIME probe
        ]
        with patch('backend.services.safe_http.requests.head', side_effect=responses) as mock_head:
            response = auth_client.post(
                '/api/media/analyze-url', json={'url': 'https://public.example.com/asset'}
            )

        assert response.status_code == 400
        assert response.get_json()['error'].startswith('This URL type is not supported.')
        assert mock_head.call_count == 2
        assert all(c.kwargs['allow_redirects'] is False for c in mock_head.call_args_list)
        assert [c.args[0] for c in fake_dns.call_args_list] == [
            'public.example.com', 'public.example.com', 'internal.example.com',
        ]
        balance = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert balance.balance == 100
