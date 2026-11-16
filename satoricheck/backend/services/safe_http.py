"""SSRF-hardened outbound HTTP for user-supplied URLs.

Why this exists: `requests` follows redirects by default, so validating only the
first URL lets a public host 302 the server into private/metadata addresses, and
`resp.content` buffers an unbounded body into memory. Every hop here is re-resolved
and checked against private ranges, redirects are followed manually, and downloads
are capped by size and wall-clock time.

Residual risk: DNS is resolved by us and again by urllib3, so a sub-second DNS
rebinding attack is not fully closed. Pinning the resolved IP needs a custom
transport adapter; tracked in docs/APP_ARCHITECTURE_AUDIT.md.
"""
import ipaddress
import logging
import socket
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


def assert_public_url(url: str) -> None:
    """Raise UnsafeURLError unless `url` is http(s) and every resolved IP is public."""
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
    for *_rest, sockaddr in addr_infos:
        if is_private_ip(sockaddr[0]):
            logger.warning(f"SSRF blocked: {url[:100]} resolved to private IP {sockaddr[0]}")
            raise UnsafeURLError('URL resolves to a restricted address')


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
        assert_public_url(url)
        resp = send(url, timeout=timeout, headers=DEFAULT_HEADERS, stream=stream, allow_redirects=False)
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
