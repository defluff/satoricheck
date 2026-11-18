"""SSRF-hardened outbound HTTP for user-supplied URLs.

Why this exists: `requests` follows redirects by default, so validating only the
first URL lets a public host 302 the server into private/metadata addresses, and
`resp.content` buffers an unbounded body into memory. Every hop here is resolved
once, checked against private ranges, pinned across the socket connect to close
DNS-rebinding TOCTOU windows, followed manually on redirects, and capped by byte
size and wall-clock time.
"""
from contextlib import contextmanager
import ipaddress
import logging
import socket
import threading
import time
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; Authenix/1.0)'}
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_REDIRECTS = 5

_ALLOWED_SCHEMES = frozenset({'http', 'https'})
_BLOCKED_METADATA_IPS = frozenset({
    '169.254.169.254',  # AWS / GCP instance metadata
    'fd00:ec2::254',    # AWS IMDSv2 IPv6
})


class UnsafeURLError(ValueError):
    """The URL (or a redirect hop) is malformed, non-HTTP, or resolves to a private address."""


class ResponseTooLargeError(ValueError):
    """The remote body exceeds the caller's byte budget or time budget."""


def is_private_ip(ip_str: str) -> bool:
    """Return True if the IP is private, loopback, link-local, reserved, or cloud metadata."""
    if ip_str in _BLOCKED_METADATA_IPS:
        return True
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved


def assert_public_url(url: str) -> tuple[str, list]:
    """Raise UnsafeURLError unless `url` is http(s) and every resolved IP is public.

    Returns `(hostname, addr_infos)` so the caller can pin the validated resolution
    for the subsequent socket connection without a second DNS query.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError('URL must be a non-empty string')
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        raise UnsafeURLError(f'Unsupported URL: {url[:100]}')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        addr_infos = socket.getaddrinfo(parsed.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, ValueError) as exc:
        raise UnsafeURLError(f'Cannot resolve {parsed.hostname}') from exc
    if not addr_infos:
        raise UnsafeURLError(f'No addresses resolved for {parsed.hostname}')
    for *_rest, sockaddr in addr_infos:
        if is_private_ip(sockaddr[0]):
            logger.warning(f"SSRF blocked: {url[:100]} resolved to private IP {sockaddr[0]}")
            raise UnsafeURLError('URL resolves to a restricted address')
    return parsed.hostname, addr_infos


_DNS_PIN_LOCK = threading.Lock()
_DNS_PIN_ACTIVE_COUNT = 0
_DNS_PIN_ORIG_GETADDRINFO = None
_THREAD_DNS_PINS = threading.local()


def _normalize_port(port) -> int | None:
    """Normalize a getaddrinfo port argument to int when numeric."""
    if isinstance(port, bool) or port is None:
        return None
    if isinstance(port, int):
        return port
    if isinstance(port, str) and port.isdigit():
        return int(port)
    return None


def _dispatch_pinned_getaddrinfo(host, port, *args, **kwargs):
    """Thread-local DNS pin dispatcher installed while at least one pin is active."""
    pins = getattr(_THREAD_DNS_PINS, 'pins', None)
    if pins:
        host_str = host.decode('ascii', 'ignore') if isinstance(host, bytes) else str(host)
        key = (host_str.strip('[]').lower(), _normalize_port(port))
        pinned = pins.get(key)
        if pinned is not None:
            family = args[0] if args else kwargs.get('family', 0)
            if family not in (0, socket.AF_UNSPEC):
                return [ai for ai in pinned if ai[0] == family]
            return pinned
    with _DNS_PIN_LOCK:
        orig = _DNS_PIN_ORIG_GETADDRINFO or socket.getaddrinfo
    return orig(host, port, *args, **kwargs)


@contextmanager
def _pin_resolved_dns(hostname: str, addr_infos: list, port: int | None = None):
    """Pin `socket.getaddrinfo` on the calling thread for `(hostname, port)` to `addr_infos`.

    Why: `urllib3` resolves the hostname again when opening the TCP socket. Pinning
    the already-validated resolution prevents a `TTL=0` DNS rebinding server from
    returning a public IP during `assert_public_url` and `169.254.169.254` on connect.
    Pins are stored per-thread and the global `socket.getaddrinfo` hook is reference-
    counted under `_DNS_PIN_LOCK` so concurrent threads never overwrite or leak each
    other's wrappers on out-of-order exit.
    """
    global _DNS_PIN_ACTIVE_COUNT, _DNS_PIN_ORIG_GETADDRINFO

    if port is None and addr_infos:
        try:
            port = int(addr_infos[0][4][1])
        except (IndexError, TypeError, ValueError):
            port = None

    key = (hostname.strip('[]').lower(), port)
    pins = getattr(_THREAD_DNS_PINS, 'pins', None)
    if pins is None:
        pins = {}
        _THREAD_DNS_PINS.pins = pins
    had_prev = key in pins
    prev_entry = pins.get(key)
    pins[key] = addr_infos

    with _DNS_PIN_LOCK:
        if _DNS_PIN_ACTIVE_COUNT == 0:
            _DNS_PIN_ORIG_GETADDRINFO = socket.getaddrinfo
            socket.getaddrinfo = _dispatch_pinned_getaddrinfo
        _DNS_PIN_ACTIVE_COUNT += 1

    try:
        yield
    finally:
        if had_prev:
            pins[key] = prev_entry
        else:
            pins.pop(key, None)
        with _DNS_PIN_LOCK:
            _DNS_PIN_ACTIVE_COUNT -= 1
            if _DNS_PIN_ACTIVE_COUNT == 0:
                socket.getaddrinfo = _DNS_PIN_ORIG_GETADDRINFO
                _DNS_PIN_ORIG_GETADDRINFO = None


def _assert_public_peer(resp: requests.Response) -> None:
    """Defense-in-depth: if the response exposes a connected socket peer IP, verify it is public."""
    try:
        raw = getattr(resp, 'raw', None)
        conn = getattr(raw, '_connection', None)
        sock = getattr(conn, 'sock', None)
        peer = sock.getpeername() if sock is not None else None
        peer_ip = peer[0] if isinstance(peer, tuple) and peer else None
    except Exception:
        peer_ip = None
    if isinstance(peer_ip, str) and is_private_ip(peer_ip):
        resp.close()
        raise UnsafeURLError('Connected peer address is restricted')


def safe_request(
    method: str,
    url: str,
    *,
    timeout: float,
    stream: bool = False,
    max_redirects: int = MAX_REDIRECTS,
) -> requests.Response:
    """Send GET/HEAD to a public URL, validating every redirect hop before following it.

    The caller owns the returned response and must close it when `stream=True`.
    """
    send = getattr(requests, method.lower())
    for _hop in range(max_redirects + 1):
        hostname, addr_infos = assert_public_url(url)
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        with _pin_resolved_dns(hostname, addr_infos, port=port):
            resp = send(
                url,
                timeout=timeout,
                headers=DEFAULT_HEADERS,
                stream=stream,
                allow_redirects=False,
            )
        _assert_public_peer(resp)
        location = resp.headers.get('Location') if 300 <= resp.status_code < 400 else None
        if not location:
            return resp
        resp.close()
        url = urljoin(url, location)
    raise UnsafeURLError(f'Exceeded {max_redirects} redirects')


def download_bounded(url: str, *, max_bytes: int, timeout: float, total_timeout: float) -> bytes:
    """Download a public URL into memory, aborting past `max_bytes` or `total_timeout` seconds.

    `timeout` bounds each connect/read; `total_timeout` bounds the whole transfer so a
    slow-drip server cannot pin a Gunicorn thread indefinitely.
    """
    resp = safe_request('GET', url, timeout=timeout, stream=True)
    try:
        resp.raise_for_status()
        declared = resp.headers.get('Content-Length', '')
        if declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLargeError(f'Remote file is {declared} bytes (limit {max_bytes})')

        deadline = time.monotonic() + total_timeout
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ResponseTooLargeError(f'Remote file exceeds {max_bytes} bytes')
            if time.monotonic() > deadline:
                raise ResponseTooLargeError(f'Download exceeded {total_timeout}s')
        return bytes(body)
    finally:
        resp.close()
