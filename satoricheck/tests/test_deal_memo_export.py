"""
Milestone M2 tests for Deal Memo Renderer (Brief & Extended HTML and Markdown)
and POST /api/export/pitchdeck/google-doc.

Follows tests/conftest.py and tests/test_suite_integrity.py strictly:
- Wire-only mocking (`requests.Session.request`, host-scoped `socket.getaddrinfo`)
- Exact HTTP status, payload, DB state, and wire-call assertions
- RFC 2606 @example.com emails
"""
from __future__ import annotations

import json
import logging
import socket
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from backend.config import Config
from backend.models import AuditLog, CloudStorageConnection
from backend.services.deal_memo_renderer import (
    build_deal_memo_filename,
    build_deal_memo_title,
    render_deal_memo_html,
    render_deal_memo_markdown,
)

PUBLIC_TEST_IP = '93.184.216.34'
DRIVE_HOSTS = frozenset({'oauth2.googleapis.com', 'www.googleapis.com'})
MAX_EXPORT_JSON_BYTES = 256 * 1024


def _sample_payload(**overrides) -> dict:
    data = {
        'company_name': 'Acme AI',
        'summary': 'Automated compliance for SaaS.',
        'usp': '10x faster audits.',
        'industry': 'Enterprise Software',
        'sector': 'B2B SaaS',
        'funding_ask': '$3M Seed',
        'market_size': '$12B TAM',
        'competition': ['LegacyCo', 'AuditBot'],
        'team_highlights': 'Ex-Stripe & ETH Zurich founders',
        'startup_profile': {
            'geography': 'DACH',
            'geography_tags': ['Switzerland'],
            'sector': 'B2B SaaS',
            'stage': 'Seed',
        },
        'vc_metrics': {
            'monthly_revenue_arr': {'value': '$120K ARR', 'assessment': 'Good', 'detail': 'Growing 15% MoM'},
            'burn_multiple': {'value': '1.2x', 'assessment': 'Elite', 'detail': 'Capital efficient'},
            'nrr_percent': {'value': '125%', 'assessment': 'Elite', 'detail': 'Strong expansion'},
            'cac_payback_months': {'value': '8 months', 'assessment': 'Good', 'detail': 'Enterprise ACV'},
            'ltv_cac_ratio': {'value': '5.1x', 'assessment': 'Elite', 'detail': 'Healthy unit economics'},
            'runway_months': {'value': '22 months', 'assessment': 'Good', 'detail': 'Post-seed buffer'},
        },
        'red_flags': ['Customer concentration 45%'],
        'verifiable_claims': [
            {'claim': 'C1', 'risk_level': 'high', 'suggested_question': 'High-risk Q1?'},
            {'claim': 'C2', 'risk_level': 'medium', 'suggested_question': 'Medium-risk Q2?'},
            {'claim': 'C3', 'risk_level': 'high', 'suggested_question': 'High-risk Q3?'},
            {'claim': 'C4', 'risk_level': 'low', 'suggested_question': 'Low-risk Q4?'},
        ],
        'page_count': 8,
        'copy_style': 'distinctive',
    }
    data.update(overrides)
    return data


def _make_resp(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    resp.headers = {'Content-Type': 'application/json'} if json_data is not None else {}
    if json_data is not None:
        raw = json.dumps(json_data).encode('utf-8')
        resp.content = raw
        resp.text = raw.decode('utf-8')
        resp.json.return_value = json_data
    else:
        resp.content = b''
        resp.text = ''
        resp.json.side_effect = ValueError('No JSON')
    resp.raw = MagicMock()
    resp.raw._connection = MagicMock()
    resp.raw._connection.sock = MagicMock()
    resp.raw._connection.sock.getpeername.return_value = (PUBLIC_TEST_IP, 443)
    return resp


class _DriveWireStub:
    def __init__(self):
        self.calls: list[dict] = []
        self.trashed_folders: set[str] = set()
        self.missing_folders: set[str] = set()
        self.created_folders: list[dict] = []
        self.uploaded_docs: list[dict] = []
        self.upload_status_queue: list[tuple[int, dict]] = []
        self.refresh_queue: list[tuple[int, dict]] = []

    def handle(self, _session, method, url, **kwargs):
        method_u = str(method).upper()
        data = kwargs.get('data')
        json_body = kwargs.get('json')
        params = dict(kwargs.get('params') or {})
        headers = dict(kwargs.get('headers') or {})
        self.calls.append({
            'method': method_u,
            'url': url,
            'params': params,
            'headers': headers,
            'data': data,
            'json': json_body,
            'timeout': kwargs.get('timeout'),
        })

        parsed = urlparse(url)
        host = parsed.hostname or ''
        path = parsed.path

        if host == 'oauth2.googleapis.com' and path == '/token' and method_u == 'POST':
            form = data or {}
            if form.get('grant_type') == 'refresh_token':
                if self.refresh_queue:
                    st, body = self.refresh_queue.pop(0)
                    return _make_resp(st, body)
                return _make_resp(200, {'access_token': 'ya29.refreshed_doc_tok', 'expires_in': 3600})
            return _make_resp(200, {
                'access_token': 'ya29.doc_access_1',
                'refresh_token': '1//doc_refresh_1',
                'expires_in': 3600,
            })

        if host == 'www.googleapis.com' and path == '/drive/v3/files' and method_u == 'POST':
            fld_id = f'fld_created_{len(self.created_folders) + 1}'
            self.created_folders.append({'id': fld_id, 'json': json_body})
            return _make_resp(200, {'id': fld_id, 'name': (json_body or {}).get('name')})

        if host == 'www.googleapis.com' and path.startswith('/drive/v3/files/') and method_u == 'GET':
            fid = path.split('/drive/v3/files/', 1)[1]
            if fid in self.missing_folders:
                return _make_resp(404, {'error': {'code': 404}})
            return _make_resp(200, {'id': fid, 'trashed': fid in self.trashed_folders})

        if host == 'www.googleapis.com' and path == '/upload/drive/v3/files' and method_u == 'POST':
            if self.upload_status_queue:
                st, body = self.upload_status_queue.pop(0)
                return _make_resp(st, body)
            doc_id = f'gdoc_{len(self.uploaded_docs) + 1}'
            doc_url = f'https://docs.google.com/document/d/{doc_id}/edit'
            body_text = data.decode('utf-8', errors='replace') if isinstance(data, bytes) else str(data or '')
            self.uploaded_docs.append({'id': doc_id, 'url': doc_url, 'body': body_text, 'headers': headers})
            return _make_resp(200, {'id': doc_id, 'webViewLink': doc_url})

        return _make_resp(404, {'error': 'unhandled'})


@pytest.fixture
def drive_wire(monkeypatch):
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_ID', 'test-g-id', raising=False)
    monkeypatch.setattr(Config, 'GOOGLE_CLIENT_SECRET', 'test-g-sec', raising=False)
    stub = _DriveWireStub()
    orig_getaddrinfo = socket.getaddrinfo

    def _dns(host, port, *args, **kwargs):
        host_str = host.decode() if isinstance(host, bytes) else str(host or '')
        if host_str.strip('[]').lower() in DRIVE_HOSTS:
            port_i = int(port) if str(port).isdigit() else 443
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (PUBLIC_TEST_IP, port_i))]
        return orig_getaddrinfo(host, port, *args, **kwargs)

    with patch('socket.getaddrinfo', side_effect=_dns), \
         patch('requests.Session.request', autospec=True, side_effect=stub.handle):
        yield stub


def _connect_google(auth_client):
    conn_resp = auth_client.get('/api/storage/google_drive/connect')
    assert conn_resp.status_code == 302
    state = parse_qs(urlparse(conn_resp.headers['Location']).query)['state'][0]
    cb_resp = auth_client.get(f'/api/storage/google_drive/callback?code=g_ok&state={state}')
    assert cb_resp.status_code == 302


class TestDealMemoRenderer:
    """Pure unit tests for Brief vs Extended HTML & Markdown rendering, bounds, and escaping."""

    def test_brief_html_includes_core_and_excludes_extended_sections(self):
        html_out = render_deal_memo_html(_sample_payload(), mode='brief')
        assert 'Acme AI' in html_out
        assert 'Automated compliance for SaaS.' in html_out
        assert 'Customer concentration 45%' in html_out
        assert 'VC Scorecard' not in html_out
        assert 'Thesis Alignment' not in html_out
        assert 'Ask the Founder' not in html_out
        assert 'Burn Multiple' not in html_out
        assert '10x faster audits.' not in html_out
        assert 'Ex-Stripe' not in html_out

    def test_extended_html_renders_all_five_sections_and_top3_questions(self):
        html_out = render_deal_memo_html(
            _sample_payload(investor_decision='Advance', investor_notes='Strong moat.'),
            mode='extended',
        )
        assert '1. Summary &amp; Metadata' in html_out
        assert '2. Business Case Info' in html_out
        assert '3. Red Flags &amp; Follow-Up Questions' in html_out
        assert '4. VC Scorecard' in html_out
        assert '5. Thesis Alignment' in html_out
        assert '$120K ARR' in html_out
        assert '1.2x' in html_out
        assert '125%' in html_out
        assert '8 months' in html_out
        assert '5.1x' in html_out
        assert '22 months' in html_out
        assert 'Strong moat.' in html_out
        assert 'High-risk Q1?' in html_out
        assert 'High-risk Q3?' in html_out
        assert 'Medium-risk Q2?' in html_out
        assert 'Low-risk Q4?' not in html_out

    def test_idempotent_html_escaping_prevents_double_encoding_and_xss(self):
        payload = _sample_payload(
            company_name="R&amp;D &lt;script&gt;alert(1)&lt;/script&gt; &#x27;26",
            summary='<script>xss()</script> &amp; growth',
            investor_notes='<img src=x onerror=alert(2)> &amp; review',
        )
        html_out = render_deal_memo_html(payload, mode='extended')
        assert 'R&amp;D' in html_out
        assert '&#x27;26' in html_out
        assert '&lt;script&gt;xss()&lt;/script&gt;' in html_out
        assert '&lt;img src=x onerror=alert(2)&gt;' in html_out
        assert '&amp;amp;' not in html_out
        assert '&amp;#x27;' not in html_out
        assert '<script>' not in html_out
        assert '<script>' not in build_deal_memo_title(payload['company_name'], '2026-11-21')

    def test_markdown_outputs_clean_unescaped_utf8_and_safe_filename(self):
        payload = _sample_payload(
            company_name="R&amp;D &#x27;26",
            summary='Growth &amp; margin expansion.',
        )
        md_brief = render_deal_memo_markdown(payload, mode='brief')
        assert "R&D '26" in md_brief
        assert 'Growth & margin expansion.' in md_brief
        assert '&amp;' not in md_brief
        assert '&#x27;' not in md_brief
        assert 'VC Scorecard' not in md_brief

        md_ext = render_deal_memo_markdown(payload, mode='extended')
        assert '## 4. VC Scorecard' in md_ext
        assert 'High-risk Q1?' in md_ext
        assert 'Low-risk Q4?' not in md_ext

        fname = build_deal_memo_filename('Acme/AI: "Seed"', '2026-11-21')
        assert fname.startswith('Authenix Brief')
        assert fname.endswith('.md')
        assert '/' not in fname
        assert 'Authenix Brief —' in build_deal_memo_title('Acme AI', '2026-11-21')

    def test_truncation_bounds_enforced(self):
        payload = _sample_payload(
            summary='X' * 6200,
            red_flags=[f'RF_{i:02d}' for i in range(25)],
            competition=[f'CP_{i:02d}' for i in range(25)],
            verifiable_claims=[
                {'claim': f'C{i}', 'risk_level': 'high' if i < 4 else 'medium', 'suggested_question': f'Q_{i:02d}?'}
                for i in range(55)
            ],
        )
        html_out = render_deal_memo_html(payload, mode='extended')
        assert ('X' * 5000) in html_out
        assert ('X' * 5001) not in html_out
        assert 'RF_19' in html_out
        assert 'RF_20' not in html_out
        assert 'CP_19' in html_out
        assert 'CP_20' not in html_out
        assert 'Q_02?' in html_out
        assert 'Q_03?' not in html_out

    def test_single_unescape_consistency_on_doubly_encoded_entities(self):
        doubly = '&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt; | A &amp;amp; B'
        expected_html = '&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt; | A &amp;amp; B'
        expected_md = '&lt;script&gt;alert(1)&lt;/script&gt; | A &amp; B'
        payload = _sample_payload(
            summary=doubly,
            red_flags=[doubly],
            competition=[doubly],
            verifiable_claims=[
                {
                    'claim': doubly,
                    'risk_level': 'high',
                    'verification_finding': doubly,
                    'suggested_question': doubly,
                }
            ],
            thesis_alignment={
                'geography': doubly,
                'geography_tags': [doubly],
                'sector': doubly,
                'stage': doubly,
                'status': doubly,
            },
        )

        html_out = render_deal_memo_html(payload, mode='extended')
        assert f'<p>{expected_html}</p>' in html_out
        assert f'<li>{expected_html}</li>' in html_out
        assert f'<li><strong>Competition:</strong> {expected_html}</li>' in html_out
        assert f'<li><strong>[HIGH]</strong> {expected_html}</li>' in html_out
        assert f'<li><strong>Geography:</strong> {expected_html} ({expected_html})</li>' in html_out
        assert f'<li><strong>Sector:</strong> {expected_html}</li>' in html_out
        assert f'<li><strong>Stage:</strong> {expected_html}</li>' in html_out
        assert f'<li><strong>Alignment Status:</strong> {expected_html}</li>' in html_out
        assert '&lt;script&gt;' not in html_out
        assert '<script>' not in html_out

        md_out = render_deal_memo_markdown(payload, mode='extended')
        assert expected_md in md_out
        assert f'- {expected_md}' in md_out
        assert f'- **Competition:** {expected_md}' in md_out
        assert f'1. **[HIGH]** {expected_md}' in md_out
        assert f'- **Geography:** {expected_md} ({expected_md})' in md_out
        assert f'- **Sector:** {expected_md}' in md_out
        assert f'- **Stage:** {expected_md}' in md_out
        assert f'- **Alignment Status:** {expected_md}' in md_out
        assert '<script>' not in md_out

    def test_markdown_vc_scorecard_normalizes_newlines_in_table_cells(self):
        payload = _sample_payload(
            vc_metrics={
                'burn_multiple': {
                    'value': '1.5x\n(net)',
                    'assessment': 'Healthy\r\nQ3',
                    'detail': 'Line1\nLine2\r\nLine3 | note',
                }
            }
        )
        md_out = render_deal_memo_markdown(payload, mode='extended')
        assert '| Burn Multiple | 1.5x (net) | Healthy Q3 | Line1 Line2 Line3 \\| note |' in md_out.splitlines()


class TestGoogleDocExportEndpoint:
    """Integration tests for POST /api/export/pitchdeck/google-doc."""

    def test_requires_auth_and_google_drive_connection(self, client, auth_client, drive_wire):
        unauth = client.post('/api/export/pitchdeck/google-doc', json=_sample_payload())
        assert unauth.status_code == 401

        no_conn = auth_client.post('/api/export/pitchdeck/google-doc', json={'mode': 'brief', **_sample_payload()})
        assert no_conn.status_code == 401
        data = no_conn.get_json()
        assert data['success'] is False
        assert data['needs_google_auth'] is True
        assert drive_wire.calls == []

    @pytest.mark.parametrize(
        'bad_body',
        [
            {'mode': 'invalid', 'company_name': 'Acme', 'summary': 'Sum'},
            {'mode': 'brief', 'company_name': '   ', 'summary': 'Sum'},
            {'mode': 'brief', 'company_name': 'Acme', 'summary': ''},
            {'mode': 'brief', 'company_name': 'Acme', 'summary': 'Sum', 'folder_id': '../bad'},
            [],
        ],
    )
    def test_rejects_invalid_inputs_with_400(self, auth_client, drive_wire, bad_body):
        _connect_google(auth_client)
        drive_wire.calls.clear()
        resp = auth_client.post('/api/export/pitchdeck/google-doc', json=bad_body)
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False
        assert drive_wire.calls == []

    @pytest.mark.parametrize('payload_len,expected_status', [(MAX_EXPORT_JSON_BYTES, 200), (MAX_EXPORT_JSON_BYTES + 1, 400)])
    def test_256kb_payload_size_boundary(self, auth_client, drive_wire, payload_len, expected_status):
        _connect_google(auth_client)
        drive_wire.calls.clear()

        base = json.dumps({'mode': 'brief', 'company_name': 'Acme', 'summary': ''})
        pad_len = payload_len - len(base)
        raw_bytes = json.dumps({'mode': 'brief', 'company_name': 'Acme', 'summary': 'Z' * pad_len}).encode('utf-8')
        assert len(raw_bytes) == payload_len

        resp = auth_client.post('/api/export/pitchdeck/google-doc', data=raw_bytes, content_type='application/json')
        assert resp.status_code == expected_status

    def test_creates_reuses_and_recreates_trashed_briefs_folder(
        self, auth_client, db_session_fixture, test_user, drive_wire
    ):
        _connect_google(auth_client)
        drive_wire.calls.clear()

        # First export creates Authenix Deal Briefs and persists output_folder_ref
        r1 = auth_client.post('/api/export/pitchdeck/google-doc', json={
            'mode': 'brief',
            'extra_passthrough_field': 'preserved',
            **_sample_payload(),
        })
        assert r1.status_code == 200
        assert r1.get_json() == {
            'success': True,
            'doc_id': 'gdoc_1',
            'doc_url': 'https://docs.google.com/document/d/gdoc_1/edit',
        }
        db_session_fixture.expire_all()
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn.output_folder_ref == 'fld_created_1'

        # Second export reuses existing non-trashed folder
        drive_wire.calls.clear()
        r2 = auth_client.post('/api/export/pitchdeck/google-doc', json={
            'mode': 'extended',
            **_sample_payload(),
        })
        assert r2.status_code == 200
        assert len(drive_wire.created_folders) == 1
        get_calls = [c for c in drive_wire.calls if c['method'] == 'GET']
        assert len(get_calls) == 1
        assert get_calls[0]['params'] == {'fields': 'id,trashed'}

        # Third export recreates folder when marked trashed
        drive_wire.trashed_folders.add('fld_created_1')
        r3 = auth_client.post('/api/export/pitchdeck/google-doc', json={
            'mode': 'brief',
            **_sample_payload(),
        })
        assert r3.status_code == 200
        db_session_fixture.expire_all()
        conn2 = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert conn2.output_folder_ref == 'fld_created_2'

    def test_upload_401_triggers_single_refresh_retry_and_key_rotation_purges(
        self, auth_client, db_session_fixture, test_user, drive_wire, monkeypatch, caplog
    ):
        _connect_google(auth_client)
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        conn.output_folder_ref = 'fld_existing_1'
        db_session_fixture.commit()
        drive_wire.calls.clear()

        drive_wire.upload_status_queue.append((401, {'error': {'code': 401}}))
        with caplog.at_level(logging.INFO):
            r_retry = auth_client.post('/api/export/pitchdeck/google-doc', json={
                'mode': 'brief',
                **_sample_payload(),
            })
        assert r_retry.status_code == 200
        assert test_user.email not in caplog.text
        assert 'ya29.doc_access_1' not in caplog.text
        assert 'ya29.refreshed_doc_tok' not in caplog.text

        # Rotate key -> InvalidToken -> purge connection + AuditLog + 401 needs_google_auth
        monkeypatch.setattr(Config, 'GOOGLE_TOKEN_ENCRYPTION_KEY', 'rotated-export-key-0123456789', raising=False)
        drive_wire.calls.clear()
        r_rot = auth_client.post('/api/export/pitchdeck/google-doc', json={
            'mode': 'brief',
            **_sample_payload(),
        })
        assert r_rot.status_code == 401
        rot_json = r_rot.get_json()
        assert rot_json['success'] is False
        assert rot_json['needs_google_auth'] is True
        assert drive_wire.calls == []

        db_session_fixture.expire_all()
        assert db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).count() == 0
        assert db_session_fixture.query(AuditLog).filter_by(
            user_id=test_user.id, provider='google_drive', event_type='storage.invalid_token'
        ).count() == 1

    def test_transient_503_refresh_returns_502_and_preserves_connection(
        self, auth_client, db_session_fixture, test_user, drive_wire
    ):
        _connect_google(auth_client)
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        conn.output_folder_ref = 'fld_existing_503'
        db_session_fixture.commit()
        drive_wire.calls.clear()

        drive_wire.upload_status_queue.append((401, {'error': {'code': 401}}))
        drive_wire.refresh_queue.append((503, {'error': 'temporarily_unavailable'}))

        resp = auth_client.post('/api/export/pitchdeck/google-doc', json={
            'mode': 'brief',
            **_sample_payload(),
        })
        assert resp.status_code == 502
        body = resp.get_json()
        assert body['success'] is False
        assert 'needs_google_auth' not in body

        db_session_fixture.expire_all()
        assert db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).count() == 1
        assert db_session_fixture.query(AuditLog).filter_by(
            user_id=test_user.id, provider='google_drive', event_type='storage.reauth_required'
        ).count() == 0
        refresh_calls = [c for c in drive_wire.calls if c['url'] == 'https://oauth2.googleapis.com/token']
        assert len(refresh_calls) == 1

    def test_malformed_output_folder_ref_is_ignored_and_recreated(
        self, auth_client, db_session_fixture, test_user, drive_wire
    ):
        _connect_google(auth_client)
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        conn.output_folder_ref = '../../malformed/path'
        db_session_fixture.commit()
        drive_wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/google-doc', json={
            'mode': 'brief',
            **_sample_payload(),
        })
        assert resp.status_code == 200
        assert resp.get_json() == {
            'success': True,
            'doc_id': 'gdoc_1',
            'doc_url': 'https://docs.google.com/document/d/gdoc_1/edit',
        }
        get_calls = [c for c in drive_wire.calls if c['method'] == 'GET']
        assert get_calls == []
        assert len(drive_wire.created_folders) == 1
        assert drive_wire.created_folders[0]['json']['name'] == 'Authenix Deal Briefs'

        db_session_fixture.expire_all()
        updated = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert updated.output_folder_ref == 'fld_created_1'

    def test_unified_cloud_sync_export_uses_saved_preset_and_auto_creates_briefs_folder(
        self, auth_client, db_session_fixture, test_user, drive_wire
    ):
        _connect_google(auth_client)
        conn = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        conn.memo_preset = 'extended'
        db_session_fixture.commit()
        drive_wire.calls.clear()

        resp = auth_client.post('/api/export/pitchdeck/cloud-sync', json=_sample_payload())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {
            'success': True,
            'provider': 'google_drive',
            'mode': 'extended',
            'memo_id': 'gdoc_1',
            'memo_url': 'https://docs.google.com/document/d/gdoc_1/edit',
            'updated_sheet_range': None,
            'sheet_url': None,
        }
        assert len(drive_wire.uploaded_docs) == 1
        assert 'Burn Multiple' in drive_wire.uploaded_docs[0]['body']
        db_session_fixture.expire_all()
        updated = db_session_fixture.query(CloudStorageConnection).filter_by(
            user_id=test_user.id, provider='google_drive'
        ).one()
        assert updated.output_folder_ref == 'fld_created_1'
