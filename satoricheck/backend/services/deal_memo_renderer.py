"""Deal memo rendering (Brief & Extended presets in HTML and Markdown) and Google Docs upload."""
from __future__ import annotations

from datetime import datetime, UTC
import html
import json
import re
import secrets

from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from backend.services.cloud_tokens import CloudTokenError, execute_with_token_refresh
from backend.services.safe_http import safe_api_request

MAX_COMPANY_NAME_LENGTH = 200
MAX_FIELD_LENGTH = 5000
MAX_LIST_ITEMS = 20
MAX_CLAIMS = 50
MAX_FOLLOWUP_QUESTIONS = 3

VALID_MEMO_MODES = ('brief', 'extended')
FOLDER_ID_RE = re.compile(r'^[A-Za-z0-9_-]{3,128}$')

_VC_METRIC_LABELS = (
    ('monthly_revenue_arr', 'Revenue / ARR'),
    ('burn_multiple', 'Burn Multiple'),
    ('nrr_percent', 'NRR'),
    ('cac_payback_months', 'CAC Payback'),
    ('ltv_cac_ratio', 'LTV:CAC'),
    ('runway_months', 'Runway'),
)


def _raw_text(value, max_len: int = MAX_FIELD_LENGTH) -> str:
    """Convert a non-None value to a bounded raw string without unescaping."""
    if value is None:
        return ''
    return str(value)[:max_len]


def _safe_text(value, max_len: int = MAX_FIELD_LENGTH) -> str:
    """Normalize pre-escaped strings and bound character length."""
    if value is None:
        return ''
    text = html.unescape(str(value))
    return text[:max_len]


def _safe_html(value, max_len: int = MAX_FIELD_LENGTH) -> str:
    """Idempotently HTML-escape a bounded field without double-escaping existing entities."""
    return html.escape(_safe_text(value, max_len), quote=True)


def _safe_md(value, max_len: int = MAX_FIELD_LENGTH) -> str:
    """Return clean unescaped UTF-8 text bounded for Markdown output."""
    return _safe_text(value, max_len)


def _md_table_cell(value, max_len: int) -> str:
    """Render a single-line escaped Markdown table cell."""
    text = _safe_md(value, max_len)
    return re.sub(r'[\r\n]+', ' ', text).replace('|', '\\|')


def resolve_analyzed_date(payload: dict) -> str:
    """Return YYYY-MM-DD date string from payload or current UTC date."""
    raw = payload.get('analyzed_at') or payload.get('date_analyzed')
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:64]
    return datetime.now(UTC).strftime('%Y-%m-%d')


def _collect_red_flags(payload: dict) -> list[str]:
    """Collect bounded deck-level red flags plus any high-risk contradicted claim findings."""
    flags: list[str] = []
    raw_flags = payload.get('red_flags')
    if isinstance(raw_flags, list):
        for item in raw_flags[:MAX_LIST_ITEMS]:
            if item is not None:
                text = _raw_text(item, MAX_FIELD_LENGTH).strip()
                if text:
                    flags.append(text)

    if len(flags) < MAX_LIST_ITEMS:
        raw_claims = payload.get('verifiable_claims')
        if isinstance(raw_claims, list):
            for claim in raw_claims[:MAX_CLAIMS]:
                if len(flags) >= MAX_LIST_ITEMS:
                    break
                if not isinstance(claim, dict):
                    continue
                risk = str(claim.get('risk_level') or '').strip().lower()
                finding = _raw_text(claim.get('verification_finding'), 500).strip()
                claim_text = _raw_text(claim.get('claim'), 500).strip()
                if risk == 'high' and finding and claim_text:
                    entry = f'{claim_text} — {finding}'
                    if entry not in flags:
                        flags.append(entry)
    return flags


def _collect_competition(payload: dict) -> list[str]:
    """Collect bounded list of competitor names."""
    raw_comp = payload.get('competition')
    if not isinstance(raw_comp, list):
        return []
    out: list[str] = []
    for item in raw_comp[:MAX_LIST_ITEMS]:
        if item is not None:
            text = _raw_text(item, MAX_FIELD_LENGTH).strip()
            if text:
                out.append(text)
    return out


def _select_followup_questions(payload: dict, limit: int = MAX_FOLLOWUP_QUESTIONS) -> list[dict]:
    """Select top follow-up questions prioritized high risk before medium risk."""
    raw_claims = payload.get('verifiable_claims')
    if not isinstance(raw_claims, list):
        return []
    candidates: list[tuple[int, int, dict]] = []
    for idx, claim in enumerate(raw_claims[:MAX_CLAIMS]):
        if not isinstance(claim, dict):
            continue
        risk = str(claim.get('risk_level') or '').strip().lower()
        if risk not in ('high', 'medium'):
            continue
        question = _raw_text(claim.get('suggested_question'), MAX_FIELD_LENGTH).strip()
        if not question:
            continue
        priority = 0 if risk == 'high' else 1
        candidates.append((priority, idx, {
            'risk_level': risk,
            'claim': _raw_text(claim.get('claim'), MAX_FIELD_LENGTH).strip(),
            'question': question,
        }))
    candidates.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in candidates[:limit]]


def _unwrap_thesis_val(val):
    """Unwrap nested frontend-shaped {'extracted': ...} dicts."""
    if isinstance(val, dict):
        return val.get('extracted')
    return val


def _extract_thesis_alignment(payload: dict) -> dict:
    """Extract normalized thesis alignment fields from thesis_alignment or startup_profile."""
    raw_thesis = payload.get('thesis_alignment')
    profile = payload.get('startup_profile') if isinstance(payload.get('startup_profile'), dict) else {}
    source = raw_thesis if isinstance(raw_thesis, dict) else profile

    raw_geo = _unwrap_thesis_val(source.get('geography')) or _unwrap_thesis_val(profile.get('geography')) or 'Unknown'
    geography = _raw_text(raw_geo, 500)
    raw_tags = source.get('geography_tags') or profile.get('geography_tags')
    if isinstance(raw_tags, list) and raw_tags:
        tags = [_raw_text(t, 100) for t in raw_tags[:10] if t]
        if tags:
            geography = f"{geography} ({', '.join(tags)})"

    raw_sector = (
        _unwrap_thesis_val(source.get('sector'))
        or _unwrap_thesis_val(profile.get('sector'))
        or payload.get('sector')
        or payload.get('industry')
        or 'Unknown'
    )
    sector = _raw_text(raw_sector, 500)
    raw_stage = _unwrap_thesis_val(source.get('stage')) or _unwrap_thesis_val(profile.get('stage')) or 'Unknown'
    stage = _raw_text(raw_stage, 200)
    status = _raw_text(source.get('status') or source.get('fit') or '', 200)
    return {
        'geography': geography,
        'sector': sector,
        'stage': stage,
        'status': status,
    }


def build_deal_memo_title(company_name: str, analyzed_date: str | None = None) -> str:
    """Build safe Google Doc title."""
    no_angles = re.sub(r'[<>]', '', _safe_text(company_name, 120))
    clean = re.sub(r'[\r\n\t\x00-\x1f\x7f]+', ' ', no_angles).strip() or 'Unknown Company'
    raw_date = _safe_text(analyzed_date, 64) if analyzed_date is not None else ''
    date_no_angles = re.sub(r'[<>]', '', raw_date)
    date_str = re.sub(r'[\r\n\t\x00-\x1f\x7f]+', ' ', date_no_angles).strip() or datetime.now(UTC).strftime('%Y-%m-%d')
    return f'Authenix Brief — {clean} ({date_str})'


def build_deal_memo_filename(company_name: str, analyzed_date: str | None = None) -> str:
    """Build safe .md filename for Dropbox/OneDrive output folders."""
    clean = re.sub(r'[/\\:*?"<>|\r\n\t\x00-\x1f\x7f]+', '-', _safe_text(company_name, 100)).strip() or 'Unknown Company'
    raw_date = _safe_text(analyzed_date, 64) if analyzed_date is not None else ''
    clean_date = re.sub(r'[/\\:*?"<>|\r\n\t\x00-\x1f\x7f]+', '-', raw_date)
    clean_date = re.sub(r'\.{2,}', '-', clean_date).strip(' -.')
    if not clean_date:
        clean_date = datetime.now(UTC).strftime('%Y-%m-%d')
    return f'Authenix Brief - {clean} ({clean_date}).md'


def render_deal_memo_html(payload: dict, mode: str = 'brief') -> str:
    """Render a pitch deck evaluation into an idempotently escaped HTML deal memo."""
    normalized_mode = (mode or 'brief').strip().lower()
    if normalized_mode not in VALID_MEMO_MODES:
        raise ValueError(f'Unsupported memo mode: {mode}')

    company_name = _safe_html(payload.get('company_name') or 'Unknown Company', MAX_COMPANY_NAME_LENGTH)
    date_str = _safe_html(resolve_analyzed_date(payload), 64)
    summary = _safe_html(payload.get('summary') or '', MAX_FIELD_LENGTH)
    red_flags = _collect_red_flags(payload)

    parts = [
        '<!DOCTYPE html>',
        '<html><head><meta charset="UTF-8"><title>Authenix Deal Memo</title></head>',
        '<body style="font-family: Arial, sans-serif; color: #111827; line-height: 1.5;">',
        f'<h1>{company_name}</h1>',
        f'<p><strong>Date Analyzed:</strong> {date_str}</p>',
    ]

    if normalized_mode == 'brief':
        parts.append('<h2>Executive Summary</h2>')
        parts.append(f'<p>{summary}</p>')
        parts.append('<h2>Potential Red Flags</h2>')
        if red_flags:
            parts.append('<ul>')
            for flag in red_flags:
                parts.append(f'<li>{_safe_html(flag, MAX_FIELD_LENGTH)}</li>')
            parts.append('</ul>')
        else:
            parts.append('<p>None flagged.</p>')
        parts.append('</body></html>')
        return '\n'.join(parts)

    # Extended preset (5 structured sections)
    industry = _safe_html(payload.get('industry') or 'Not specified', MAX_FIELD_LENGTH)
    sector = _safe_html(payload.get('sector') or 'Not specified', MAX_FIELD_LENGTH)
    funding_ask = _safe_html(payload.get('funding_ask') or 'Not specified', MAX_FIELD_LENGTH)
    investor_decision = _safe_html(payload.get('investor_decision') or 'Pending Review', MAX_FIELD_LENGTH)

    parts.extend([
        '<h2>1. Summary &amp; Metadata</h2>',
        '<ul>',
        f'<li><strong>Industry:</strong> {industry}</li>',
        f'<li><strong>Sector:</strong> {sector}</li>',
        f'<li><strong>Funding Ask:</strong> {funding_ask}</li>',
        f'<li><strong>Investor Decision:</strong> {investor_decision}</li>',
        '</ul>',
        f'<p>{summary}</p>',
    ])

    usp = _safe_html(payload.get('usp') or 'Not specified', MAX_FIELD_LENGTH)
    market_size = _safe_html(payload.get('market_size') or 'Not specified', MAX_FIELD_LENGTH)
    team_highlights = _safe_html(payload.get('team_highlights') or 'Not specified', MAX_FIELD_LENGTH)
    competition = _collect_competition(payload)
    comp_html = ', '.join(_safe_html(c, MAX_FIELD_LENGTH) for c in competition) if competition else 'None specified'

    parts.extend([
        '<h2>2. Business Case Info</h2>',
        '<ul>',
        f'<li><strong>USP:</strong> {usp}</li>',
        f'<li><strong>Market Size:</strong> {market_size}</li>',
        f'<li><strong>Competition:</strong> {comp_html}</li>',
        f'<li><strong>Team Highlights:</strong> {team_highlights}</li>',
        '</ul>',
    ])

    parts.append('<h2>3. Red Flags &amp; Follow-Up Questions</h2>')
    parts.append('<h3>Potential Red Flags</h3>')
    if red_flags:
        parts.append('<ul>')
        for flag in red_flags:
            parts.append(f'<li>{_safe_html(flag, MAX_FIELD_LENGTH)}</li>')
        parts.append('</ul>')
    else:
        parts.append('<p>None flagged.</p>')

    questions = _select_followup_questions(payload)
    parts.append('<h3>Ask the Founder in Follow-Up</h3>')
    if questions:
        parts.append('<ol>')
        for q in questions:
            risk_label = _safe_html(q['risk_level'].upper(), 32)
            parts.append(f'<li><strong>[{risk_label}]</strong> {_safe_html(q["question"], MAX_FIELD_LENGTH)}</li>')
        parts.append('</ol>')
    else:
        parts.append('<p>No high or medium risk follow-up questions generated.</p>')

    parts.append('<h2>4. VC Scorecard</h2>')
    raw_metrics = payload.get('vc_metrics') if isinstance(payload.get('vc_metrics'), dict) else {}
    parts.append('<table border="1" cellpadding="6" cellspacing="0"><thead><tr>'
                 '<th>Metric</th><th>Value</th><th>Assessment</th><th>Detail</th>'
                 '</tr></thead><tbody>')
    for key, label in _VC_METRIC_LABELS:
        m = raw_metrics.get(key) if isinstance(raw_metrics.get(key), dict) else {}
        val = _safe_html(m.get('value') or 'Not Disclosed', 500)
        assess = _safe_html(m.get('assessment') or 'Not Disclosed', 100)
        detail = _safe_html(m.get('detail') or '', MAX_FIELD_LENGTH)
        parts.append(
            f'<tr><td>{_safe_html(label, 100)}</td><td>{val}</td><td>{assess}</td><td>{detail}</td></tr>'
        )
    parts.append('</tbody></table>')

    thesis = _extract_thesis_alignment(payload)
    investor_notes = _safe_html(payload.get('investor_notes') or '', MAX_FIELD_LENGTH)
    parts.extend([
        '<h2>5. Thesis Alignment</h2>',
        '<ul>',
        f'<li><strong>Geography:</strong> {_safe_html(thesis["geography"], 500)}</li>',
        f'<li><strong>Sector:</strong> {_safe_html(thesis["sector"], 500)}</li>',
        f'<li><strong>Stage:</strong> {_safe_html(thesis["stage"], 200)}</li>',
    ])
    if thesis['status']:
        parts.append(f'<li><strong>Alignment Status:</strong> {_safe_html(thesis["status"], 200)}</li>')
    parts.append('</ul>')
    if investor_notes:
        parts.append(f'<p><strong>Investor Notes:</strong> {investor_notes}</p>')

    parts.append('</body></html>')
    return '\n'.join(parts)


def render_deal_memo_markdown(payload: dict, mode: str = 'brief') -> str:
    """Render a pitch deck evaluation into clean unescaped UTF-8 Markdown."""
    normalized_mode = (mode or 'brief').strip().lower()
    if normalized_mode not in VALID_MEMO_MODES:
        raise ValueError(f'Unsupported memo mode: {mode}')

    company_name = _safe_md(payload.get('company_name') or 'Unknown Company', MAX_COMPANY_NAME_LENGTH)
    date_str = _safe_md(resolve_analyzed_date(payload), 64)
    summary = _safe_md(payload.get('summary') or '', MAX_FIELD_LENGTH)
    red_flags = _collect_red_flags(payload)

    lines = [
        f'# {company_name}',
        f'**Date Analyzed:** {date_str}',
        '',
    ]

    if normalized_mode == 'brief':
        lines.extend([
            '## Executive Summary',
            summary,
            '',
            '## Potential Red Flags',
        ])
        if red_flags:
            for flag in red_flags:
                lines.append(f'- {_safe_md(flag, MAX_FIELD_LENGTH)}')
        else:
            lines.append('- None flagged.')
        return '\n'.join(lines) + '\n'

    # Extended preset
    industry = _safe_md(payload.get('industry') or 'Not specified', MAX_FIELD_LENGTH)
    sector = _safe_md(payload.get('sector') or 'Not specified', MAX_FIELD_LENGTH)
    funding_ask = _safe_md(payload.get('funding_ask') or 'Not specified', MAX_FIELD_LENGTH)
    investor_decision = _safe_md(payload.get('investor_decision') or 'Pending Review', MAX_FIELD_LENGTH)

    lines.extend([
        '## 1. Summary & Metadata',
        f'- **Industry:** {industry}',
        f'- **Sector:** {sector}',
        f'- **Funding Ask:** {funding_ask}',
        f'- **Investor Decision:** {investor_decision}',
        '',
        summary,
        '',
    ])

    usp = _safe_md(payload.get('usp') or 'Not specified', MAX_FIELD_LENGTH)
    market_size = _safe_md(payload.get('market_size') or 'Not specified', MAX_FIELD_LENGTH)
    team_highlights = _safe_md(payload.get('team_highlights') or 'Not specified', MAX_FIELD_LENGTH)
    competition = _collect_competition(payload)
    comp_md = ', '.join(_safe_md(c, MAX_FIELD_LENGTH) for c in competition) if competition else 'None specified'

    lines.extend([
        '## 2. Business Case Info',
        f'- **USP:** {usp}',
        f'- **Market Size:** {market_size}',
        f'- **Competition:** {comp_md}',
        f'- **Team Highlights:** {team_highlights}',
        '',
        '## 3. Red Flags & Follow-Up Questions',
        '### Potential Red Flags',
    ])
    if red_flags:
        for flag in red_flags:
            lines.append(f'- {_safe_md(flag, MAX_FIELD_LENGTH)}')
    else:
        lines.append('- None flagged.')

    questions = _select_followup_questions(payload)
    lines.append('')
    lines.append('### Ask the Founder in Follow-Up')
    if questions:
        for idx, q in enumerate(questions, start=1):
            lines.append(f"{idx}. **[{q['risk_level'].upper()}]** {_safe_md(q['question'], MAX_FIELD_LENGTH)}")
    else:
        lines.append('- No high or medium risk follow-up questions generated.')

    lines.extend([
        '',
        '## 4. VC Scorecard',
        '| Metric | Value | Assessment | Detail |',
        '| --- | --- | --- | --- |',
    ])
    raw_metrics = payload.get('vc_metrics') if isinstance(payload.get('vc_metrics'), dict) else {}
    for key, label in _VC_METRIC_LABELS:
        m = raw_metrics.get(key) if isinstance(raw_metrics.get(key), dict) else {}
        val = _md_table_cell(m.get('value') or 'Not Disclosed', 500)
        assess = _md_table_cell(m.get('assessment') or 'Not Disclosed', 100)
        detail = _md_table_cell(m.get('detail') or '', MAX_FIELD_LENGTH)
        lines.append(f'| {label} | {val} | {assess} | {detail} |')

    thesis = _extract_thesis_alignment(payload)
    investor_notes = _safe_md(payload.get('investor_notes') or '', MAX_FIELD_LENGTH)
    lines.extend([
        '',
        '## 5. Thesis Alignment',
        f"- **Geography:** {_safe_md(thesis['geography'], 500)}",
        f"- **Sector:** {_safe_md(thesis['sector'], 500)}",
        f"- **Stage:** {_safe_md(thesis['stage'], 200)}",
    ])
    if thesis['status']:
        lines.append(f"- **Alignment Status:** {_safe_md(thesis['status'], 200)}")
    if investor_notes:
        lines.append(f'- **Investor Notes:** {investor_notes}')

    return '\n'.join(lines) + '\n'


def ensure_google_briefs_folder(session_db, conn, requested_folder_id: str | None = None) -> str:
    """Resolve explicit folder_id or verify/create 'Authenix Deal Briefs' on Google Drive."""
    if requested_folder_id:
        clean_id = requested_folder_id.strip()
        if not FOLDER_ID_RE.fullmatch(clean_id):
            raise ValueError('Invalid Google Drive folder_id')
        return clean_id

    if isinstance(conn.output_folder_ref, str) and FOLDER_ID_RE.fullmatch(conn.output_folder_ref):
        existing_id = conn.output_folder_ref
        resp = execute_with_token_refresh(
            session_db,
            conn,
            lambda tok, fid=existing_id: safe_api_request(
                'GET',
                f'https://www.googleapis.com/drive/v3/files/{fid}',
                headers={'Authorization': f'Bearer {tok}'},
                params={'fields': 'id,trashed'},
                timeout=10,
            ),
        )
        if resp.status_code == 200:
            try:
                meta = resp.json()
            except ValueError as exc:
                raise RuntimeError('Google Drive folder check returned invalid JSON') from exc
            if not isinstance(meta, dict):
                raise RuntimeError('Google Drive folder check returned invalid metadata')
            if not meta.get('trashed'):
                return existing_id
        elif resp.status_code != 404:
            raise RuntimeError(f'Google Drive folder check failed with status {resp.status_code}')

    create_resp = execute_with_token_refresh(
        session_db,
        conn,
        lambda tok: safe_api_request(
            'POST',
            'https://www.googleapis.com/drive/v3/files',
            headers={'Authorization': f'Bearer {tok}'},
            json_body={
                'name': 'Authenix Deal Briefs',
                'mimeType': 'application/vnd.google-apps.folder',
            },
            timeout=10,
        ),
    )
    if create_resp.status_code not in (200, 201):
        raise RuntimeError(f'Google Drive folder creation failed with status {create_resp.status_code}')

    created = create_resp.json()
    folder_id = created.get('id') if isinstance(created, dict) else None
    if not folder_id:
        raise RuntimeError('Google Drive folder creation response missing id')

    try:
        conn.output_folder_ref = folder_id
        session_db.commit()
    except (StaleDataError, ObjectDeletedError) as exc:
        session_db.rollback()
        raise CloudTokenError(
            'Google Drive authorization required',
            event_type='storage.reauth_required',
            status_code=401,
        ) from exc
    return folder_id


def upload_google_doc_multipart(
    session_db,
    conn,
    *,
    title: str,
    html_body: str,
    folder_id: str,
) -> tuple[str, str]:
    """Upload HTML content as a native Google Doc via Drive v3 multipart/related."""
    boundary = f'authenix_doc_{secrets.token_hex(12)}'
    metadata_json = json.dumps({
        'name': title,
        'mimeType': 'application/vnd.google-apps.document',
        'parents': [folder_id],
    })
    multipart_bytes = (
        f'--{boundary}\r\n'
        'Content-Type: application/json; charset=UTF-8\r\n\r\n'
        f'{metadata_json}\r\n'
        f'--{boundary}\r\n'
        'Content-Type: text/html; charset=UTF-8\r\n\r\n'
        f'{html_body}\r\n'
        f'--{boundary}--\r\n'
    ).encode('utf-8')

    resp = execute_with_token_refresh(
        session_db,
        conn,
        lambda tok: safe_api_request(
            'POST',
            'https://www.googleapis.com/upload/drive/v3/files',
            headers={
                'Authorization': f'Bearer {tok}',
                'Content-Type': f'multipart/related; boundary={boundary}',
            },
            params={'uploadType': 'multipart', 'fields': 'id,webViewLink'},
            data=multipart_bytes,
            timeout=15,
        ),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f'Google Drive upload failed with status {resp.status_code}')

    data = resp.json()
    doc_id = data.get('id') if isinstance(data, dict) else None
    if not doc_id:
        raise RuntimeError('Google Drive upload response missing document id')
    doc_url = data.get('webViewLink') or f'https://docs.google.com/document/d/{doc_id}/edit'
    return doc_id, doc_url
