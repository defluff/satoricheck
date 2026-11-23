"""
SSRF hardening tests for backend.services.safe_http and its call sites.

Only wire boundaries are mocked (DNS via socket.getaddrinfo, HTTP via requests.get/head);
the real is_private_ip / redirect-walking logic executes.
"""
import socket
import threading
from unittest.mock import MagicMock, patch

import pytest

from backend.services import safe_http

PUBLIC_IP = '93.184.216.34'
DNS_TABLE = {
    'public.example.com': PUBLIC_IP,
    'cdn.example.com': '93.184.216.35',
    'internal.example.com': '10.0.0.5',
    '127.0.0.1': '127.0.0.1',
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

    def test_dns_pin_is_scoped_to_resolved_port(self):
        """Pinning hostname:443 must not intercept a subsequent lookup for hostname:80."""
        dns_answers = [
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (PUBLIC_IP, 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.99', 80))],
        ]
        observed = {}

        def transport_get(url, **kwargs):
            observed['https'] = socket.getaddrinfo('rebind.example.com', 443, proto=socket.IPPROTO_TCP)[0][4]
            observed['http'] = socket.getaddrinfo('rebind.example.com', 80, proto=socket.IPPROTO_TCP)[0][4]
            return _response(200)

        with patch('backend.services.safe_http.socket.getaddrinfo', side_effect=dns_answers) as mock_dns, \
             patch('backend.services.safe_http.requests.get', side_effect=transport_get):
            resp = safe_http.safe_request('GET', 'https://rebind.example.com/img.png', timeout=5)

        assert resp.status_code == 200
        assert observed == {
            'https': (PUBLIC_IP, 443),
            'http': ('93.184.216.99', 80),
        }
        assert mock_dns.call_count == 2

    def test_concurrent_requests_with_out_of_order_exit_do_not_leak_dns_pin(self, fake_dns):
        """Overlapping threads that exit out of order must stay isolated and restore socket.getaddrinfo."""
        both_in_pin = threading.Barrier(2, timeout=2)
        thread_a_exited = threading.Event()
        results = {}
        errors = []

        def transport_get(url, **kwargs):
            if 'public.example.com' in url:
                both_in_pin.wait()
                # Thread A sees its own pin and does NOT see Thread B's pin for cdn.example.com
                results['a_own'] = socket.getaddrinfo('public.example.com', 443, proto=socket.IPPROTO_TCP)[0][4][0]
                results['a_other_hits_dns'] = socket.getaddrinfo('cdn.example.com', 443, proto=socket.IPPROTO_TCP)[0][4][0]
                return _response(200)

            both_in_pin.wait()
            assert thread_a_exited.wait(timeout=2)
            results['b_own'] = socket.getaddrinfo('cdn.example.com', 443, proto=socket.IPPROTO_TCP)[0][4][0]
            return _response(200)

        def run_a():
            try:
                safe_http.safe_request('GET', 'https://public.example.com/a.png', timeout=5)
            except BaseException as exc:
                errors.append(exc)
            finally:
                thread_a_exited.set()

        def run_b():
            try:
                safe_http.safe_request('GET', 'https://cdn.example.com/b.png', timeout=5)
            except BaseException as exc:
                errors.append(exc)

        with patch('backend.services.safe_http.requests.get', side_effect=transport_get):
            t_a = threading.Thread(target=run_a)
            t_b = threading.Thread(target=run_b)
            t_a.start()
            t_b.start()
            t_a.join(timeout=5)
            t_b.join(timeout=5)

        assert errors == []
        assert results == {
            'a_own': PUBLIC_IP,
            'a_other_hits_dns': DNS_TABLE['cdn.example.com'],
            'b_own': DNS_TABLE['cdn.example.com'],
        }
        assert socket.getaddrinfo is fake_dns
        assert safe_http._DNS_PIN_ACTIVE_COUNT == 0
        # After both threads exit, public.example.com hits fake_dns again (not a leaked pin)
        calls_before = fake_dns.call_count
        socket.getaddrinfo('public.example.com', 443, proto=socket.IPPROTO_TCP)
        assert fake_dns.call_count == calls_before + 1

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
        """A single HEAD MIME probe receiving a redirect to an internal host must block it
        without a second HEAD call and without charging tokens."""
        from backend.models import TokenBalance

        responses = [
            _response(302, {'Location': 'http://internal.example.com/admin'}),
        ]
        with patch('backend.services.safe_http.requests.head', side_effect=responses) as mock_head:
            response = auth_client.post(
                '/api/media/analyze-url', json={'url': 'https://public.example.com/asset'}
            )

        assert response.status_code == 400
        assert response.get_json()['error'].startswith('Invalid or restricted URL.')
        assert mock_head.call_count == 1
        assert all(c.kwargs['allow_redirects'] is False for c in mock_head.call_args_list)
        assert [c.args[0] for c in fake_dns.call_args_list] == [
            'public.example.com', 'internal.example.com',
        ]
        balance = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert balance.balance == 100

    def test_zero_balance_does_not_issue_head_probe(
        self, auth_client, test_user, db_session_fixture, fake_dns
    ):
        """Underfunded users must fail fast with 403 before outbound HEAD probing."""
        from backend.models import TokenBalance

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 0
        db_session_fixture.commit()

        with patch('backend.services.safe_http.requests.head') as mock_head:
            response = auth_client.post(
                '/api/media/analyze-url', json={'url': 'https://public.example.com/asset'}
            )

        assert response.status_code == 403
        assert 'Insufficient tokens' in response.get_json()['error']
        mock_head.assert_not_called()


class TestSafeApiRequest:

    @pytest.mark.parametrize('url', [
        'http://public.example.com/oauth/token',
        'file:///etc/passwd',
        'ftp://public.example.com/file',
        'https://',
        '',
    ])
    def test_rejects_non_https_or_malformed_urls_without_dns_or_http(self, fake_dns, url):
        with patch('backend.services.safe_http.requests.request') as mock_req:
            with pytest.raises(safe_http.UnsafeURLError):
                safe_http.safe_api_request('GET', url)
        fake_dns.assert_not_called()
        mock_req.assert_not_called()

    @pytest.mark.parametrize('url', [
        'https://internal.example.com/api/v1',
        'https://169.254.169.254/computeMetadata/v1/',
        'https://127.0.0.1/admin',
    ])
    def test_blocks_private_loopback_and_metadata_ips_before_http(self, fake_dns, url):
        with patch('backend.services.safe_http.requests.request') as mock_req:
            with pytest.raises(safe_http.UnsafeURLError, match='restricted address'):
                safe_http.safe_api_request('POST', url, json_body={'k': 'v'})
        mock_req.assert_not_called()

    @pytest.mark.parametrize('bad_method', ['', '   ', 'TRACE', 'CONNECT', None])
    def test_rejects_invalid_http_methods(self, fake_dns, bad_method):
        with patch('backend.services.safe_http.requests.request') as mock_req:
            with pytest.raises(ValueError):
                safe_http.safe_api_request(bad_method, 'https://public.example.com/api')
        mock_req.assert_not_called()

    @pytest.mark.parametrize('bad_timeout', [0, -1, -0.5, False, True, None, (5,), (5, 0), float('nan'), float('inf')])
    def test_rejects_non_positive_or_invalid_timeout(self, fake_dns, bad_timeout):
        with patch('backend.services.safe_http.requests.request') as mock_req:
            with pytest.raises(ValueError, match='timeout'):
                safe_http.safe_api_request('GET', 'https://public.example.com/api', timeout=bad_timeout)
        mock_req.assert_not_called()

    def test_pins_dns_and_blocks_private_connected_peer(self):
        rebind_answers = [
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (PUBLIC_IP, 443))],
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('169.254.169.254', 443))],
        ]
        observed_ips = []

        def transport(method, url, **kwargs):
            resolved = socket.getaddrinfo('rebind.example.com', 443, proto=socket.IPPROTO_TCP)
            observed_ips.append(resolved[0][4][0])
            return _response(200)

        with patch('backend.services.safe_http.socket.getaddrinfo', side_effect=rebind_answers) as mock_dns, \
             patch('backend.services.safe_http.requests.request', side_effect=transport):
            resp = safe_http.safe_api_request('GET', 'https://rebind.example.com/v1')

        assert resp.status_code == 200
        assert observed_ips == [PUBLIC_IP]
        assert mock_dns.call_count == 1

        private_peer_resp = _response(200)
        private_peer_resp.raw._connection.sock.getpeername.return_value = ('10.0.0.5', 443)
        with patch('backend.services.safe_http.socket.getaddrinfo', side_effect=_fake_getaddrinfo), \
             patch('backend.services.safe_http.requests.request', return_value=private_peer_resp):
            with pytest.raises(safe_http.UnsafeURLError, match='Connected peer address is restricted'):
                safe_http.safe_api_request('GET', 'https://public.example.com/v1')
        private_peer_resp.close.assert_called_once_with()

    def test_forwards_parameters_and_disables_redirects(self, fake_dns):
        redirect_resp = _response(302, {'Location': 'https://internal.example.com/steal'})
        with patch('backend.services.safe_http.requests.request', return_value=redirect_resp) as mock_req:
            resp = safe_http.safe_api_request(
                'post',
                'https://public.example.com/oauth/token',
                headers={'Authorization': 'Bearer tok'},
                params={'alt': 'json'},
                json_body={'hello': 'world'},
                data=None,
                timeout=(3, 10),
                stream=True,
            )

        assert resp is redirect_resp
        assert resp.status_code == 302
        mock_req.assert_called_once_with(
            'POST',
            'https://public.example.com/oauth/token',
            headers={'Authorization': 'Bearer tok'},
            params={'alt': 'json'},
            json={'hello': 'world'},
            data=None,
            timeout=(3, 10),
            stream=True,
            allow_redirects=False,
        )

    def test_safe_api_request_strips_query_string_and_userinfo_from_errors(self, fake_dns):
        with patch('backend.services.safe_http.requests.request') as mock_req:
            with pytest.raises(safe_http.UnsafeURLError) as exc_info:
                safe_http.safe_api_request(
                    'GET',
                    'http://user:SECRET_PW@api.example.com/v1?access_token=SECRET_TOK#frag',
                )
        err_msg = str(exc_info.value)
        assert 'access_token' not in err_msg
        assert 'SECRET_TOK' not in err_msg
        assert 'SECRET_PW' not in err_msg
        assert 'http://api.example.com/v1' in err_msg
        fake_dns.assert_not_called()
        mock_req.assert_not_called()

    @pytest.mark.parametrize('bad_url', [
        ' https://public.example.com/v1?token=SECRET_WS',
        'https://public.example.com/v1 ',
        'https://public.example.com/v1\n?token=SECRET_CTL',
    ])
    def test_rejects_whitespace_and_control_chars_without_dns_or_leak(self, fake_dns, bad_url):
        with patch('backend.services.safe_http.requests.request') as mock_req:
            with pytest.raises(safe_http.UnsafeURLError) as exc_info:
                safe_http.safe_api_request('GET', bad_url)
        err_msg = str(exc_info.value)
        assert 'SECRET_' not in err_msg
        fake_dns.assert_not_called()
        mock_req.assert_not_called()

    def test_assert_public_url_ssrf_log_scrubs_query_string_and_userinfo(self, fake_dns, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        with pytest.raises(safe_http.UnsafeURLError, match='restricted address'):
            safe_http.assert_public_url(
                'https://user:SECRET_PASS@127.0.0.1/video.mp4?secret_token=SECRET_QS_999#frag'
            )
        assert 'SECRET_PASS' not in caplog.text
        assert 'secret_token' not in caplog.text
        assert 'SECRET_QS_999' not in caplog.text
        assert 'https://127.0.0.1/video.mp4' in caplog.text
