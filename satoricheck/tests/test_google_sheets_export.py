"""
Milestone M4 tests for Google Sheets v4 export service, POST /api/export/pitchdeck/google-sheet,
formula-injection neutralization, OAuth refresh/InvalidToken/502 handling,
folder_sync_service auto_export_sheets (same-provider & cross-provider), and PII-free logging.

Follows tests/conftest.py and tests/test_suite_integrity.py strictly:
- Wire-only mocking (`requests.Session.request`, host-scoped `socket.getaddrinfo`, Gemini `svc.client`)
- Exact HTTP status, payload, DB state, and wire-call assertions
- RFC 2606 @example.com emails
"""
from __future__ import annotations

import json
import logging
import socket
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest
import requests

from backend.config import Config
from backend.models import AuditLog, CloudStorageConnection, SyncedDeckRecord, TokenBalance, Transaction
from backend.services import get_pitchdeck_service
from backend.services.cloud_tokens import decrypt_token
from backend.services.google_sheets_service import build_pitchdeck_sheet_row, sanitize_sheet_cell

PUBLIC_TEST_IP = '93.184.216.34'
SAMPLE_SHEET_ID = '1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms'
SAMPLE_SHEET_URL = f'https://docs.google.com/spreadsheets/d/{SAMPLE_SHEET_ID}/edit#gid=0'
MAX_EXPORT_JSON_BYTES = 256 * 1024

WIRE_HOSTS = frozenset({
    'oauth2.googleapis.com',
    'www.googleapis.com',
    'sheets.googleapis.com',
    'api.dropboxapi.com',
    'content.dropboxapi.com',
    'login.microsoftonline.com',
    'graph.microsoft.com',
})


def _make_pdf(pages: int = 8) -> bytes:
    header = b'%PDF-1.4\n'
    body = f'1 0 obj\n<< /Type /Pages /Count {pages} >>\nendobj\n'.encode('ascii')
    return header + body + b'%%EOF\n'


def _canonical_payload(**overrides) -> dict:
    payload = {
        'company_name': 'Acme AI',
        'summary': 'Automated compliance for SaaS.',
        'usp': '10x faster audits.',
        'industry': 'Enterprise Software',
        'sector': 'B2B SaaS',
        'funding_ask': '$3M Seed',
        'page_count': 8,
        'investor_decision': 'Watchlist',
        'analyzed_at': '2026-11-21',
        'startup_profile': {
            'geography': 'Europe',
            'geography_tags': ['DACH'],
            'sector': 'B2B SaaS',
            'stage': 'Seed',
        },
        'red_flags': ['Customer concentration 45%'],
        'verifiable_claims': [
            {
                'claim': 'Claim Med',
                'risk_level': 'medium',
                'suggested_question': 'Medium-risk Q2?',
            },
            {
                'claim': 'Claim High 1',
                'risk_level': 'high',
                'suggested_question': 'High-risk Q1?',
            },
            {
                'claim': 'Claim Low',
                'risk_level': 'low',
                'suggested_question': 'Low-risk Q4?',
            },
            {
                'claim': 'Claim High 2',
                'risk_level': 'high',
                'suggested_question': 'High-risk Q3?',
            },
        ],
    }
    payload.update(overrides)
    return payload


def _make_resp(
    status_code: int = 200,
    *,
    json_data: dict | None = None,
    content: bytes | None = None,
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

    resp.iter_content.return_value = iter([resp.content] if resp.content else [])
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f'HTTP {status_code}', response=resp)
    else:
        resp.raise_for_status.return_value = None

    resp.raw = MagicMock()
    resp.raw._connection = MagicMock()
    resp.raw._connection.sock = MagicMock()
    resp.raw._connection.sock.getpeername.return_value = (PUBLIC_TEST_IP, 443)
    return resp


class _M4WireRouter:
    def __init__(self):
        self.calls: list[dict] = []
        self.appended_sheet_rows: list[dict] = []
        self.uploaded_memos: list[dict] = []
        self.sheets_status_queue: list[tuple[int, dict]] = []
        self.refresh_queue: list[tuple[int, dict]] = []
        self.provider_files: dict[str, list[dict]] = {
            'google_drive': [],
            'dropbox': [],
            'onedrive': [],
        }
        self.file_bytes: dict[str, bytes] = {}
        self.token_overrides: dict[str, dict] = {}

    def handle(self, _session, method, url, **kwargs):
        method_u = str(method).upper()
        data = kwargs.get('data')
        json_body = kwargs.get('json')
        params = dict(kwargs.get('params') or {})
        headers = dict(kwargs.get('headers') or {})
        parsed = urlparse(url)
        host = parsed.hostname or ''
        path = parsed.path

        self.calls.append({
            'method': method_u,
            'host': host,
            'path': path,
            'url': url,
            'params': params,
            'headers': headers,
            'json': json_body,
            'data': data,
        })

        # --- OAuth Token Endpoints ---
        if host == 'oauth2.googleapis.com' and path == '/token' and method_u == 'POST':
            form = data or {}
            if form.get('grant_type') == 'refresh_token':
                if self.refresh_queue:
                    st, body = self.refresh_queue.pop(0)
                    return _make_resp(st, json_data=body)
                return _make_resp(200, json_data={'access_token': 'ya29.refreshed_sheets_tok', 'expires_in': 3600})
            tok = self.token_overrides.get('google_drive', {
                'access_token': 'ya29.g_access_1',
                'refresh_token': '1//g_refresh_1',
                'expires_in': 3600,
            })
            return _make_resp(200, json_data=tok)

        if host == 'api.dropboxapi.com' and path == '/oauth2/token' and method_u == 'POST':
            tok = self.token_overrides.get('dropbox', {
                'access_token': 'sl.dbx_access_1',
                'refresh_token': 'dbx_refresh_1',
                'expires_in': 3600,
            })
            return _make_resp(200, json_data=tok)

        if host == 'login.microsoftonline.com' and path.endswith('/token') and method_u == 'POST':
            tok = self.token_overrides.get('onedrive', {
                'access_token': 'od.access_1',
                'refresh_token': 'od_refresh_1',
                'expires_in': 3600,
            })
            return _make_resp(200, json_data=tok)

        # --- Google Sheets v4 API ---
        if host == 'sheets.googleapis.com' and ':append' in path and method_u == 'POST':
            if self.sheets_status_queue:
                st, body = self.sheets_status_queue.pop(0)
                return _make_resp(st, json_data=body)
            parts = path.split('/')
            spreadsheet_id = parts[3] if len(parts) > 3 else ''
            raw_range = unquote(parts[5].split(':append')[0]) if len(parts) > 5 else 'Sheet1!A1'
            sheet_prefix = raw_range.split('!')[0].strip("'") if '!' in raw_range else 'Sheet1'
            row_idx = len(self.appended_sheet_rows) + 2
            updated_range = f'{sheet_prefix}!A{row_idx}:L{row_idx}'
            self.appended_sheet_rows.append({
                'spreadsheet_id': spreadsheet_id,
                'raw_range': raw_range,
                'url': url,
                'params': params,
                'headers': headers,
                'json': json_body,
                'updated_range': updated_range,
            })
            return _make_resp(200, json_data={
                'spreadsheetId': spreadsheet_id,
                'updates': {
                    'spreadsheetId': spreadsheet_id,
                    'updatedRange': updated_range,
                    'updatedRows': 1,
                },
            })

        # --- Google Drive API (for sync) ---
        if host == 'www.googleapis.com' and path == '/drive/v3/files' and method_u == 'GET':
            return _make_resp(200, json_data={'files': self.provider_files['google_drive']})

        if host == 'www.googleapis.com' and path.startswith('/drive/v3/files/') and method_u == 'GET':
            fid = path.split('/drive/v3/files/', 1)[1]
            if params.get('alt') == 'media':
                return _make_resp(200, content=self.file_bytes.get(fid, b''))
            return _make_resp(200, json_data={
                'id': fid,
                'name': f'Folder-{fid}',
                'mimeType': 'application/vnd.google-apps.folder',
                'trashed': False,
            })

        if host == 'www.googleapis.com' and path == '/upload/drive/v3/files' and method_u == 'POST':
            doc_id = f'gdoc_{len(self.uploaded_memos) + 1}'
            doc_url = f'https://docs.google.com/document/d/{doc_id}/edit'
            self.uploaded_memos.append({'provider': 'google_drive', 'id': doc_id, 'url': doc_url})
            return _make_resp(200, json_data={'id': doc_id, 'webViewLink': doc_url})

        if host == 'www.googleapis.com' and path.startswith('/drive/v3/files/') and method_u == 'PATCH':
            fid = path.split('/drive/v3/files/', 1)[1]
            return _make_resp(200, json_data={'id': fid, 'name': (json_body or {}).get('name', '')})

        # --- Dropbox API ---
        if host == 'api.dropboxapi.com' and path == '/2/files/get_metadata' and method_u == 'POST':
            req_path = (json_body or {}).get('path', '/PitchDecks')
            return _make_resp(200, json_data={'.tag': 'folder', 'id': f'id:{req_path}', 'name': req_path.strip('/')})

        if host == 'api.dropboxapi.com' and path == '/2/files/list_folder' and method_u == 'POST':
            return _make_resp(200, json_data={'entries': self.provider_files['dropbox'], 'has_more': False})

        if host == 'api.dropboxapi.com' and path == '/2/files/move_v2' and method_u == 'POST':
            to_p = (json_body or {}).get('to_path', '')
            return _make_resp(200, json_data={'metadata': {'.tag': 'file', 'path_display': to_p}})

        if host == 'content.dropboxapi.com' and path == '/2/files/download' and method_u == 'POST':
            arg = json.loads(headers.get('Dropbox-API-Arg', '{}'))
            fid = arg.get('path', '')
            return _make_resp(200, content=self.file_bytes.get(fid, b''))

        if host == 'content.dropboxapi.com' and path == '/2/files/upload' and method_u == 'POST':
            arg = json.loads(headers.get('Dropbox-API-Arg', '{}'))
            dest = arg.get('path', '/DealMemos/brief.md')
            memo_url = f'https://www.dropbox.com/home{dest}'
            self.uploaded_memos.append({'provider': 'dropbox', 'path': dest, 'url': memo_url})
            return _make_resp(200, json_data={'id': f'dbx_{len(self.uploaded_memos)}', 'path_display': dest})

        # --- OneDrive API ---
        if host == 'graph.microsoft.com' and path.endswith('/children') and method_u == 'GET':
            return _make_resp(200, json_data={'value': self.provider_files['onedrive']})

        if host == 'graph.microsoft.com' and path.endswith('/children') and method_u == 'POST':
            sub_name = (json_body or {}).get('name', 'Done')
            return _make_resp(201, json_data={'id': f'od_sub_{sub_name.lower()}', 'name': sub_name, 'folder': {}})

        if host == 'graph.microsoft.com' and '/me/drive/items/' in path and method_u == 'PATCH':
            fid = path.split('/me/drive/items/', 1)[1]
            return _make_resp(200, json_data={'id': fid, 'name': (json_body or {}).get('name', '')})

        if host == 'graph.microsoft.com' and path.endswith('/content') and method_u == 'GET':
            parts = path.split('/')
            fid = parts[-2] if len(parts) >= 2 else ''
            return _make_resp(200, content=self.file_bytes.get(fid, b''))

        if host == 'graph.microsoft.com' and path.endswith('/content') and method_u == 'PUT':
            memo_url = f'https://onedrive.live.com/view.aspx?id=od_{len(self.uploaded_memos) + 1}'
            self.uploaded_memos.append({'provider': 'onedrive', 'url': memo_url})
            return _make_resp(201, json_data={'id': f'od_{len(self.uploaded_memos)}', 'webUrl': memo_url})

        if host == 'graph.microsoft.com' and '/me/drive/items/' in path and method_u == 'GET':
            fid = path.split('/me/drive/items/', 1)[1]
            return _make_resp(200, json_data={'id': fid, 'name': f'Folder-{fid}', 'folder': {'childCount': 0}})

        return _make_resp(404, json_data={'error': {'message': 'not_found'}})


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_ID', 'test-g-id', raising=False)
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_SECRET', 'test-g-sec', raising=False)
    monkeypatch.setattr(Config, 'DROPBOX_CLIENT_ID', 'test-dbx-id', raising=False)
    monkeypatch.setattr(Config, 'DROPBOX_CLIENT_SECRET', 'test-dbx-sec', raising=False)
    monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_ID', 'test-od-id', raising=False)
    monkeypatch.setattr(Config, 'ONEDRIVE_CLIENT_SECRET', 'test-od-sec', raising=False)

    router = _M4WireRouter()
    orig_getaddrinfo = socket.getaddrinfo

    def _dns(host, port, *args, **kwargs):
        host_str = host.decode() if isinstance(host, bytes) else str(host or '')
        if host_str.strip('[]').lower() in WIRE_HOSTS:
            port_i = int(port) if str(port).isdigit() else 443
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (PUBLIC_TEST_IP, port_i))]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    with patch('socket.getaddrinfo', side_effect=_dns), \
         patch('requests.Session.request', autospec=True, side_effect=router.handle):
        yield router


def _connect_provider(auth_client, provider: str):
    r_conn = auth_client.get(f'/api/storage/{provider}/connect')
    assert r_conn.status_code == 302
    state = parse_qs(urlparse(r_conn.headers['Location']).query)['state'][0]
    r_cb = auth_client.get(f'/api/storage/{provider}/callback?code=code_{provider}&state={state}')
    assert r_cb.status_code == 302


def _seed_file(router: _M4WireRouter, provider: str, file_id: str, name: str, content: bytes, etag: str = 'etag_1'):
    router.file_bytes[file_id] = content
    if provider == 'google_drive':
        router.provider_files['google_drive'].append({
            'id': file_id,
            'name': name,
            'mimeType': 'application/pdf',
            'size': str(len(content)),
            'modifiedTime': '2026-11-21T10:00:00Z',
            'md5Checksum': etag,
        })
    elif provider == 'dropbox':
        router.provider_files['dropbox'].append({
            '.tag': 'file',
            'id': file_id,
            'path_lower': file_id,
            'name': name,
            'size': len(content),
            'rev': etag,
        })
    elif provider == 'onedrive':
        router.provider_files['onedrive'].append({
            'id': file_id,
            'name': name,
            'size': len(content),
            'eTag': etag,
            'file': {'mimeType': 'application/pdf'},
        })


def _install_gemini_mock(payload: dict | None = None):
    svc = get_pitchdeck_service()
    mock_client = MagicMock()
    resp_obj = MagicMock()
    resp_obj.text = json.dumps(payload if payload is not None else _canonical_payload())
    mock_client.models.generate_content.return_value = resp_obj
    svc.client = mock_client
    return mock_client


class TestGoogleSheetsManualExport:
    """Tests for POST /api/export/pitchdeck/google-sheet and google_sheets_service."""

    def test_requires_authentication(self, client):
        resp = client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(),
        })
        assert resp.status_code == 401
        assert resp.get_json()['success'] is False

    def test_requires_google_drive_connection(self, auth_client, wire):
        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(),
        })
        assert resp.status_code == 401
        data = resp.get_json()
        assert data['success'] is False
        assert data['needs_google_auth'] is True
        assert len(wire.calls) == 0

    @pytest.mark.parametrize(
        'bad_body',
        [
            {'spreadsheet_id': SAMPLE_SHEET_ID, 'summary': 'No company'},
            {'spreadsheet_id': SAMPLE_SHEET_ID, 'company_name': '   ', 'summary': 'Blank company'},
            {'spreadsheet_id': SAMPLE_SHEET_ID, 'company_name': 'No summary'},
            {'spreadsheet_id': SAMPLE_SHEET_ID, 'company_name': 'Acme', 'summary': '   '},
            {'company_name': 'Acme', 'summary': 'Missing sheet id'},
            {},
        ],
    )
    def test_rejects_missing_required_fields(self, auth_client, wire, bad_body):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json=bad_body)
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert len(wire.calls) == 0

    def test_rejects_non_dict_json_and_invalid_json(self, auth_client, wire):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        r_list = auth_client.post('/api/export/pitchdeck/google-sheet', json=['not', 'a', 'dict'])
        assert r_list.status_code == 400
        assert r_list.get_json()['success'] is False

        r_bad = auth_client.post(
            '/api/export/pitchdeck/google-sheet',
            data=b'{invalid-json',
            content_type='application/json',
        )
        assert r_bad.status_code == 400
        assert r_bad.get_json()['success'] is False
        assert len(wire.calls) == 0

    @pytest.mark.parametrize(
        'bad_sheet_id',
        [
            'short',
            '../etc/passwd',
            'https://evil.example.com/spreadsheets/d/12345678901234567890',
            'http://docs.google.com/spreadsheets/d/12345678901234567890',
            'https://user:pass@docs.google.com/spreadsheets/d/12345678901234567890',
            'https://docs.google.com:8443/spreadsheets/d/12345678901234567890',
            'https://[::1/spreadsheets/d/12345678901234567890',
            '1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms?foo=bar',
        ],
    )
    def test_rejects_malformed_spreadsheet_id(self, auth_client, wire, bad_sheet_id):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': bad_sheet_id,
            **_canonical_payload(),
        })
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert len(wire.calls) == 0

    @pytest.mark.parametrize(
        'bad_sheet_name',
        [
            'Bad:Name',
            'Sheet[1]',
            'Sheet]1',
            'A*B',
            'What?',
            'X/Y',
            'X\\Y',
            'Tab!A1',
            "'LeadingQuote",
            "TrailingQuote'",
            'S' * 101,
        ],
    )
    def test_rejects_invalid_sheet_name(self, auth_client, wire, bad_sheet_name):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            'sheet_name': bad_sheet_name,
            **_canonical_payload(),
        })
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert len(wire.calls) == 0

    @pytest.mark.parametrize('payload_len,expected_status', [
        (MAX_EXPORT_JSON_BYTES, 200),
        (MAX_EXPORT_JSON_BYTES + 1, 400),
    ])
    def test_256kb_payload_boundary(self, auth_client, wire, payload_len, expected_status):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        base = json.dumps({'spreadsheet_id': SAMPLE_SHEET_ID, 'company_name': 'Acme', 'summary': ''})
        pad_len = payload_len - len(base)
        body_bytes = json.dumps({
            'spreadsheet_id': SAMPLE_SHEET_ID,
            'company_name': 'Acme',
            'summary': 'S' * pad_len,
        }).encode('utf-8')
        assert len(body_bytes) == payload_len

        resp = auth_client.post(
            '/api/export/pitchdeck/google-sheet',
            data=body_bytes,
            content_type='application/json',
        )
        assert resp.status_code == expected_status
        if expected_status == 400:
            assert len(wire.calls) == 0
        else:
            assert len(wire.appended_sheet_rows) == 1

    def test_manual_append_12_column_row_and_raw_mode(self, auth_client, wire):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            'memo_url': 'https://docs.google.com/document/d/doc_1/edit',
            **_canonical_payload(),
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {
            'success': True,
            'spreadsheet_id': SAMPLE_SHEET_ID,
            'updated_range': 'Sheet1!A2:L2',
            'sheet_url': f'https://docs.google.com/spreadsheets/d/{SAMPLE_SHEET_ID}',
        }

        assert len(wire.appended_sheet_rows) == 1
        call = wire.appended_sheet_rows[0]
        assert call['spreadsheet_id'] == SAMPLE_SHEET_ID
        assert call['raw_range'] == 'Sheet1!A1'
        assert call['params'] == {'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'}

        row = call['json']['values'][0]
        assert len(row) == 12
        assert row[0] == '2026-11-21'
        assert row[1] == 'Acme AI'
        assert row[2] == 'Enterprise Software'
        assert row[3] == 'B2B SaaS'
        assert row[4] == 'Seed'
        assert row[5] == '$3M Seed'
        assert row[6] == 8
        assert isinstance(row[6], int)
        assert row[7] == 'Watchlist'
        assert row[8] == 'https://docs.google.com/document/d/doc_1/edit'
        assert row[9] == 'Automated compliance for SaaS.'
        assert row[10] == 'Customer concentration 45%'
        # Top 3 prioritized (high before medium, low excluded), NO [HIGH] prefix
        assert row[11] == 'High-risk Q1?; High-risk Q3?; Medium-risk Q2?'

    def test_parses_url_quotes_sheet_name_and_unescapes_html_entities(self, auth_client, wire):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_URL,
            'sheet_name': "Deal's Flow",
            **_canonical_payload(
                company_name='R&amp;D &#x27;26',
                summary='AI &amp; ML tooling &lt;2026&gt;',
            ),
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['spreadsheet_id'] == SAMPLE_SHEET_ID
        assert data['updated_range'] == "Deal''s Flow!A2:L2"

        call = wire.appended_sheet_rows[0]
        assert call['raw_range'] == "'Deal''s Flow'!A1"
        row = call['json']['values'][0]
        assert row[1] == "R&D '26"
        assert row[9] == 'AI & ML tooling <2026>'

    def test_stage_falls_back_to_thesis_alignment(self):
        row = build_pitchdeck_sheet_row({
            'company_name': 'ThesisCo',
            'summary': 'Summary text',
            'thesis_alignment': {'stage': 'Series A', 'sector': 'FinTech'},
            'page_count': '12',
        })
        assert row[4] == 'Series A'
        assert row[6] == 12

    @pytest.mark.parametrize(
        'formula_vector',
        [
            '=SUM(A1:A2)',
            '+1+1',
            "-cmd|' /C calc'!A0",
            '@SUM(1,1)',
            '\t=1+1',
            '\r=1+1',
            '\n=1+1',
            ' =HYPERLINK("https://evil.example.com","click")',
            '=IMPORTXML("https://evil.example.com","//a")',
        ],
    )
    def test_formula_injection_neutralized_across_fields(self, auth_client, wire, formula_vector):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(
                company_name=formula_vector,
                summary=formula_vector,
                funding_ask=formula_vector,
                red_flags=[formula_vector],
                verifiable_claims=[{
                    'claim': 'Claim 1',
                    'risk_level': 'high',
                    'suggested_question': formula_vector,
                }],
            ),
        })
        assert resp.status_code == 200
        call = wire.appended_sheet_rows[0]
        assert call['params']['valueInputOption'] == 'RAW'
        row = call['json']['values'][0]
        assert row[1] == f"'{formula_vector}"
        assert row[5] == f"'{formula_vector}"
        assert row[9] == f"'{formula_vector}"
        assert row[10] == f"'{formula_vector.strip()}"
        assert row[11] == f"'{formula_vector.strip()}"

    def test_entity_encoded_formula_vector_is_neutralized(self):
        assert sanitize_sheet_cell('&#x3d;1+1') == "'=1+1"
        assert sanitize_sheet_cell('&#43;SUM(A1)') == "'+SUM(A1)"


class TestGoogleSheetsOAuthAndErrors:
    """Tests for 401 token refresh, invalid_grant/InvalidToken purge, and 502 preservation."""

    def test_401_triggers_single_refresh_and_persists_new_access_token(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()
        wire.sheets_status_queue.append((401, {'error': {'code': 401}}))

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(),
        })
        assert resp.status_code == 200
        assert resp.get_json()['updated_range'] == 'Sheet1!A2:L2'

        db_session_fixture.expire_all()
        conn = (
            db_session_fixture.query(CloudStorageConnection)
            .filter_by(user_id=test_user.id, provider='google_drive')
            .one()
        )
        assert decrypt_token(conn.encrypted_access_token) == 'ya29.refreshed_sheets_tok'

        refresh_calls = [c for c in wire.calls if c['host'] == 'oauth2.googleapis.com' and c['path'] == '/token']
        assert len(refresh_calls) == 1

    def test_invalid_grant_on_refresh_purges_connection_and_writes_audit_log(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()
        wire.sheets_status_queue.append((401, {'error': {'code': 401}}))
        wire.refresh_queue.append((400, {'error': 'invalid_grant'}))

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(),
        })
        assert resp.status_code == 401
        data = resp.get_json()
        assert data['success'] is False
        assert data['needs_google_auth'] is True

        db_session_fixture.expire_all()
        conn = (
            db_session_fixture.query(CloudStorageConnection)
            .filter_by(user_id=test_user.id, provider='google_drive')
            .first()
        )
        assert conn is None
        audit = (
            db_session_fixture.query(AuditLog)
            .filter_by(user_id=test_user.id, provider='google_drive', event_type='storage.reauth_required')
            .first()
        )
        assert audit is not None

    def test_invalid_token_key_rotation_purges_connection_with_zero_wire_calls(
        self, auth_client, db_session_fixture, test_user, wire, monkeypatch
    ):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()
        monkeypatch.setattr(Config, 'GOOGLE_TOKEN_ENCRYPTION_KEY', 'rotated-secret-key-xyz', raising=False)

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(),
        })
        assert resp.status_code == 401
        data = resp.get_json()
        assert data['success'] is False
        assert data['needs_google_auth'] is True
        assert len(wire.calls) == 0

        db_session_fixture.expire_all()
        conn = (
            db_session_fixture.query(CloudStorageConnection)
            .filter_by(user_id=test_user.id, provider='google_drive')
            .first()
        )
        assert conn is None
        audit = (
            db_session_fixture.query(AuditLog)
            .filter_by(user_id=test_user.id, provider='google_drive', event_type='storage.invalid_token')
            .first()
        )
        assert audit is not None

    def test_transient_refresh_503_returns_502_and_preserves_connection(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()
        wire.sheets_status_queue.append((401, {'error': {'code': 401}}))
        wire.refresh_queue.append((503, {'error': 'temporarily_unavailable'}))

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(),
        })
        assert resp.status_code == 502
        data = resp.get_json()
        assert data['success'] is False
        assert 'needs_google_auth' not in data

        db_session_fixture.expire_all()
        conn = (
            db_session_fixture.query(CloudStorageConnection)
            .filter_by(user_id=test_user.id, provider='google_drive')
            .first()
        )
        assert conn is not None

    @pytest.mark.parametrize('upstream_status', [404, 500])
    def test_upstream_sheets_error_returns_502_without_leaking_body(
        self, auth_client, wire, upstream_status
    ):
        _connect_provider(auth_client, 'google_drive')
        wire.calls.clear()
        wire.sheets_status_queue.append((upstream_status, {'error': {'message': 'SENSITIVE_UPSTREAM_TRACE'}}))

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(),
        })
        assert resp.status_code == 502
        body_text = resp.get_data(as_text=True)
        assert 'SENSITIVE_UPSTREAM_TRACE' not in body_text
        assert resp.get_json()['success'] is False


class TestFolderSyncAutoExportSheets:
    """Integration tests for M4 auto_export_sheets inside folder_sync_service."""

    def test_same_provider_google_drive_auto_export_and_dedup(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _install_gemini_mock()
        _connect_provider(auth_client, 'google_drive')
        r_cfg = auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
            'memo_preset': 'extended',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': SAMPLE_SHEET_URL,
            'sheets_sheet_name': 'Pipeline',
        })
        assert r_cfg.status_code == 200
        _seed_file(wire, 'google_drive', 'drv_file_1', 'deck.pdf', _make_pdf(8), etag='v1')

        r_sync1 = auth_client.post('/api/storage/google_drive/sync')
        assert r_sync1.status_code == 200
        d1 = r_sync1.get_json()
        assert d1['processed'] == 1
        assert d1['new_balance'] == 97  # 100 - (2 analysis + 1 sync fee)
        assert d1['records'][0]['exported_sheet_range'] == 'Pipeline!A2:L2'

        assert len(wire.appended_sheet_rows) == 1
        sheet_call = wire.appended_sheet_rows[0]
        assert sheet_call['params']['valueInputOption'] == 'RAW'
        row = sheet_call['json']['values'][0]
        assert row[8] == d1['records'][0]['exported_doc_url']
        assert row[1] == 'Acme AI'

        db_session_fixture.expire_all()
        rec = (
            db_session_fixture.query(SyncedDeckRecord)
            .filter_by(user_id=test_user.id, external_file_id='drv_file_1')
            .one()
        )
        assert rec.status == 'completed'
        assert rec.exported_sheet_range == 'Pipeline!A2:L2'
        assert rec.cp_charged == 3

        tx = (
            db_session_fixture.query(Transaction)
            .filter_by(user_id=test_user.id, type='deduction')
            .one()
        )
        assert tx.amount == -3

        # Second sync deduplicates and appends 0 new rows
        r_sync2 = auth_client.post('/api/storage/google_drive/sync')
        assert r_sync2.status_code == 200
        d2 = r_sync2.get_json()
        assert d2['processed'] == 0
        assert d2['skipped_duplicate'] == 1
        assert len(wire.appended_sheet_rows) == 1

    @pytest.mark.parametrize('provider,in_ref,out_ref,file_id', [
        ('dropbox', '/PitchDecks', '/DealMemos', '/PitchDecks/deck.pdf'),
        ('onedrive', 'od_in_1', 'od_out_1', 'od_file_1'),
    ])
    def test_cross_provider_auto_export_uses_google_drive_connection(
        self, auth_client, db_session_fixture, test_user, wire, provider, in_ref, out_ref, file_id
    ):
        _install_gemini_mock()
        _connect_provider(auth_client, 'google_drive')
        _connect_provider(auth_client, provider)

        r_cfg = auth_client.put(f'/api/storage/{provider}/config', json={
            'input_folder_ref': in_ref,
            'output_folder_ref': out_ref,
            'memo_preset': 'brief',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': SAMPLE_SHEET_ID,
            'sheets_sheet_name': 'CrossProvider',
        })
        assert r_cfg.status_code == 200
        _seed_file(wire, provider, file_id, 'deck.pdf', _make_pdf(8), etag='etag_cp_1')

        r_sync = auth_client.post(f'/api/storage/{provider}/sync')
        assert r_sync.status_code == 200
        data = r_sync.get_json()
        assert data['processed'] == 1
        assert data['new_balance'] == 97
        assert data['records'][0]['exported_sheet_range'] == 'CrossProvider!A2:L2'

        assert len(wire.appended_sheet_rows) == 1
        sheet_call = wire.appended_sheet_rows[0]
        assert sheet_call['headers']['Authorization'] == 'Bearer ya29.g_access_1'
        assert sheet_call['json']['values'][0][8] == data['records'][0]['exported_doc_url']

        db_session_fixture.expire_all()
        rec = (
            db_session_fixture.query(SyncedDeckRecord)
            .filter_by(user_id=test_user.id, external_file_id=file_id)
            .one()
        )
        assert rec.exported_sheet_range == 'CrossProvider!A2:L2'

    def test_auto_export_failure_is_non_fatal_to_memo_and_cp_billing(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _install_gemini_mock()
        _connect_provider(auth_client, 'google_drive')
        _connect_provider(auth_client, 'dropbox')
        auth_client.put('/api/storage/dropbox/config', json={
            'input_folder_ref': '/PitchDecks',
            'output_folder_ref': '/DealMemos',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': SAMPLE_SHEET_ID,
        })
        _seed_file(wire, 'dropbox', '/PitchDecks/deck.pdf', 'deck.pdf', _make_pdf(8))
        wire.sheets_status_queue.append((404, {'error': {'code': 404}}))

        r_sync = auth_client.post('/api/storage/dropbox/sync')
        assert r_sync.status_code == 200
        data = r_sync.get_json()
        assert data['processed'] == 1
        assert data['errors'] == 0
        assert data['new_balance'] == 97
        assert data['records'][0]['status'] == 'completed'
        assert data['records'][0]['exported_sheet_range'] is None
        assert len(wire.uploaded_memos) == 1

        db_session_fixture.expire_all()
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.balance == 97
        rec = (
            db_session_fixture.query(SyncedDeckRecord)
            .filter_by(user_id=test_user.id, external_file_id='/PitchDecks/deck.pdf')
            .one()
        )
        assert rec.status == 'completed'
        assert rec.exported_sheet_range is None

    def test_auto_export_missing_google_connection_is_non_fatal(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _install_gemini_mock()
        _connect_provider(auth_client, 'dropbox')
        auth_client.put('/api/storage/dropbox/config', json={
            'input_folder_ref': '/PitchDecks',
            'output_folder_ref': '/DealMemos',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': SAMPLE_SHEET_ID,
        })
        _seed_file(wire, 'dropbox', '/PitchDecks/deck.pdf', 'deck.pdf', _make_pdf(8))

        r_sync = auth_client.post('/api/storage/dropbox/sync')
        assert r_sync.status_code == 200
        data = r_sync.get_json()
        assert data['processed'] == 1
        assert data['new_balance'] == 97
        assert data['records'][0]['status'] == 'completed'
        assert data['records'][0]['exported_sheet_range'] is None
        assert len(wire.appended_sheet_rows) == 0

    def test_same_provider_auto_export_401_purge_does_not_crash_or_refund(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _install_gemini_mock()
        _connect_provider(auth_client, 'google_drive')
        auth_client.put('/api/storage/google_drive/config', json={
            'input_folder_ref': 'drv_in_1',
            'output_folder_ref': 'drv_out_1',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': SAMPLE_SHEET_ID,
        })
        _seed_file(wire, 'google_drive', 'drv_f_401', 'deck.pdf', _make_pdf(8))
        wire.sheets_status_queue.append((401, {'error': {'code': 401}}))
        wire.refresh_queue.append((400, {'error': 'invalid_grant'}))

        r_sync = auth_client.post('/api/storage/google_drive/sync')
        assert r_sync.status_code == 200
        data = r_sync.get_json()
        assert data['processed'] == 1
        assert data['errors'] == 0
        assert data['new_balance'] == 97
        assert data['records'][0]['status'] == 'completed'
        assert data['records'][0]['exported_sheet_range'] is None
        assert len(wire.uploaded_memos) == 1

        db_session_fixture.expire_all()
        conn = (
            db_session_fixture.query(CloudStorageConnection)
            .filter_by(user_id=test_user.id, provider='google_drive')
            .first()
        )
        assert conn is None
        bal = db_session_fixture.query(TokenBalance).filter_by(user_id=test_user.id).one()
        assert bal.balance == 97
        audit = (
            db_session_fixture.query(AuditLog)
            .filter_by(user_id=test_user.id, provider='google_drive', event_type='storage.reauth_required')
            .first()
        )
        assert audit is not None

    def test_cross_provider_auto_export_401_purge_preserves_dropbox_record(
        self, auth_client, db_session_fixture, test_user, wire
    ):
        _install_gemini_mock()
        _connect_provider(auth_client, 'google_drive')
        _connect_provider(auth_client, 'dropbox')
        auth_client.put('/api/storage/dropbox/config', json={
            'input_folder_ref': '/PitchDecks',
            'output_folder_ref': '/DealMemos',
            'auto_export_sheets': True,
            'sheets_spreadsheet_id': SAMPLE_SHEET_ID,
        })
        _seed_file(wire, 'dropbox', '/PitchDecks/deck.pdf', 'deck.pdf', _make_pdf(8))
        wire.sheets_status_queue.append((401, {'error': {'code': 401}}))
        wire.refresh_queue.append((400, {'error': 'invalid_grant'}))

        r_sync = auth_client.post('/api/storage/dropbox/sync')
        assert r_sync.status_code == 200
        data = r_sync.get_json()
        assert data['processed'] == 1
        assert data['new_balance'] == 97
        assert data['records'][0]['status'] == 'completed'
        assert data['records'][0]['exported_sheet_range'] is None

        db_session_fixture.expire_all()
        g_conn = (
            db_session_fixture.query(CloudStorageConnection)
            .filter_by(user_id=test_user.id, provider='google_drive')
            .first()
        )
        dbx_conn = (
            db_session_fixture.query(CloudStorageConnection)
            .filter_by(user_id=test_user.id, provider='dropbox')
            .first()
        )
        assert g_conn is None
        assert dbx_conn is not None
        rec = (
            db_session_fixture.query(SyncedDeckRecord)
            .filter_by(user_id=test_user.id, connection_id=dbx_conn.id)
            .one()
        )
        assert rec.status == 'completed'
        assert rec.exported_sheet_range is None

    def test_google_sheets_logs_are_strictly_pii_free(
        self, auth_client, test_user, wire, caplog
    ):
        caplog.set_level(logging.DEBUG)
        wire.token_overrides['google_drive'] = {
            'access_token': 'ya29.SECRET_SHEETS_ACCESS_TOKEN',
            'refresh_token': '1//SECRET_SHEETS_REFRESH_TOKEN',
            'expires_in': 3600,
        }
        _connect_provider(auth_client, 'google_drive')

        resp = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(summary='CONFIDENTIAL_SNIPPET_XYZ_999'),
        })
        assert resp.status_code == 200

        wire.sheets_status_queue.append((500, {'error': {'message': 'UPSTREAM_SECRET_500'}}))
        resp_err = auth_client.post('/api/export/pitchdeck/google-sheet', json={
            'spreadsheet_id': SAMPLE_SHEET_ID,
            **_canonical_payload(summary='CONFIDENTIAL_SNIPPET_XYZ_999'),
        })
        assert resp_err.status_code == 502

        logs = caplog.text
        assert test_user.email not in logs
        assert 'ya29.SECRET_SHEETS_ACCESS_TOKEN' not in logs
        assert '1//SECRET_SHEETS_REFRESH_TOKEN' not in logs
        assert 'CONFIDENTIAL_SNIPPET_XYZ_999' not in logs
        assert 'UPSTREAM_SECRET_500' not in logs
