"""
Milestone M3 tests for multi-provider folder sync (Google Drive, Dropbox, OneDrive),
bounded streaming downloads, PDF pre-flight validation, SyncedDeckRecord dedup &
in-place retry, CP billing + 1 CP sync fee, auto-refill, refunds/Transaction ledger,
401 token refresh / InvalidToken recovery, daily cron cap, and PII-clean logging.

Follows tests/conftest.py and tests/test_suite_integrity.py strictly:
- Wire-only mocking (`requests.Session.request`, host-scoped `socket.getaddrinfo`,
  Gemini `svc.client`, `stripe.PaymentIntent.create`)
- Exact HTTP status, payload, DB state, and wire-call assertions
- RFC 2606 @example.com emails
"""
from __future__ import annotations

from datetime import datetime, timedelta, UTC
import json
import logging
import socket
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote, urlparse

import bcrypt
import pytest
import requests
import stripe

from backend.config import Config
from backend.jwt_utils import JWT_COOKIE_NAME, create_token
from backend.models import (
    AuditLog,
    CloudStorageConnection,
    SyncedDeckRecord,
    TokenBalance,
    Transaction,
    User,
)
from backend.services import get_pitchdeck_service
from backend.services.cloud_tokens import decrypt_token
from backend.services.folder_sync_service import MAX_SYNC_FILES_PER_RUN, calculate_sync_cp
from backend.services.google_sheets_service import parse_spreadsheet_id
from backend.services.gemini.pitchdeck import GeminiServicePitchdeck
from backend.services.safe_http import ResponseTooLargeError, read_bounded_response
from backend.services.storage_providers.base import ProviderFolderError
from backend.services.storage_providers.dropbox import DropboxProvider
from backend.services.storage_providers.google_drive import GoogleDriveProvider

PUBLIC_TEST_IP = '93.184.216.34'
MAX_PDF_BYTES = 25 * 1024 * 1024
PROVIDER_HOSTS = frozenset({
    'oauth2.googleapis.com',
    'www.googleapis.com',
    'api.dropboxapi.com',
    'content.dropboxapi.com',
    'login.microsoftonline.com',
    'graph.microsoft.com',
})


def _make_pdf(pages: int = 8) -> bytes:
    header = b'%PDF-1.4\n'
    body = f'1 0 obj\n<< /Type /Pages /Count {pages} >>\nendobj\n'.encode('ascii')
    return header + body + b'%%EOF\n'


def _make_exact_size_pdf(total_bytes: int, pages: int = 10) -> bytes:
    base = _make_pdf(pages)
    if total_bytes <= len(base):
        return base[:total_bytes]
    pad = b'%' + (b'A' * (total_bytes - len(base) - 1))
    return base + pad


def _make_spoofed_count_pdf() -> bytes:
    return b'%PDF-1.4\n1 0 obj\n<< /Type /Pages /Count 9999999999 >>\nendobj\n%%EOF\n'


def _make_zero_count_pdf() -> bytes:
    return b'%PDF-1.4\n1 0 obj\n<< /Type /Pages /Count 000 >>\nendobj\n%%EOF\n'


def _canonical_pitchdeck_payload(**overrides) -> dict:
    payload = {
        'company_name': 'Acme AI',
        'summary': 'Automated compliance for SaaS.',
        'usp': '10x faster audits.',
        'industry': 'Enterprise Software',
        'sector': 'B2B SaaS',
        'startup_profile': {
            'geography': 'Europe',
            'geography_tags': ['DACH'],
            'sector': 'B2B SaaS',
            'stage': 'Seed',
        },
        'market_size': '$12B TAM',
        'competition': ['CompA', 'CompB'],
        'team_highlights': 'Ex-Stripe founders.',
        'funding_ask': '$3M Seed',
        'page_count': 8,
        'red_flags': ['Customer concentration 45%'],
        'verifiable_claims': [
            {
                'claim': 'ARR grew 300% YoY',
                'risk_level': 'high',
                'suggested_question': 'High-risk Q1?',
                'verification_finding': 'Unverified private figure',
            },
            {
                'claim': 'ISO 27001 certified',
                'risk_level': 'low',
                'suggested_question': 'Low-risk Q4?',
                'verification_finding': 'Plausible',
            },
        ],
        'vc_metrics': {
            'monthly_revenue_arr': {'value': '$120K ARR', 'assessment': 'Strong', 'detail': 'Verified'},
            'burn_multiple': {'value': '1.2x', 'assessment': 'Healthy', 'detail': 'Efficient'},
            'nrr_percent': {'value': '118%', 'assessment': 'Strong', 'detail': 'Expanding'},
            'cac_payback_months': {'value': '10 months', 'assessment': 'Healthy', 'detail': 'Fast'},
            'ltv_cac_ratio': {'value': '4.5x', 'assessment': 'Strong', 'detail': 'Good'},
            'runway_months': {'value': '22 months', 'assessment': 'Healthy', 'detail': 'Sufficient'},
        },
        'copy_style': 'distinctive',
    }
    payload.update(overrides)
    return payload


def _install_gemini_mock(payload: dict | Exception | None = None):
    svc = get_pitchdeck_service()
    mock_client = MagicMock()
    mock_uploaded = MagicMock()
    mock_uploaded.name = 'files/m3-large-pdf-1'
    mock_uploaded.state = 'ACTIVE'
    mock_client.files.upload.return_value = mock_uploaded
    mock_client.files.get.return_value = mock_uploaded

    if isinstance(payload, Exception):
        mock_client.models.generate_content.side_effect = payload
    else:
        data = payload if payload is not None else _canonical_pitchdeck_payload()
        resp_obj = MagicMock()
        resp_obj.text = json.dumps(data)
        mock_client.models.generate_content.return_value = resp_obj

    svc.client = mock_client
    return mock_client


def _make_wire_response(
    status_code: int = 200,
    *,
    json_data: dict | None = None,
    content: bytes | None = None,
    chunks: list[bytes] | None = None,
    headers: dict | None = None,
) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    resp.headers = dict(headers or {})
    if json_data is not None:
        raw = json.dumps(json_data).encode('utf-8')
        resp.content = raw
        resp.text = raw.decode('utf-8')
        resp.json.return_value = json_data
        resp.headers.setdefault('Content-Type', 'application/json')
    elif content is not None:
        resp.content = content
        resp.text = ''
        resp.json.side_effect = ValueError('No JSON')
    else:
        resp.content = b''
        resp.text = ''
        resp.json.side_effect = ValueError('No JSON')

    if chunks is not None:
        resp.iter_content.return_value = iter(chunks)
    else:
        raw_body = resp.content
        resp.iter_content.return_value = iter([raw_body] if raw_body else [])

    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f'HTTP {status_code}', response=resp)
    else:
        resp.raise_for_status.return_value = None

    resp.raw = MagicMock()
    resp.raw._connection = MagicMock()
    resp.raw._connection.sock = MagicMock()
    resp.raw._connection.sock.getpeername.return_value = (PUBLIC_TEST_IP, 443)
    return resp


class _SyncWireRouter:
    """Stateful HTTP wire stub for Google Drive, Dropbox, and OneDrive APIs."""

    def __init__(self):
        self.calls: list[dict] = []
        self.tokens = {
            'google_drive': {
                'access_token': 'ya29.m3_access_1',
                'refresh_token': '1//m3_refresh_1',
                'expires_in': 3600,
                'scope': 'https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/spreadsheets',
            },
            'dropbox': {
                'access_token': 'sl.m3_dbx_access_1',
                'refresh_token': 'dbx_m3_refresh_1',
                'expires_in': 14400,
            },
            'onedrive': {
                'access_token': 'eyJ.m3_od_access_1',
                'refresh_token': 'od_m3_refresh_1',
                'expires_in': 3600,
            },
        }
        self.refresh_queue: dict[str, list[tuple[int, dict]]] = {}
        self.trashed_folders: set[str] = set()
        self.missing_folders: set[str] = set()
        self.non_folder_ids: set[str] = set()
        self.drive_files: list[dict] = []
        self.dropbox_entries: list[dict] = []
        self.onedrive_items: list[dict] = []
        self.file_contents: dict[str, bytes] = {}
        self.undeclared_stream_chunks: dict[str, list[bytes]] = {}
        self.list_status_queue: dict[str, list[tuple[int, dict]]] = {
            'google_drive': [],
            'dropbox': [],
            'onedrive': [],
        }
        self.validate_status_queue: dict[str, list[tuple[int, dict]]] = {
            'google_drive': [],
            'dropbox': [],
            'onedrive': [],
        }
        self.download_status_queue: dict[str, list[tuple[int, dict]]] = {
            'google_drive': [],
            'dropbox': [],
            'onedrive': [],
        }
        self.upload_status_queue: dict[str, list[tuple[int, dict]]] = {
            'google_drive': [],
            'dropbox': [],
            'onedrive': [],
        }
        self.uploaded_docs: list[dict] = []
        self.uploaded_md_files: list[dict] = []
        self.archived_files: list[dict] = []
        self.created_responses: list[MagicMock] = []
        self._doc_seq = 0

    def calls_matching(self, url_substring: str, method: str | None = None) -> list[dict]:
        out = []
        for c in self.calls:
            if url_substring in c['url']:
                if method is None or c['method'] == method.upper():
                    out.append(c)
        return out

    def _record_and_return(self, resp: MagicMock) -> MagicMock:
        self.created_responses.append(resp)
        return resp

    def handle(self, _session, method, url, **kwargs) -> MagicMock:
        method_u = str(method).upper()
        headers = dict(kwargs.get('headers') or {})
        params = dict(kwargs.get('params') or {})
        data = kwargs.get('data')
        json_body = kwargs.get('json')
        self.calls.append({
            'method': method_u,
            'url': url,
            'headers': headers,
            'params': params,
            'data': data,
            'json': json_body,
            'stream': kwargs.get('stream'),
            'timeout': kwargs.get('timeout'),
            'allow_redirects': kwargs.get('allow_redirects'),
        })

        parsed = urlparse(url)
        host = parsed.hostname or ''
        path = parsed.path

        token_routes = {
            ('oauth2.googleapis.com', '/token'): 'google_drive',
            ('api.dropboxapi.com', '/oauth2/token'): 'dropbox',
            ('login.microsoftonline.com', '/common/oauth2/v2.0/token'): 'onedrive',
        }
        if (host, path) in token_routes and method_u == 'POST':
            prov = token_routes[(host, path)]
            form = data if isinstance(data, dict) else {}
            if form.get('grant_type') == 'refresh_token':
                q = self.refresh_queue.get(prov)
                if q:
                    st, body = q.pop(0)
                    return self._record_and_return(_make_wire_response(st, json_data=body))
                return self._record_and_return(_make_wire_response(200, json_data={
                    'access_token': f'{prov}_refreshed_tok',
                    'expires_in': 3600,
                }))
            return self._record_and_return(_make_wire_response(200, json_data=dict(self.tokens[prov])))

        # Google Drive
        if host == 'www.googleapis.com' and path == '/upload/drive/v3/files' and method_u == 'POST':
            if self.upload_status_queue['google_drive']:
                st, body = self.upload_status_queue['google_drive'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            self._doc_seq += 1
            doc_id = f'doc_{self._doc_seq}'
            doc_url = f'https://docs.google.com/document/d/{doc_id}/edit'
            body_text = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else str(data or '')
            self.uploaded_docs.append({'doc_id': doc_id, 'doc_url': doc_url, 'body': body_text, 'headers': headers})
            return self._record_and_return(_make_wire_response(200, json_data={'id': doc_id, 'webViewLink': doc_url}))

        if host == 'www.googleapis.com' and path == '/drive/v3/files' and method_u == 'GET':
            if self.list_status_queue['google_drive']:
                st, body = self.list_status_queue['google_drive'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            return self._record_and_return(_make_wire_response(200, json_data={'files': list(self.drive_files)}))

        if host == 'www.googleapis.com' and path.startswith('/drive/v3/files/') and method_u == 'GET':
            file_id = path.split('/drive/v3/files/', 1)[1]
            if params.get('alt') == 'media' or 'alt=media' in parsed.query:
                if self.download_status_queue['google_drive']:
                    st, body = self.download_status_queue['google_drive'].pop(0)
                    return self._record_and_return(_make_wire_response(st, json_data=body))
                if file_id in self.undeclared_stream_chunks:
                    return self._record_and_return(_make_wire_response(200, chunks=self.undeclared_stream_chunks[file_id]))
                raw = self.file_contents.get(file_id, b'')
                return self._record_and_return(
                    _make_wire_response(200, content=raw, headers={'Content-Length': str(len(raw))})
                )
            if self.validate_status_queue['google_drive']:
                st, body = self.validate_status_queue['google_drive'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            if file_id in self.missing_folders:
                return self._record_and_return(_make_wire_response(404, json_data={'error': {'code': 404}}))
            seeded = next((f for f in self.drive_files if f.get('id') == file_id), None)
            if seeded is not None:
                return self._record_and_return(_make_wire_response(200, json_data={
                    **seeded,
                    'trashed': file_id in self.trashed_folders,
                }))
            mime = 'application/pdf' if file_id in self.non_folder_ids else 'application/vnd.google-apps.folder'
            return self._record_and_return(_make_wire_response(200, json_data={
                'id': file_id,
                'name': f'Folder-{file_id}',
                'mimeType': mime,
                'trashed': file_id in self.trashed_folders,
            }))

        if host == 'www.googleapis.com' and path == '/drive/v3/files' and method_u == 'POST':
            folder_name = (json_body or {}).get('name', 'Authenix Deal Briefs')
            return self._record_and_return(_make_wire_response(200, json_data={
                'id': 'auto_briefs_folder_1',
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
            }))

        if host == 'www.googleapis.com' and path.startswith('/drive/v3/files/') and method_u == 'PATCH':
            file_id = path.split('/drive/v3/files/', 1)[1]
            new_name = (json_body or {}).get('name', '')
            self.archived_files.append({
                'provider': 'google_drive',
                'file_id': file_id,
                'new_name': new_name,
            })
            return self._record_and_return(_make_wire_response(200, json_data={'id': file_id, 'name': new_name}))

        # Dropbox
        if host == 'api.dropboxapi.com' and path == '/2/files/get_metadata' and method_u == 'POST':
            req_path = (json_body or {}).get('path', '')
            if req_path in self.missing_folders:
                return self._record_and_return(_make_wire_response(409, json_data={'error_summary': 'path/not_found'}))
            tag = 'file' if req_path in self.non_folder_ids else 'folder'
            return self._record_and_return(_make_wire_response(200, json_data={
                '.tag': tag,
                'id': f'id:{req_path}',
                'name': req_path.strip('/'),
                'path_display': req_path,
            }))

        if host == 'api.dropboxapi.com' and path == '/2/files/list_folder' and method_u == 'POST':
            if self.list_status_queue['dropbox']:
                st, body = self.list_status_queue['dropbox'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            return self._record_and_return(
                _make_wire_response(200, json_data={'entries': list(self.dropbox_entries), 'has_more': False})
            )

        if host == 'api.dropboxapi.com' and path == '/2/files/move_v2' and method_u == 'POST':
            from_p = (json_body or {}).get('from_path', '')
            to_p = (json_body or {}).get('to_path', '')
            self.archived_files.append({
                'provider': 'dropbox',
                'from_path': from_p,
                'to_path': to_p,
            })
            return self._record_and_return(_make_wire_response(200, json_data={
                'metadata': {'.tag': 'file', 'path_display': to_p, 'name': to_p.rsplit('/', 1)[-1]},
            }))

        if host == 'content.dropboxapi.com' and path == '/2/files/download' and method_u == 'POST':
            if self.download_status_queue['dropbox']:
                st, body = self.download_status_queue['dropbox'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            api_arg = json.loads(headers.get('Dropbox-API-Arg', '{}'))
            target_key = api_arg.get('path') or api_arg.get('id') or ''
            if target_key in self.undeclared_stream_chunks:
                return self._record_and_return(_make_wire_response(200, chunks=self.undeclared_stream_chunks[target_key]))
            raw = self.file_contents.get(target_key, b'')
            return self._record_and_return(
                _make_wire_response(200, content=raw, headers={'Content-Length': str(len(raw))})
            )

        if host == 'content.dropboxapi.com' and path == '/2/files/upload' and method_u == 'POST':
            if self.upload_status_queue['dropbox']:
                st, body = self.upload_status_queue['dropbox'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            api_arg = json.loads(headers.get('Dropbox-API-Arg', '{}'))
            dest_path = api_arg.get('path', '/DealMemos/memo.md')
            body_text = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else str(data or '')
            self.uploaded_md_files.append({
                'provider': 'dropbox',
                'path': dest_path,
                'headers': headers,
                'api_arg': api_arg,
                'body': body_text,
            })
            return self._record_and_return(_make_wire_response(200, json_data={
                'id': f'id:dbx_{len(self.uploaded_md_files)}',
                'name': dest_path.rsplit('/', 1)[-1],
                'path_display': dest_path,
            }))

        # OneDrive
        if host == 'graph.microsoft.com' and path.endswith('/children') and method_u == 'GET':
            if self.list_status_queue['onedrive']:
                st, body = self.list_status_queue['onedrive'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            return self._record_and_return(_make_wire_response(200, json_data={'value': list(self.onedrive_items)}))

        if host == 'graph.microsoft.com' and path.endswith('/children') and method_u == 'POST':
            sub_name = (json_body or {}).get('name', 'Done')
            return self._record_and_return(_make_wire_response(201, json_data={
                'id': f'od_sub_{sub_name.lower()}',
                'name': sub_name,
                'folder': {'childCount': 0},
            }))

        if host == 'graph.microsoft.com' and path.startswith('/v1.0/me/drive/items/') and method_u == 'PATCH':
            item_id = path.split('/v1.0/me/drive/items/', 1)[1].strip('/')
            new_name = (json_body or {}).get('name', '')
            parent_ref = (json_body or {}).get('parentReference', {})
            self.archived_files.append({
                'provider': 'onedrive',
                'file_id': item_id,
                'new_name': new_name,
                'parent_id': parent_ref.get('id'),
            })
            return self._record_and_return(_make_wire_response(200, json_data={'id': item_id, 'name': new_name}))

        if host == 'graph.microsoft.com' and path.endswith('/content') and method_u == 'GET':
            if self.download_status_queue['onedrive']:
                st, body = self.download_status_queue['onedrive'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            item_id = path.split('/')[-2]
            if item_id in self.undeclared_stream_chunks:
                return self._record_and_return(_make_wire_response(200, chunks=self.undeclared_stream_chunks[item_id]))
            raw = self.file_contents.get(item_id, b'')
            return self._record_and_return(
                _make_wire_response(200, content=raw, headers={'Content-Length': str(len(raw))})
            )

        if host == 'graph.microsoft.com' and path.endswith(':/content') and method_u == 'PUT':
            if self.upload_status_queue['onedrive']:
                st, body = self.upload_status_queue['onedrive'].pop(0)
                return self._record_and_return(_make_wire_response(st, json_data=body))
            decoded_path = unquote(path)
            filename = decoded_path.split(':/')[-2] if ':/' in decoded_path else 'memo.md'
            body_text = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else str(data or '')
            web_url = f'https://onedrive.live.com/view/od_doc_{len(self.uploaded_md_files) + 1}'
            self.uploaded_md_files.append({
                'provider': 'onedrive',
                'path': decoded_path,
                'filename': filename,
                'headers': headers,
                'body': body_text,
                'webUrl': web_url,
            })
            return self._record_and_return(_make_wire_response(201, json_data={
                'id': f'od_doc_{len(self.uploaded_md_files)}',
                'name': filename,
                'webUrl': web_url,
            }))

        if host == 'graph.microsoft.com' and method_u == 'GET' and path.startswith('/v1.0/me/drive/items/'):
            item_id = path.split('/v1.0/me/drive/items/', 1)[1].strip('/')
            if item_id in self.missing_folders:
                return self._record_and_return(_make_wire_response(404, json_data={'error': {'code': 'itemNotFound'}}))
            payload = {'id': item_id, 'name': f'Folder-{item_id}'}
            if item_id not in self.non_folder_ids:
                payload['folder'] = {'childCount': 0}
            else:
                payload['file'] = {'mimeType': 'application/pdf'}
            return self._record_and_return(_make_wire_response(200, json_data=payload))

        return self._record_and_return(_make_wire_response(404, json_data={'error': 'unhandled'}))


@pytest.fixture
def sync_wire(monkeypatch):
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_ID', 'test-g-id', raising=False)
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_SECRET', 'test-g-sec', raising=False)
    monkeypatch.setattr(Config, 'DROPBOX_CLIENT_ID', 'test-d-id', raising=False)
    monkeypatch.setattr(Config, 'DROPBOX_CLIENT_SECRET', 'test-d-sec', raising=False)
    monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_ID', 'test-o-id', raising=False)
    monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_SECRET', 'test-o-sec', raising=False)
    monkeypatch.setattr(Config, 'CLOUD_OAUTH_REDIRECT_BASE', None, raising=False)

    svc = get_pitchdeck_service()
    prev_gemini_client = svc.client
    router = _SyncWireRouter()
    orig_getaddrinfo = socket.getaddrinfo

    def _dns(host, port, *args, **kwargs):
        host_str = host.decode() if isinstance(host, bytes) else str(host or '')
        if host_str.strip('[]').lower() in PROVIDER_HOSTS:
            port_i = int(port) if str(port).isdigit() else 443
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (PUBLIC_TEST_IP, port_i))]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    try:
        with patch('socket.getaddrinfo', side_effect=_dns), \
             patch('requests.Session.request', autospec=True, side_effect=router.handle):
            yield router
    finally:
        svc.client = prev_gemini_client


def _connect(client, provider: str, code: str = 'code_m3'):
    r1 = client.get(f'/api/storage/{provider}/connect')
    assert r1.status_code == 302
    state = parse_qs(urlparse(r1.headers['Location']).query)['state'][0]
    r2 = client.get(f'/api/storage/{provider}/callback?code={code}&state={state}')
    assert r2.status_code == 302
    assert r2.headers['Location'] == f'/?storage_connected={provider}'


def _default_folders(provider: str) -> tuple[str, str]:
    if provider == 'google_drive':
        return 'drv_in_1', 'drv_out_1'
    if provider == 'dropbox':
        return '/PitchDecks', '/DealMemos'
    return 'od_in_1', 'od_out_1'


def _configure(
    client,
    provider: str,
    in_ref: str | None = None,
    out_ref: str | None = None,
    **extra,
):
    d_in, d_out = _default_folders(provider)
    body = {
        'input_folder_ref': in_ref if in_ref is not None else d_in,
        'output_folder_ref': out_ref if out_ref is not None else d_out,
        **extra,
    }
    resp = client.put(f'/api/storage/{provider}/config', json=body)
    assert resp.status_code == 200
    return resp.get_json()


def _seed_file(
    router: _SyncWireRouter,
    provider: str,
    file_id: str,
    name: str,
    raw_bytes: bytes,
    *,
    etag: str = 'v1',
    mime_type: str = 'application/pdf',
    declared_size: int | None = None,
    folder_path: str = '/PitchDecks',
    is_folder: bool = False,
    undeclared_chunks: list[bytes] | None = None,
):
    size_val = declared_size if declared_size is not None else len(raw_bytes)
    if provider == 'google_drive':
        router.drive_files.append({
            'id': file_id,
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder' if is_folder else mime_type,
            'size': str(size_val),
            'modifiedTime': f'2026-11-21T10:00:00Z#{etag}',
            'md5Checksum': etag,
        })
        if undeclared_chunks is not None:
            router.undeclared_stream_chunks[file_id] = undeclared_chunks
        else:
            router.file_contents[file_id] = raw_bytes
    elif provider == 'dropbox':
        full_path = f"{folder_path.rstrip('/')}/{name}"
        router.dropbox_entries.append({
            '.tag': 'folder' if is_folder else 'file',
            'id': file_id,
            'name': name,
            'path_lower': full_path.lower(),
            'path_display': full_path,
            'size': size_val,
            'rev': etag,
            'content_hash': etag,
            'server_modified': '2026-11-21T10:00:00Z',
        })
        if undeclared_chunks is not None:
            router.undeclared_stream_chunks[full_path] = undeclared_chunks
        else:
            router.file_contents[full_path] = raw_bytes
    elif provider == 'onedrive':
        entry = {
            'id': file_id,
            'name': name,
            'size': size_val,
            'eTag': etag,
            'lastModifiedDateTime': '2026-11-21T10:00:00Z',
        }
        if is_folder:
            entry['folder'] = {'childCount': 0}
        else:
            entry['file'] = {'mimeType': mime_type}
        router.onedrive_items.append(entry)
        if undeclared_chunks is not None:
            router.undeclared_stream_chunks[file_id] = undeclared_chunks
        else:
            router.file_contents[file_id] = raw_bytes


def _create_user_client(app, db_session_fixture, email: str, *, balance: int = 100, with_balance: bool = True):
    pw_hash = bcrypt.hashpw(b'testpass123', bcrypt.gensalt(rounds=4)).decode('utf-8')
    user = User(email=email, password_hash=pw_hash)
    db_session_fixture.add(user)
    db_session_fixture.commit()
    if with_balance:
        db_session_fixture.add(TokenBalance(user_id=user.id, balance=balance))
        db_session_fixture.commit()
    c = app.test_client()
    c.set_cookie(JWT_COOKIE_NAME, create_token(user.id, user.email), domain='localhost')
    return user, c


class TestSafeHttpAndCompositionInvariants:
    """Verify bounded response reading and PitchdeckService composition invariant."""

    def test_pitchdeck_service_is_composed_not_subclassed(self):
        import backend.services.folder_sync_service as fss

        svc = get_pitchdeck_service()
        assert isinstance(svc, GeminiServicePitchdeck)
        subclasses = [
            obj for obj in vars(fss).values()
            if isinstance(obj, type) and issubclass(obj, GeminiServicePitchdeck)
        ]
        assert subclasses == []

    def test_read_bounded_response_enforces_header_and_stream_caps_and_closes(self):
        resp_hdr = _make_wire_response(200, content=b'abc', headers={'Content-Length': str(MAX_PDF_BYTES + 1)})
        with pytest.raises(ResponseTooLargeError):
            read_bounded_response(resp_hdr, max_bytes=MAX_PDF_BYTES, total_timeout=5.0)
        resp_hdr.close.assert_called_once()

        resp_stream = _make_wire_response(200, chunks=[b'A' * 600, b'B' * 600])
        with pytest.raises(ResponseTooLargeError):
            read_bounded_response(resp_stream, max_bytes=1024, total_timeout=5.0)
        resp_stream.close.assert_called_once()

        resp_ok = _make_wire_response(200, chunks=[b'%PDF-', b'1.4'], headers={'Content-Length': '8'})
        data = read_bounded_response(resp_ok, max_bytes=1024, total_timeout=5.0)
        assert data == b'%PDF-1.4'
        resp_ok.close.assert_called_once()


class TestFolderConfigValidation:
    """Verify PUT /api/storage/<provider>/config across all 3 providers and validation boundaries."""

    def test_config_requires_auth_and_connected_provider(self, client, auth_client, sync_wire):
        unauth = client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
        })
        assert unauth.status_code == 401
        assert unauth.get_json()['success'] is False

        no_conn = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
        })
        assert no_conn.status_code == 401
        assert no_conn.get_json()['needs_reauth'] is True
        assert sync_wire.calls == []

    @pytest.mark.parametrize(
        'in_url,expected_in,out_url,expected_out',
        [
            (
                'https://drive.google.com/drive/folders/1InFolderId999',
                '1InFolderId999',
                'https://drive.google.com/drive/u/2/folders/1OutFolderId888?usp=sharing',
                '1OutFolderId888',
            ),
            (
                'https://drive.google.com/drive/u/0/folders/in1/',
                'in1',
                'https://drive.google.com/drive/folders/out1',
                'out1',
            ),
        ],
    )
    def test_google_drive_url_to_id_normalization_and_metadata_validation(
        self, auth_client, db_session_fixture, test_user, sync_wire, in_url, expected_in, out_url, expected_out
    ):
        _connect(auth_client, 'google_drive')
        sync_wire.calls.clear()

        cfg = _configure(
            auth_client, 'google_drive', in_url, out_url,
            memo_preset='extended',
            daily_sync_enabled=True,
            auto_export_sheets=True,
            sheets_spreadsheet_id='https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit#gid=0',
            sheets_sheet_name='Deal Flow',
        )
        assert cfg['input_folder_ref'] == expected_in
        assert cfg['output_folder_ref'] == expected_out
        assert cfg['memo_preset'] == 'extended'
        assert cfg['daily_sync_enabled'] is True
        assert cfg['auto_export_sheets'] is True
        assert cfg['sheets_spreadsheet_id'] == '1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms'
        assert cfg['sheets_sheet_name'] == 'Deal Flow'

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.input_folder_ref == expected_in
        assert conn.output_folder_ref == expected_out
        assert len(sync_wire.calls_matching(f'/drive/v3/files/{expected_in}', 'GET')) == 1
        assert len(sync_wire.calls_matching(f'/drive/v3/files/{expected_out}', 'GET')) == 1

    def test_rejects_identical_folders_across_drive_urls_and_dropbox_case_slash(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        _connect(auth_client, 'google_drive')
        _connect(auth_client, 'dropbox')
        sync_wire.calls.clear()

        r_drv = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'https://drive.google.com/drive/u/0/folders/1AbCdEfGhIjK?usp=sharing',
            'output_folder_ref': '1AbCdEfGhIjK',
        })
        assert r_drv.status_code == 400
        assert r_drv.get_json()['success'] is False

        r_dbx = auth_client.put('/api/storage/dropbox/config', json={
            'input_folder_ref': '/PitchDecks/',
            'output_folder_ref': '/pitchdecks',
        })
        assert r_dbx.status_code == 400
        assert r_dbx.get_json()['success'] is False
        assert sync_wire.calls == []

        db_session_fixture.expire_all()
        for prov in ('google_drive', 'dropbox'):
            c = db_session_fixture.query(CloudStorageConnection).filter_by(
                user_id=test_user.id, provider=prov
            ).one()
            assert c.input_folder_ref is None
            assert c.output_folder_ref is None

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_rejects_trashed_missing_or_non_folder_upstream(
        self, auth_client, db_session_fixture, test_user, sync_wire, provider
    ):
        _connect(auth_client, provider)
        in_f, out_f = _default_folders(provider)
        if provider == 'google_drive':
            sync_wire.trashed_folders.add(in_f)
        else:
            sync_wire.missing_folders.add(in_f)

        r_bad = auth_client.put(f'/api/storage/{provider}/config', json={
            'input_folder_ref': in_f,
            'output_folder_ref': out_f,
        })
        assert r_bad.status_code == 400
        assert r_bad.get_json()['success'] is False

        sync_wire.trashed_folders.clear()
        sync_wire.missing_folders.clear()
        sync_wire.non_folder_ids.add(out_f)
        r_file = auth_client.put(f'/api/storage/{provider}/config', json={
            'input_folder_ref': in_f,
            'output_folder_ref': out_f,
        })
        assert r_file.status_code == 400
        assert r_file.get_json()['success'] is False

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider=provider
        ).one()
        assert conn.input_folder_ref is None

    @pytest.mark.parametrize(
        'bad_sheet_id',
        [
            'abc',
            '../etc/passwd',
            'https://evil.example.com/spreadsheets/d/12345678901234567890',
            '1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms?foo=bar',
        ],
    )
    def test_rejects_malformed_sheets_spreadsheet_id(
        self, auth_client, db_session_fixture, test_user, sync_wire, bad_sheet_id
    ):
        _connect(auth_client, 'google_drive')
        sync_wire.calls.clear()
        resp = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': bad_sheet_id,
        })
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert sync_wire.calls == []
        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.sheets_spreadsheet_id is None

    @pytest.mark.parametrize(
        'bad_name',
        ['Bad:Name', 'Sheet[1]', 'A*B', 'X/Y', "A'B!C1:D2", 'S' * 101],
    )
    def test_rejects_invalid_sheets_sheet_name(
        self, auth_client, db_session_fixture, test_user, sync_wire, bad_name
    ):
        _connect(auth_client, 'google_drive')
        sync_wire.calls.clear()
        resp = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': '1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms',
            'sheets_sheet_name': bad_name,
        })
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert sync_wire.calls == []
        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.sheets_sheet_name is None

    def test_rejects_dropbox_traversal_and_invalid_onedrive_ids(self, auth_client, sync_wire):
        _connect(auth_client, 'dropbox')
        _connect(auth_client, 'onedrive')

        r_dbx = auth_client.put('/api/storage/dropbox/config', json={
            'input_folder_ref': '/../secret',
            'output_folder_ref': '/DealMemos',
        })
        assert r_dbx.status_code == 400

        r_od = auth_client.put('/api/storage/onedrive/config', json={
            'input_folder_ref': '../bad',
            'output_folder_ref': 'od_out_1',
        })
        assert r_od.status_code == 400
        assert sync_wire.calls_matching('get_metadata') == []
        assert sync_wire.calls_matching('/v1.0/me/drive/items/') == []


class TestMultiProviderSyncAndPreflight:
    """Verify manual sync, PDF discovery/pre-flight, deduplication, and Zero-File purge."""

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_manual_sync_happy_path_all_providers_with_ledger_and_memo_format(
        self, auth_client, db_session_fixture, test_user, sync_wire, provider
    ):
        mock_gemini = _install_gemini_mock()
        _connect(auth_client, provider)
        in_f, out_f = _default_folders(provider)
        _configure(auth_client, provider, in_f, out_f, memo_preset='extended')
        _seed_file(sync_wire, provider, 'f_ok', 'deck.pdf', _make_pdf(12))
        _seed_file(sync_wire, provider, 'f_png', 'logo.png', b'PNG', mime_type='image/png')
        _seed_file(sync_wire, provider, 'f_dir', 'Nested', b'', is_folder=True)

        resp = auth_client.post(f'/api/storage/{provider}/sync')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['provider'] == provider
        assert data['processed'] == 1
        assert data['skipped_duplicate'] == 0
        assert data['skipped_invalid'] == 0
        assert data['skipped_insufficient_cp'] == 0
        assert data['errors'] == 0
        assert data['new_balance'] == 95  # 4 CP analysis + 1 CP sync fee

        mock_gemini.models.generate_content.assert_called_once()
        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 95
        tx = db_session_fixture.query(Transaction).filter_by(user_id=test_user.id, type='deduction').one()
        assert tx.amount == -5
        recs = db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=test_user.id).all()
        assert len(recs) == 1
        assert recs[0].status == 'completed'
        assert recs[0].cp_charged == 5
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider=provider
        ).one()
        assert conn.last_synced_at is not None

        if provider == 'google_drive':
            assert len(sync_wire.uploaded_docs) == 1
            assert recs[0].exported_doc_url == sync_wire.uploaded_docs[0]['doc_url']
            assert 'Burn Multiple' in sync_wire.uploaded_docs[0]['body']
        else:
            assert len(sync_wire.uploaded_md_files) == 1
            md = sync_wire.uploaded_md_files[0]
            assert md['provider'] == provider
            assert 'Burn Multiple' in md['body']
            assert '&amp;' not in md['body']
            if provider == 'onedrive':
                assert md['filename'].endswith('.md')
                assert md['headers']['Content-Type'] == 'text/markdown; charset=utf-8'
                assert recs[0].exported_doc_url == md['webUrl']
            else:
                assert md['path'].endswith('.md')
                assert recs[0].exported_doc_url.startswith('https://www.dropbox.com/home/')

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_sync_before_folders_configured_returns_400_with_zero_wire_calls(
        self, auth_client, db_session_fixture, test_user, sync_wire, provider
    ):
        _connect(auth_client, provider)
        sync_wire.calls.clear()

        resp = auth_client.post(f'/api/storage/{provider}/sync')
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert sync_wire.calls == []
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 100

    def test_trashed_folder_at_sync_time_returns_400_and_sets_last_error(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        mock_gemini = _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')
        sync_wire.trashed_folders.add('drv_in_1')

        resp = auth_client.post('/api/storage/google_drive/sync')
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.last_error == 'Configured folder is invalid or inaccessible'
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 100
        mock_gemini.models.generate_content.assert_not_called()

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_preflight_rejects_metadata_oversize_and_stream_oversize_and_zero_byte(
        self, auth_client, db_session_fixture, test_user, sync_wire, provider
    ):
        mock_gemini = _install_gemini_mock()
        _connect(auth_client, provider)
        in_f, out_f = _default_folders(provider)
        _configure(auth_client, provider, in_f, out_f)

        _seed_file(sync_wire, provider, 'f_zero', 'zero.pdf', b'', declared_size=0)
        _seed_file(sync_wire, provider, 'f_big_meta', 'big.pdf', b'', declared_size=30 * 1024 * 1024)
        _seed_file(
            sync_wire, provider, 'f_stream_big', 'stream.pdf', b'',
            declared_size=1024,
            undeclared_chunks=[b'%PDF-1.4\n' + b'X' * (10 * 1024 * 1024), b'Y' * (16 * 1024 * 1024)],
        )
        _seed_file(sync_wire, provider, 'f_bad_magic', 'bad.pdf', b'PNG\x89notpdf')
        _seed_file(sync_wire, provider, 'f_101', '101.pdf', _make_pdf(101))
        _seed_file(sync_wire, provider, 'f_spoof', 'spoof.pdf', _make_spoofed_count_pdf())
        sync_wire.calls.clear()

        resp = auth_client.post(f'/api/storage/{provider}/sync')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] == 0
        assert data['skipped_invalid'] == 6
        assert data['new_balance'] == 100

        mock_gemini.models.generate_content.assert_not_called()
        # Only the 4 files passing metadata size check were downloaded (zero-byte & >25MB meta skipped pre-download)
        if provider == 'google_drive':
            dl_calls = [
                c for c in sync_wire.calls_matching('/drive/v3/files/', 'GET')
                if c['params'].get('alt') == 'media'
            ]
            assert len(dl_calls) == 4
        elif provider == 'dropbox':
            assert len(sync_wire.calls_matching('/2/files/download', 'POST')) == 4
        else:
            assert len(sync_wire.calls_matching('/content', 'GET')) == 4

        db_session_fixture.expire_all()
        recs = db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=test_user.id).all()
        assert len(recs) == 6
        assert all(r.status == 'skipped_invalid' and r.cp_charged == 0 for r in recs)

    def test_large_15mb_pdf_uses_files_api_and_purges_on_success_and_error(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        mock_ok = _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')
        large_pdf = _make_exact_size_pdf(15 * 1024 * 1024, pages=10)
        _seed_file(sync_wire, 'google_drive', 'f_15_ok', 'large_ok.pdf', large_pdf, etag='v1')

        r1 = auth_client.post('/api/storage/google_drive/sync')
        assert r1.status_code == 200
        assert r1.get_json()['processed'] == 1
        assert r1.get_json()['new_balance'] == 97
        mock_ok.files.upload.assert_called_once()
        mock_ok.files.delete.assert_called_once_with(name='files/m3-large-pdf-1')

        sync_wire.drive_files.clear()
        mock_err = _install_gemini_mock(RuntimeError('Gemini vision error'))
        _seed_file(sync_wire, 'google_drive', 'f_15_err', 'large_err.pdf', large_pdf, etag='v1')

        r2 = auth_client.post('/api/storage/google_drive/sync')
        assert r2.status_code == 200
        assert r2.get_json()['errors'] == 1
        assert r2.get_json()['new_balance'] == 97
        mock_err.files.upload.assert_called_once()
        mock_err.files.delete.assert_called_once_with(name='files/m3-large-pdf-1')

    def test_dedup_skips_completed_and_invalid_and_retries_failed_in_place_and_reanalyzes_new_etag(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        mock_gemini = _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')

        _seed_file(sync_wire, 'google_drive', 'f_done', 'done.pdf', _make_pdf(8), etag='v1')
        _seed_file(sync_wire, 'google_drive', 'f_bad', 'bad.pdf', _make_pdf(101), etag='v1')
        r1 = auth_client.post('/api/storage/google_drive/sync')
        assert r1.status_code == 200
        assert r1.get_json()['processed'] == 1
        assert r1.get_json()['skipped_invalid'] == 1

        sync_wire.calls.clear()
        r2 = auth_client.post('/api/storage/google_drive/sync')
        assert r2.status_code == 200
        assert r2.get_json()['processed'] == 0
        assert r2.get_json()['skipped_duplicate'] == 2
        assert len(sync_wire.calls_matching('alt=media')) == 0
        assert mock_gemini.models.generate_content.call_count == 1

        # New ETag triggers fresh analysis and second record
        sync_wire.drive_files.clear()
        _seed_file(sync_wire, 'google_drive', 'f_done', 'done.pdf', _make_pdf(8), etag='v2')
        r3 = auth_client.post('/api/storage/google_drive/sync')
        assert r3.status_code == 200
        assert r3.get_json()['processed'] == 1
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, external_file_id='f_done'
        ).count() == 2

        # Transient error & insufficient_cp remain retryable and update existing row in place
        sync_wire.drive_files.clear()
        _install_gemini_mock(RuntimeError('Transient crash'))
        _seed_file(sync_wire, 'google_drive', 'f_retry', 'retry.pdf', _make_pdf(8), etag='v1')
        r4 = auth_client.post('/api/storage/google_drive/sync')
        assert r4.status_code == 200
        assert r4.get_json()['errors'] == 1
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, external_file_id='f_retry'
        ).one().status == 'error'

        _install_gemini_mock()
        r5 = auth_client.post('/api/storage/google_drive/sync')
        assert r5.status_code == 200
        assert r5.get_json()['processed'] == 1
        rec_retry = db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, external_file_id='f_retry'
        ).one()
        assert rec_retry.status == 'completed'
        assert rec_retry.cp_charged == 3


class TestCPBillingAutoRefillAndRefunds:
    """Verify CP formula, non-overdraw, missing TokenBalance, auto-refill, and refund ledger."""

    @pytest.mark.parametrize(
        'pages,expected_cp',
        [(1, 3), (10, 3), (11, 5), (25, 7), (51, 13), (60, 13), (100, 13)],
    )
    def test_cp_formula_and_zero_count_clamp(
        self, auth_client, db_session_fixture, test_user, sync_wire, pages, expected_cp
    ):
        assert calculate_sync_cp(pages) == expected_cp
        _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')
        _seed_file(sync_wire, 'google_drive', f'f_{pages}', f'd_{pages}.pdf', _make_pdf(pages))

        resp = auth_client.post('/api/storage/google_drive/sync')
        assert resp.status_code == 200
        assert resp.get_json()['new_balance'] == 100 - expected_cp
        db_session_fixture.expire_all()
        rec = db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, external_file_id=f'f_{pages}'
        ).one()
        assert rec.cp_charged == expected_cp

    def test_zero_count_tag_clamps_to_1_page_3_cp(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')
        _seed_file(sync_wire, 'google_drive', 'f_z', 'z.pdf', _make_zero_count_pdf())

        resp = auth_client.post('/api/storage/google_drive/sync')
        assert resp.status_code == 200
        assert resp.get_json()['new_balance'] == 97
        rec = db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, external_file_id='f_z'
        ).one()
        assert rec.cp_charged == 3

    def test_missing_token_balance_row_not_auto_created(self, app, db_session_fixture, sync_wire):
        mock_gemini = _install_gemini_mock()
        user, client = _create_user_client(app, db_session_fixture, 'nobal@example.com', with_balance=False)
        _connect(client, 'google_drive')
        _configure(client, 'google_drive', 'drv_in_1', 'drv_out_1')
        _seed_file(sync_wire, 'google_drive', 'f_nb', 'deck.pdf', _make_pdf(8))

        resp = client.post('/api/storage/google_drive/sync')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] == 0
        assert data['skipped_insufficient_cp'] == 1
        assert data['new_balance'] == 0

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=user.id).first() is None
        rec = db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=user.id, external_file_id='f_nb').one()
        assert rec.status == 'insufficient_cp'
        mock_gemini.models.generate_content.assert_not_called()

    def test_insufficient_cp_mid_batch_records_without_overdraw_and_retries_after_topup(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        mock_gemini = _install_gemini_mock()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 3
        db_session_fixture.commit()

        _connect(auth_client, 'dropbox')
        _configure(auth_client, 'dropbox', '/PitchDecks', '/DealMemos')
        _seed_file(sync_wire, 'dropbox', 'f_1', '01.pdf', _make_pdf(10))
        _seed_file(sync_wire, 'dropbox', 'f_2', '02.pdf', _make_pdf(10))

        r1 = auth_client.post('/api/storage/dropbox/sync')
        assert r1.status_code == 200
        d1 = r1.get_json()
        assert d1['processed'] == 1
        assert d1['skipped_insufficient_cp'] == 1
        assert d1['new_balance'] == 0
        assert mock_gemini.models.generate_content.call_count == 1

        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 10
        db_session_fixture.commit()

        r2 = auth_client.post('/api/storage/dropbox/sync')
        assert r2.status_code == 200
        d2 = r2.get_json()
        assert d2['processed'] == 1
        assert d2['skipped_duplicate'] == 1
        assert d2['new_balance'] == 7
        db_session_fixture.expire_all()
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, status='completed'
        ).count() == 2

    def test_auto_refill_succeeds_and_card_error_disables_refill(
        self, app, db_session_fixture, sync_wire
    ):
        _install_gemini_mock()
        user_ok, client_ok = _create_user_client(app, db_session_fixture, 'refill_ok@example.com', balance=1)
        bal_ok = db_session_fixture.query(TokenBalance).filter_by(user_id=user_ok.id).one()
        bal_ok.auto_refill_enabled = True
        bal_ok.auto_refill_package = 'battery_medium'
        bal_ok.stripe_customer_id = 'cus_ok'
        bal_ok.stripe_payment_method_id = 'pm_ok'
        db_session_fixture.commit()

        _connect(client_ok, 'google_drive')
        _configure(client_ok, 'google_drive', 'drv_in_1', 'drv_out_1')
        _seed_file(sync_wire, 'google_drive', 'f_rf_ok', 'ok.pdf', _make_pdf(8))

        mock_intent = MagicMock(id='pi_m3_ok', status='succeeded')
        with patch('stripe.PaymentIntent.create', return_value=mock_intent) as mock_pi:
            r_ok = client_ok.post('/api/storage/google_drive/sync')
        assert r_ok.status_code == 200
        assert r_ok.get_json()['processed'] == 1
        assert r_ok.get_json()['new_balance'] == 448
        mock_pi.assert_called_once()

        user_bad, client_bad = _create_user_client(app, db_session_fixture, 'refill_bad@example.com', balance=1)
        bal_bad = db_session_fixture.query(TokenBalance).filter_by(user_id=user_bad.id).one()
        bal_bad.auto_refill_enabled = True
        bal_bad.auto_refill_package = 'battery_medium'
        bal_bad.stripe_customer_id = 'cus_bad'
        bal_bad.stripe_payment_method_id = 'pm_bad'
        db_session_fixture.commit()

        _connect(client_bad, 'google_drive')
        _configure(client_bad, 'google_drive', 'drv_in_1', 'drv_out_1')

        with patch('stripe.PaymentIntent.create', side_effect=stripe.error.CardError('Declined', 'pm_bad', 'card_declined')):
            r_bad = client_bad.post('/api/storage/google_drive/sync')
        assert r_bad.status_code == 200
        assert r_bad.get_json()['processed'] == 0
        assert r_bad.get_json()['skipped_insufficient_cp'] == 1
        db_session_fixture.expire_all()
        updated = db_session_fixture.query(TokenBalance).filter_by(user_id=user_bad.id).one()
        assert updated.balance == 1
        assert updated.auto_refill_enabled is False
        assert updated.auto_refill_failed is True

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_upload_503_failure_refunds_cp_and_writes_net_zero_ledger(
        self, auth_client, db_session_fixture, test_user, sync_wire, provider
    ):
        _install_gemini_mock()
        _connect(auth_client, provider)
        in_f, out_f = _default_folders(provider)
        _configure(auth_client, provider, in_f, out_f)
        _seed_file(sync_wire, provider, 'f_up_fail', 'deck.pdf', _make_pdf(8))
        sync_wire.upload_status_queue[provider].append((503, {'error': 'Upstream outage'}))

        resp = auth_client.post(f'/api/storage/{provider}/sync')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] == 0
        assert data['errors'] == 1
        assert data['new_balance'] == 100

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 100
        rec = db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, external_file_id='f_up_fail'
        ).one()
        assert rec.status == 'error'
        assert rec.cp_charged == 0
        txs = db_session_fixture.query(Transaction).filter_by(user_id=test_user.id).all()
        assert [t.type for t in txs] == ['deduction', 'refund']
        assert sum(t.amount for t in txs) == 0

    def test_auto_export_sheets_hook_is_non_fatal_when_m4_absent(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        _install_gemini_mock()
        _connect(auth_client, 'dropbox')
        _configure(
            auth_client, 'dropbox', '/PitchDecks', '/DealMemos',
            auto_export_sheets=True,
            sheets_spreadsheet_id='1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms',
            sheets_sheet_name='Pipeline',
        )
        _seed_file(sync_wire, 'dropbox', 'f_m4_hook', 'deck.pdf', _make_pdf(8))

        resp = auth_client.post('/api/storage/dropbox/sync')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] == 1
        assert data['errors'] == 0
        assert data['new_balance'] == 97
        rec = db_session_fixture.query(SyncedDeckRecord).filter_by(
            user_id=test_user.id, external_file_id='f_m4_hook'
        ).one()
        assert rec.status == 'completed'
        assert rec.exported_sheet_range is None
        assert rec.cp_charged == 3


class TestTokenLifecyclePIILoggingAndDailyCron:
    """Verify 401 refresh + stream close, InvalidToken recovery, 500 non-PII, and daily cron."""

    def test_401_on_streaming_download_closes_response_refreshes_and_succeeds(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')
        _seed_file(sync_wire, 'google_drive', 'f_dl401', 'deck.pdf', _make_pdf(8))
        sync_wire.download_status_queue['google_drive'].append((401, {'error': 'expired'}))
        sync_wire.refresh_queue['google_drive'] = [
            (200, {'access_token': 'ya29.m3_dl_refreshed', 'expires_in': 3600})
        ]

        resp = auth_client.post('/api/storage/google_drive/sync')
        assert resp.status_code == 200
        assert resp.get_json()['processed'] == 1
        assert resp.get_json()['new_balance'] == 97

        # Verify every 401 response created during download was explicitly closed
        closed_401s = [r for r in sync_wire.created_responses if r.status_code == 401]
        assert len(closed_401s) == 1
        closed_401s[0].close.assert_called_once()

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert decrypt_token(conn.encrypted_access_token) == 'ya29.m3_dl_refreshed'

    def test_double_401_and_invalid_token_purge_connection_and_return_401(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        _connect(auth_client, 'dropbox')
        _configure(auth_client, 'dropbox', '/PitchDecks', '/DealMemos')
        sync_wire.list_status_queue['dropbox'].extend([
            (401, {'error': 'expired'}),
            (401, {'error': 'still_bad'}),
        ])

        r_401 = auth_client.post('/api/storage/dropbox/sync')
        assert r_401.status_code == 401
        assert r_401.get_json()['needs_reauth'] is True
        db_session_fixture.expire_all()
        assert db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='dropbox'
        ).count() == 0

        _connect(auth_client, 'onedrive')
        _configure(auth_client, 'onedrive', 'od_in_1', 'od_out_1')
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='onedrive'
        ).one()
        conn.encrypted_access_token = 'corrupted-fernet-token'
        db_session_fixture.commit()

        r_inv = auth_client.post('/api/storage/onedrive/sync')
        assert r_inv.status_code == 401
        assert r_inv.get_json()['needs_reauth'] is True
        db_session_fixture.expire_all()
        assert db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='onedrive'
        ).count() == 0
        audit = db_session_fixture.query(AuditLog).filter_by(
            user_id=test_user.id, provider='onedrive', event_type='storage.invalid_token'
        ).one()
        assert audit.detail == 'invalid_access_token'

    @pytest.mark.parametrize('provider', ['google_drive', 'dropbox', 'onedrive'])
    def test_upstream_500_list_error_returns_200_errors_1_and_leaks_zero_pii(
        self, auth_client, db_session_fixture, test_user, sync_wire, caplog, provider
    ):
        _connect(auth_client, provider)
        in_f, out_f = _default_folders(provider)
        _configure(auth_client, provider, in_f, out_f)
        sync_wire.list_status_queue[provider].append(
            (500, {'error': 'secret-user@example.com denied', 'token': 'ya29.secret_leak'})
        )

        with caplog.at_level(logging.DEBUG):
            resp = auth_client.post(f'/api/storage/{provider}/sync')

        assert resp.status_code == 200
        assert resp.get_json()['errors'] == 1
        assert 'secret-user@example.com' not in caplog.text
        assert 'ya29.secret_leak' not in caplog.text
        assert test_user.email not in caplog.text

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider=provider
        ).one()
        assert conn.last_error == 'Failed to list input folder'
        assert conn.last_synced_at is not None

    def test_daily_cron_enforces_cap_5_filters_enabled_and_isolates_tenants(
        self, app, client, db_session_fixture, sync_wire
    ):
        assert MAX_SYNC_FILES_PER_RUN == 5
        mock_gemini = _install_gemini_mock()

        u1, c1 = _create_user_client(app, db_session_fixture, 'cron_a@example.com', balance=50)
        u2, c2 = _create_user_client(app, db_session_fixture, 'cron_b@example.com', balance=0)
        u3, c3 = _create_user_client(app, db_session_fixture, 'cron_c@example.com', balance=50)

        _connect(c1, 'google_drive')
        _configure(c1, 'google_drive', 'drv_in_1', 'drv_out_1', daily_sync_enabled=True)
        _connect(c2, 'dropbox')
        _configure(c2, 'dropbox', '/PitchDecks', '/DealMemos', daily_sync_enabled=True)
        _connect(c3, 'onedrive')
        _configure(c3, 'onedrive', 'od_in_1', 'od_out_1', daily_sync_enabled=False)

        for idx in range(1, 8):
            _seed_file(sync_wire, 'google_drive', f'f_g_{idx}', f'g_{idx}.pdf', _make_pdf(5))
        _seed_file(sync_wire, 'dropbox', 'f_d_1', 'd_1.pdf', _make_pdf(5))
        _seed_file(sync_wire, 'onedrive', 'f_o_1', 'o_1.pdf', _make_pdf(5))
        sync_wire.calls.clear()

        t1 = client.post('/api/cron/sync-watched-folders', headers={'X-Scheduler-Secret': Config.SCHEDULER_SECRET})
        assert t1.status_code == 200
        d1 = t1.get_json()
        assert d1['success'] is True
        assert d1['connections_checked'] == 2
        assert d1['processed'] == 5
        assert d1['skipped_insufficient_cp'] == 1
        assert d1['errors'] == 0
        assert sync_wire.calls_matching('graph.microsoft.com') == []

        t2 = client.post('/api/cron/sync-watched-folders', headers={'X-Scheduler-Secret': Config.SCHEDULER_SECRET})
        assert t2.status_code == 200
        d2 = t2.get_json()
        assert d2['processed'] == 2
        assert d2['skipped_insufficient_cp'] == 1

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=u1.id).one().balance == 29
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=u2.id).one().balance == 0
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=u3.id).one().balance == 50
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=u1.id, status='completed').count() == 7
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=u2.id, status='insufficient_cp').count() == 1
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=u3.id).count() == 0
        assert mock_gemini.models.generate_content.call_count == 7


class TestIteration2RemediationRegressions:
    """Regression tests for Iteration 2 security, validation, and lifecycle fixes."""

    @pytest.mark.parametrize('encoded', ['%00', '%0a', '%0d', '%7f'])
    def test_dropbox_percent_encoded_control_bytes_rejected_400(
        self, auth_client, db_session_fixture, test_user, sync_wire, encoded
    ):
        bad_url = f'https://www.dropbox.com/home/Decks{encoded}Evil'
        with pytest.raises(ProviderFolderError):
            DropboxProvider().normalize_folder_ref(bad_url)

        _connect(auth_client, 'dropbox')
        sync_wire.calls.clear()

        resp = auth_client.put('/api/storage/dropbox/config', json={
            'input_folder_ref': bad_url,
            'output_folder_ref': '/DealMemos',
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert data['error'] == 'Invalid folder reference'
        assert sync_wire.calls == []

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='dropbox'
        ).one()
        assert conn.input_folder_ref is None
        assert conn.output_folder_ref is None

    @pytest.mark.parametrize(
        'bad_url',
        [
            'https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/../evil',
            'https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/%2e%2e/evil',
            'https://docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/..',
        ],
    )
    def test_parse_spreadsheet_id_rejects_traversal_suffix(
        self, auth_client, db_session_fixture, test_user, sync_wire, bad_url
    ):
        with pytest.raises(ValueError):
            parse_spreadsheet_id(bad_url)

        _connect(auth_client, 'google_drive')
        sync_wire.calls.clear()

        resp = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': bad_url,
        })
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert sync_wire.calls == []

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.sheets_spreadsheet_id is None

    def test_manual_sync_validate_folder_upstream_500_returns_502_without_pii_leak(
        self, auth_client, db_session_fixture, test_user, sync_wire, caplog
    ):
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')
        sync_wire.calls.clear()
        sync_wire.validate_status_queue['google_drive'].append(
            (500, {'error': 'leak-user@example.com ya29.secret_tok'})
        )

        with caplog.at_level(logging.DEBUG):
            resp = auth_client.post('/api/storage/google_drive/sync')

        assert resp.status_code == 502
        data = resp.get_json()
        assert data['success'] is False
        assert data['error'] == 'Cloud storage provider unavailable'
        assert 'leak-user@example.com' not in caplog.text
        assert 'ya29.secret_tok' not in caplog.text
        assert test_user.email not in caplog.text
        assert len(sync_wire.calls_matching('/drive/v3/files/drv_in_1', 'GET')) == 1

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 100

    def test_mid_sync_upload_memo_double_401_refunds_cp_purges_connection_and_returns_401(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        mock_gemini = _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        _configure(auth_client, 'google_drive', 'drv_in_1', 'drv_out_1')
        _seed_file(sync_wire, 'google_drive', 'f_mid401', 'deck.pdf', _make_pdf(8))
        sync_wire.upload_status_queue['google_drive'].extend([
            (401, {'error': 'expired'}),
            (401, {'error': 'revoked'}),
        ])
        sync_wire.calls.clear()

        resp = auth_client.post('/api/storage/google_drive/sync')
        assert resp.status_code == 401
        data = resp.get_json()
        assert data['success'] is False
        assert data['needs_reauth'] is True

        mock_gemini.models.generate_content.assert_called_once()
        assert len(sync_wire.calls_matching('/upload/drive/v3/files', 'POST')) == 2

        db_session_fixture.expire_all()
        assert db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one().balance == 100
        txs = db_session_fixture.query(Transaction).filter_by(user_id=test_user.id).order_by(Transaction.id.asc()).all()
        assert [t.type for t in txs] == ['deduction', 'refund']
        assert [t.amount for t in txs] == [-3, 3]
        assert db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).count() == 0
        assert db_session_fixture.query(SyncedDeckRecord).filter_by(user_id=test_user.id).count() == 0
        audit = db_session_fixture.query(AuditLog).filter_by(
            user_id=test_user.id, provider='google_drive', event_type='storage.reauth_required'
        ).one()
        assert audit.detail == 'rejected_after_refresh'

    @pytest.mark.parametrize(
        'provider,bad_url,valid_other',
        [
            ('dropbox', 'https://[::1/home/Decks', '/DealMemos'),
            ('dropbox', 'https://[invalid/home/Decks', '/DealMemos'),
            ('google_drive', 'https://[::1/drive/folders/abc123', 'out_folder_123'),
        ],
    )
    def test_malformed_ipv6_urls_raise_provider_folder_error_and_return_400(
        self, auth_client, db_session_fixture, test_user, sync_wire, provider, bad_url, valid_other
    ):
        provider_impl = DropboxProvider() if provider == 'dropbox' else GoogleDriveProvider()
        with pytest.raises(ProviderFolderError):
            provider_impl.normalize_folder_ref(bad_url)

        _connect(auth_client, provider)
        sync_wire.calls.clear()

        resp = auth_client.put(f'/api/storage/{provider}/config', json={
            'input_folder_ref': bad_url,
            'output_folder_ref': valid_other,
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert data['error'] == 'Invalid folder reference'
        assert sync_wire.calls == []

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider=provider
        ).one()
        assert conn.input_folder_ref is None
        assert conn.output_folder_ref is None

    def test_parse_spreadsheet_id_malformed_ipv6_url_raises_value_error_and_returns_400(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        bad_sheet_url = 'https://[::1/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms'
        with pytest.raises(ValueError, match='Invalid Google Sheets URL'):
            parse_spreadsheet_id(bad_sheet_url)

        _connect(auth_client, 'google_drive')
        sync_wire.calls.clear()

        resp = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': bad_sheet_url,
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert sync_wire.calls == []

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.sheets_spreadsheet_id is None

    @pytest.mark.parametrize(
        'provider,bad_url,valid_other',
        [
            ('dropbox', 'https://www.dropbox.com:notaport/home/Decks', '/DealMemos'),
            ('dropbox', 'https://www.dropbox.com:99999/home/Decks', '/DealMemos'),
            ('dropbox', 'https://www.dropbox.com:-1/home/Decks', '/DealMemos'),
            ('dropbox', 'https://www.dropbox.com:0/home/Decks', '/DealMemos'),
            ('dropbox', 'https://www.dropbox.com:/home/Decks', '/DealMemos'),
            ('dropbox', 'https://www.dropbox.com:8443/home/Decks', '/DealMemos'),
            ('dropbox', 'https://evil.com@www.dropbox.com/home/Decks', '/DealMemos'),
            ('google_drive', 'https://drive.google.com:notaport/drive/folders/abc123', 'out_folder_123'),
            ('google_drive', 'https://drive.google.com:99999/drive/folders/abc123', 'out_folder_123'),
            ('google_drive', 'https://drive.google.com:-1/drive/folders/abc123', 'out_folder_123'),
            ('google_drive', 'https://drive.google.com:0/drive/folders/abc123', 'out_folder_123'),
            ('google_drive', 'https://drive.google.com:/drive/folders/abc123', 'out_folder_123'),
            ('google_drive', 'https://drive.google.com:8443/drive/folders/abc123', 'out_folder_123'),
            ('google_drive', 'https://evil.com@drive.google.com/drive/folders/abc123', 'out_folder_123'),
        ],
    )
    def test_weird_port_and_userinfo_folder_urls_rejected_400_with_zero_wire_calls(
        self, auth_client, db_session_fixture, test_user, sync_wire, provider, bad_url, valid_other
    ):
        provider_impl = DropboxProvider() if provider == 'dropbox' else GoogleDriveProvider()
        with pytest.raises(ProviderFolderError):
            provider_impl.normalize_folder_ref(bad_url)

        _connect(auth_client, provider)
        sync_wire.calls.clear()

        resp = auth_client.put(f'/api/storage/{provider}/config', json={
            'input_folder_ref': bad_url,
            'output_folder_ref': valid_other,
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert data['error'] == 'Invalid folder reference'
        assert sync_wire.calls == []

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider=provider
        ).one()
        assert conn.input_folder_ref is None
        assert conn.output_folder_ref is None

    @pytest.mark.parametrize(
        'bad_sheet_url',
        [
            'https://docs.google.com:notaport/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit',
            'https://docs.google.com:99999/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit',
            'https://docs.google.com:-1/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit',
            'https://docs.google.com:0/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit',
            'https://docs.google.com:/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit',
            'https://docs.google.com:8443/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit',
            'https://evil.com@docs.google.com/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit',
        ],
    )
    def test_weird_port_and_userinfo_sheets_urls_rejected_400_with_zero_wire_calls(
        self, auth_client, db_session_fixture, test_user, sync_wire, bad_sheet_url
    ):
        with pytest.raises(ValueError, match='Invalid Google Sheets URL host'):
            parse_spreadsheet_id(bad_sheet_url)

        _connect(auth_client, 'google_drive')
        sync_wire.calls.clear()

        resp = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': bad_sheet_url,
        })
        assert resp.status_code == 400
        data = resp.get_json()
        assert data['success'] is False
        assert data['error'] == 'Invalid Google Sheets spreadsheet ID'
        assert sync_wire.calls == []

        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.input_folder_ref is None
        assert conn.output_folder_ref is None
        assert conn.sheets_spreadsheet_id is None
        assert conn.auto_export_sheets is False

    def test_explicit_443_port_urls_normalize_cleanly(self):
        assert DropboxProvider().normalize_folder_ref('https://www.dropbox.com:443/home/Decks') == '/Decks'
        assert GoogleDriveProvider().normalize_folder_ref('https://drive.google.com:443/drive/folders/abc123') == 'abc123'
        assert parse_spreadsheet_id(
            'https://docs.google.com:443/spreadsheets/d/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms/edit'
        ) == '1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms'

    def test_inbox_archive_moves_completed_to_done_and_invalid_to_skipped_leaving_insufficient_cp_in_inbox(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        _install_gemini_mock()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        bal.balance = 3
        db_session_fixture.commit()

        _connect(auth_client, 'dropbox')
        _configure(auth_client, 'dropbox', '/PitchDecks', '/DealMemos')
        _seed_file(sync_wire, 'dropbox', 'f_ok', 'StartupA.pdf', _make_pdf(8))
        _seed_file(sync_wire, 'dropbox', 'f_bad', 'Corrupt.pdf', b'NOT_A_PDF')
        _seed_file(sync_wire, 'dropbox', 'f_nocp', 'WaitForTopup.pdf', _make_pdf(8))
        sync_wire.archived_files.clear()

        resp = auth_client.post('/api/storage/dropbox/sync')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] == 1
        assert data['skipped_invalid'] == 1
        assert data['skipped_insufficient_cp'] == 1

        # Completed -> /PitchDecks/Done/[Analyzed] StartupA.pdf
        # Skipped invalid -> /PitchDecks/Skipped/[Skipped - Invalid] Corrupt.pdf
        # Insufficient CP -> NOT moved, stays in inbox
        assert sync_wire.archived_files == [
            {
                'provider': 'dropbox',
                'from_path': '/PitchDecks/StartupA.pdf',
                'to_path': '/PitchDecks/Done/[Analyzed] StartupA.pdf',
            },
            {
                'provider': 'dropbox',
                'from_path': '/PitchDecks/Corrupt.pdf',
                'to_path': '/PitchDecks/Skipped/[Skipped - Invalid] Corrupt.pdf',
            },
        ]

    def test_google_drive_explicit_file_ids_sync_under_drive_file_auto_creates_output_and_renames(
        self, auth_client, db_session_fixture, test_user, sync_wire
    ):
        _install_gemini_mock()
        _connect(auth_client, 'google_drive')
        # Note: no input_folder_ref configured — pure drive.file Picker flow
        _seed_file(sync_wire, 'google_drive', 'picked_1', 'SeedDeck.pdf', _make_pdf(10))
        sync_wire.archived_files.clear()

        resp = auth_client.post('/api/storage/google_drive/sync', json={
            'file_ids': ['picked_1'],
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['processed'] == 1
        assert data['errors'] == 0
        assert data['new_balance'] == 97
        assert sync_wire.archived_files == [
            {
                'provider': 'google_drive',
                'file_id': 'picked_1',
                'new_name': '[Analyzed] SeedDeck.pdf',
            }
        ]
        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.output_folder_ref == 'auto_briefs_folder_1'
