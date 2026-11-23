"""Google Sheets v4 append service for pitch deck evaluations and folder sync auto-export."""
from __future__ import annotations

import html
import logging
import math
import re
from urllib.parse import quote, unquote, urlparse

from backend.models import CloudStorageConnection
from backend.services.cloud_tokens import execute_with_token_refresh
from backend.services.deal_memo_renderer import (
    MAX_COMPANY_NAME_LENGTH,
    MAX_FIELD_LENGTH,
    _collect_red_flags,
    _select_followup_questions,
    resolve_analyzed_date,
)
from backend.services.safe_http import safe_api_request

logger = logging.getLogger(__name__)

DEFAULT_SHEET_NAME = 'Sheet1'
FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r', '\n')
SPREADSHEET_ID_RE = re.compile(r'^[A-Za-z0-9_-]{20,128}$')
SHEET_NAME_RE = re.compile(r'^[^:\[\]\*?/\\!]{1,100}$')
_SHEETS_URL_PATH_RE = re.compile(r'^/spreadsheets/d/([A-Za-z0-9_-]{20,128})(?:/.*)?$')


def parse_spreadsheet_id(raw_value: str) -> str:
    """Parse and validate a Google Sheets spreadsheet ID or docs.google.com URL."""
    if not isinstance(raw_value, str):
        raise ValueError('Spreadsheet ID must be a string')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_value):
        raise ValueError('Invalid Google Sheets spreadsheet ID')
    cleaned = raw_value.strip(' ')
    if not cleaned:
        raise ValueError('Invalid Google Sheets spreadsheet ID')

    if '://' in cleaned or cleaned.startswith('docs.google.com/'):
        candidate = cleaned if '://' in cleaned else f'https://{cleaned}'
        try:
            parsed = urlparse(candidate)
        except ValueError as exc:
            raise ValueError('Invalid Google Sheets URL') from exc
        if parsed.scheme != 'https' or (parsed.netloc or '').lower() not in (
            'docs.google.com',
            'docs.google.com:443',
        ):
            raise ValueError('Invalid Google Sheets URL host')
        raw_path = parsed.path or ''
        unquoted_path = unquote(raw_path)
        if '..' in raw_path.split('/') or '..' in unquoted_path.split('/'):
            raise ValueError('Invalid Google Sheets URL path')
        match = _SHEETS_URL_PATH_RE.fullmatch(raw_path)
        if not match:
            raise ValueError('Invalid Google Sheets URL path')
        sheet_id = match.group(1)
    else:
        sheet_id = cleaned

    if not SPREADSHEET_ID_RE.fullmatch(sheet_id):
        raise ValueError('Invalid Google Sheets spreadsheet ID format')
    return sheet_id


def validate_sheet_name(raw_name: str) -> str:
    """Validate a Google Sheets tab name without range-injection characters."""
    if not isinstance(raw_name, str):
        raise ValueError('Sheet name must be a string')
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_name):
        raise ValueError('Invalid sheet name')
    cleaned = raw_name.strip(' ')
    if not cleaned:
        raise ValueError('Invalid sheet name')
    if cleaned.startswith("'") or cleaned.endswith("'"):
        raise ValueError('Invalid sheet name quoting')
    if not SHEET_NAME_RE.fullmatch(cleaned):
        raise ValueError('Invalid characters or length in sheet name')
    return cleaned


def sanitize_sheet_cell(value, max_len: int = MAX_FIELD_LENGTH) -> str:
    """Unescape HTML entities and prefix leading spreadsheet formula characters."""
    if value is None:
        return ''
    text = html.unescape(str(value))[:max_len]
    if (
        text.startswith(FORMULA_PREFIXES)
        or text.lstrip(' ').startswith(FORMULA_PREFIXES)
        or text.lstrip().startswith(('=', '+', '-', '@'))
    ):
        return f"'{text}"
    return text


def _format_a1_range(sheet_name: str | None) -> str:
    """Format a validated A1 range target for Google Sheets values:append."""
    if sheet_name is not None and sheet_name != '':
        clean = validate_sheet_name(sheet_name)
        escaped = clean.replace("'", "''")
        return f"'{escaped}'!A1"
    return f'{DEFAULT_SHEET_NAME}!A1'


def _extract_stage(payload: dict) -> str:
    """Extract startup stage from top-level payload, startup_profile, or thesis_alignment."""
    stage = payload.get('stage')
    if not stage and isinstance(payload.get('startup_profile'), dict):
        stage = payload['startup_profile'].get('stage')
    if not stage and isinstance(payload.get('thesis_alignment'), dict):
        raw_stage = payload['thesis_alignment'].get('stage')
        if isinstance(raw_stage, dict):
            stage = raw_stage.get('extracted')
        else:
            stage = raw_stage
    return sanitize_sheet_cell(stage or '', 200)


def _extract_sector(payload: dict) -> str:
    """Extract sector from top-level payload, startup_profile, or thesis_alignment."""
    sector = payload.get('sector')
    if not sector and isinstance(payload.get('startup_profile'), dict):
        sector = payload['startup_profile'].get('sector')
    if not sector and isinstance(payload.get('thesis_alignment'), dict):
        raw_sector = payload['thesis_alignment'].get('sector')
        if isinstance(raw_sector, dict):
            sector = raw_sector.get('extracted')
        else:
            sector = raw_sector
    return sanitize_sheet_cell(sector or '', 500)


def _extract_page_count(payload: dict) -> int:
    """Extract a non-negative integer page_count."""
    raw = payload.get('page_count')
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return max(0, raw)
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return 0
        return max(0, int(raw))
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isascii() and stripped.isdigit():
            return int(stripped)
    return 0


def _format_red_flags_cell(payload: dict) -> str:
    """Format sanitized red flags joined by '; '."""
    flags = _collect_red_flags(payload)
    if not flags and isinstance(payload.get('red_flags'), str) and payload['red_flags'].strip():
        flags = [payload['red_flags'].strip()]
    sanitized = [sanitize_sheet_cell(f, 500) for f in flags if f is not None and str(f).strip()]
    return '; '.join(sanitized)[:MAX_FIELD_LENGTH]


def _format_founder_questions_cell(payload: dict) -> str:
    """Format top-3 prioritized founder questions without extra prefixes so formula safety holds."""
    questions = _select_followup_questions(payload, limit=3)
    items = [sanitize_sheet_cell(q['question'], 500) for q in questions if q.get('question')]
    if not items:
        raw_fq = payload.get('founder_questions')
        if isinstance(raw_fq, list):
            items = [sanitize_sheet_cell(q, 500) for q in raw_fq[:3] if q is not None and str(q).strip()]
        elif isinstance(raw_fq, str) and raw_fq.strip():
            items = [sanitize_sheet_cell(raw_fq.strip(), 500)]
    return '; '.join(items)[:MAX_FIELD_LENGTH]


def build_pitchdeck_sheet_row(payload: dict, *, memo_url: str | None = None) -> list:
    """Build the 12-column Google Sheets row for a pitch deck evaluation."""
    resolved_memo_url = memo_url if memo_url is not None else (payload.get('memo_url') or '')
    return [
        sanitize_sheet_cell(resolve_analyzed_date(payload), 64),
        sanitize_sheet_cell(payload.get('company_name') or 'Unknown Company', MAX_COMPANY_NAME_LENGTH),
        sanitize_sheet_cell(payload.get('industry') or '', 500),
        _extract_sector(payload),
        _extract_stage(payload),
        sanitize_sheet_cell(payload.get('funding_ask') or '', 500),
        _extract_page_count(payload),
        sanitize_sheet_cell(payload.get('investor_decision') or 'Pending Review', 200),
        sanitize_sheet_cell(resolved_memo_url, 512),
        sanitize_sheet_cell(payload.get('summary') or '', MAX_FIELD_LENGTH),
        _format_red_flags_cell(payload),
        _format_founder_questions_cell(payload),
    ]


def append_pitchdeck_to_sheet(
    session_db,
    google_conn: CloudStorageConnection,
    *,
    spreadsheet_id: str,
    sheet_name: str | None = None,
    payload: dict,
    memo_url: str | None = None,
) -> dict:
    """Append a structured 12-column row to a Google Sheet via Sheets API v4."""
    clean_id = parse_spreadsheet_id(spreadsheet_id)
    range_a1 = _format_a1_range(sheet_name)
    row = build_pitchdeck_sheet_row(payload, memo_url=memo_url)
    encoded_range = quote(range_a1, safe='')
    url = f'https://sheets.googleapis.com/v4/spreadsheets/{clean_id}/values/{encoded_range}:append'

    resp = execute_with_token_refresh(
        session_db,
        google_conn,
        lambda tok: safe_api_request(
            'POST',
            url,
            headers={'Authorization': f'Bearer {tok}'},
            params={'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'},
            json_body={'values': [row]},
            timeout=15,
        ),
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f'Google Sheets append failed with status {resp.status_code}')

    try:
        data = resp.json()
    except ValueError:
        data = {}

    updated_range = ''
    if isinstance(data, dict):
        updates = data.get('updates')
        if isinstance(updates, dict) and updates.get('updatedRange'):
            updated_range = str(updates['updatedRange'])
        elif data.get('tableRange'):
            updated_range = str(data['tableRange'])

    return {
        'spreadsheet_id': clean_id,
        'updated_range': updated_range,
        'sheet_url': f'https://docs.google.com/spreadsheets/d/{clean_id}',
    }


def auto_export_synced_deck_to_sheet(
    session_db,
    conn: CloudStorageConnection,
    *,
    analysis_result: dict,
    memo_url: str,
) -> str | None:
    """Append a synced deck row to the user's configured Google Sheet (same or cross-provider)."""
    user_id = conn.user_id
    provider = conn.provider
    raw_sheet_id = conn.sheets_spreadsheet_id
    raw_sheet_name = conn.sheets_sheet_name

    if not conn.auto_export_sheets or not raw_sheet_id:
        return None

    if provider == 'google_drive':
        google_conn = conn
    else:
        google_conn = (
            session_db.query(CloudStorageConnection)
            .filter_by(user_id=user_id, provider='google_drive')
            .first()
        )

    if google_conn is None or not (google_conn.encrypted_access_token or google_conn.encrypted_refresh_token):
        logger.warning(
            f"Skipping Google Sheets auto-export: missing Google Drive connection "
            f"(user_id={user_id}, provider={provider})"
        )
        return None

    spreadsheet_id = parse_spreadsheet_id(raw_sheet_id)
    sheet_name = validate_sheet_name(raw_sheet_name) if raw_sheet_name else None
    result = append_pitchdeck_to_sheet(
        session_db,
        google_conn,
        spreadsheet_id=spreadsheet_id,
        sheet_name=sheet_name,
        payload=analysis_result,
        memo_url=memo_url,
    )
    return result.get('updated_range') or None
