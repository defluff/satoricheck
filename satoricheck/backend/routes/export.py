"""
Export routes.
Handles data export functionality.
"""
from flask import Blueprint, request, make_response, jsonify
import csv
import io
import json
import logging
import re
from datetime import datetime, UTC

from backend.config import Config
from backend.database import db_session
from backend.models import CloudStorageConnection, FactCheck
from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.extensions import limiter
from backend.services.cloud_tokens import CloudTokenError, VALID_PROVIDERS
from backend.services.deal_memo_renderer import (
    FOLDER_ID_RE,
    VALID_MEMO_MODES,
    build_deal_memo_title,
    ensure_google_briefs_folder,
    render_deal_memo_html,
    resolve_analyzed_date,
    upload_google_doc_multipart,
)
from backend.services.google_sheets_service import (
    FORMULA_PREFIXES,
    append_pitchdeck_to_sheet,
    auto_export_synced_deck_to_sheet,
    parse_spreadsheet_id,
    validate_sheet_name,
)
from backend.services.storage_providers import (
    ProviderFolderError,
    ProviderUpstreamError,
    get_storage_provider,
)

logger = logging.getLogger(__name__)

export_bp = Blueprint('export', __name__, url_prefix='/api/export')

MAX_EXPORT_ROWS = 1000
MAX_EXPORT_JSON_BYTES = 256 * 1024
_SOURCE_FILTER_RE = re.compile(r'^[A-Za-z0-9_-]{1,50}$')
_CSV_FORMULA_PREFIXES = FORMULA_PREFIXES


def _sanitize_csv_cell(value) -> str:
    """Prefix leading spreadsheet formula characters to neutralize CSV injection."""
    if value is None:
        return ''
    text = str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES) or text.lstrip().startswith(('=', '+', '-', '@')):
        return f"'{text}"
    return text


@export_bp.route('/factchecks', methods=['GET'])
@login_required
@limiter.limit("10 per minute")
def export_fact_checks():
    """Export user's fact-check history as CSV."""
    try:
        user = request.current_user

        # Get filters
        format_type = request.args.get('format', 'csv').lower()
        source_filter = request.args.get('source')
        if source_filter is not None and source_filter != '':
            if not _SOURCE_FILTER_RE.fullmatch(source_filter):
                raise APIError('Invalid source filter', status_code=400)
        else:
            source_filter = None

        # Get bounded fact checks
        query = db_session.query(FactCheck).filter_by(user_id=user.id)
        if source_filter:
            query = query.filter_by(source=source_filter)

        fact_checks = query.order_by(FactCheck.timestamp.desc()).limit(MAX_EXPORT_ROWS).all()

        if format_type == 'csv':
            # Create CSV
            output = io.StringIO()
            writer = csv.writer(output)

            # Write header
            writer.writerow([
                'ID',
                'Date',
                'Source',
                'Source ID',
                'Claim Text',
                'Verdict',
                'Explanation',
                'Fallacy',
                'Sources',
            ])

            # Write data
            for fc in fact_checks:
                # Handle sources formatting
                sources_data = fc.sources
                if isinstance(sources_data, str) and (sources_data.startswith('[') or sources_data.startswith('{')):
                    try:
                        sources_list = json.loads(sources_data)
                        sources_str = '; '.join(sources_list) if isinstance(sources_list, list) else str(sources_list)
                    except Exception:
                        sources_str = sources_data
                elif isinstance(sources_data, list):
                    sources_str = '; '.join(sources_data)
                else:
                    sources_str = str(sources_data) if sources_data else ""

                writer.writerow([
                    fc.id,
                    fc.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    _sanitize_csv_cell(getattr(fc, 'source', 'factcheck')),
                    _sanitize_csv_cell(fc.source_id or ''),
                    _sanitize_csv_cell(fc.claim_text),
                    _sanitize_csv_cell(fc.verdict),
                    _sanitize_csv_cell(fc.explanation),
                    _sanitize_csv_cell(fc.fallacy or ''),
                    _sanitize_csv_cell(sources_str),
                ])

            # Create response
            output.seek(0)

            filename = f"authenix_export_{datetime.now(UTC).strftime('%Y%m%d')}.csv"
            if source_filter:
                filename = f"authenix_{source_filter}_{datetime.now(UTC).strftime('%Y%m%d')}.csv"

            response = make_response(output.getvalue())
            response.headers['Content-Type'] = 'text/csv'
            response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'

            logger.info(f"Exported {len(fact_checks)} fact checks as CSV for user_id={user.id}")
            return response

        else:
            raise APIError('Unsupported format. Use: csv', status_code=400)

    except APIError:
        raise
    except Exception as e:
        logger.error(f"Export error: {e}", exc_info=True)
        raise APIError('Failed to export data')


def _parse_bounded_export_json() -> dict:
    """Parse and validate a bounded (<= 256 KB) JSON object request body."""
    if request.content_length is not None and request.content_length > MAX_EXPORT_JSON_BYTES:
        raise APIError('Payload exceeds maximum allowed size of 256 KB', status_code=400)

    raw_body = request.get_data()
    if len(raw_body) > MAX_EXPORT_JSON_BYTES:
        raise APIError('Payload exceeds maximum allowed size of 256 KB', status_code=400)

    try:
        payload = json.loads(raw_body.decode('utf-8')) if raw_body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise APIError('Invalid JSON payload', status_code=400)

    if not isinstance(payload, dict):
        raise APIError('Request body must be a JSON object', status_code=400)
    return payload


def _require_company_and_summary(payload: dict) -> tuple[str, str]:
    """Validate required non-empty company_name and summary strings."""
    company_name = payload.get('company_name')
    summary = payload.get('summary')
    if (
        not isinstance(company_name, str)
        or not company_name.strip()
        or not isinstance(summary, str)
        or not summary.strip()
    ):
        raise APIError('company_name and summary are required', status_code=400)
    return company_name, summary


def _get_authorized_google_drive_connection(user_id: int) -> CloudStorageConnection:
    """Return the user's connected Google Drive row or raise 401 with needs_google_auth."""
    conn = (
        db_session.query(CloudStorageConnection)
        .filter_by(user_id=user_id, provider='google_drive')
        .first()
    )
    if conn is None or not (conn.encrypted_access_token or conn.encrypted_refresh_token):
        raise APIError(
            'Google Drive authorization required',
            status_code=401,
            payload={'needs_google_auth': True},
        )
    return conn


@export_bp.route('/pitchdeck/google-doc', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def export_pitchdeck_google_doc():
    """Render a pitch deck deal memo (Brief or Extended) and upload to Google Docs."""
    if not Config.is_folder_sync_enabled():
        raise APIError('Folder sync is coming soon', status_code=403)
    user = request.current_user
    user_id = user.id
    payload = _parse_bounded_export_json()

    raw_mode = payload.get('mode')
    mode = 'brief' if raw_mode is None else (raw_mode.strip().lower() if isinstance(raw_mode, str) else '')
    if mode not in VALID_MEMO_MODES:
        raise APIError('Invalid export mode. Must be "brief" or "extended".', status_code=400)

    company_name, _ = _require_company_and_summary(payload)

    requested_folder_id = payload.get('folder_id')
    if requested_folder_id is not None and requested_folder_id != '':
        if not isinstance(requested_folder_id, str) or not FOLDER_ID_RE.fullmatch(requested_folder_id.strip()):
            raise APIError('Invalid folder_id format', status_code=400)
        requested_folder_id = requested_folder_id.strip()
    else:
        requested_folder_id = None

    conn = _get_authorized_google_drive_connection(user_id)

    try:
        folder_id = ensure_google_briefs_folder(db_session, conn, requested_folder_id=requested_folder_id)
        html_body = render_deal_memo_html(payload, mode=mode)
        title = build_deal_memo_title(company_name, resolve_analyzed_date(payload))
        doc_id, doc_url = upload_google_doc_multipart(
            db_session,
            conn,
            title=title,
            html_body=html_body,
            folder_id=folder_id,
        )
    except CloudTokenError as exc:
        if exc.status_code != 401:
            raise APIError('Cloud storage authentication service unavailable', status_code=exc.status_code)
        raise APIError(
            'Google Drive authorization required',
            status_code=401,
            payload={'needs_google_auth': True},
        )
    except APIError:
        raise
    except Exception as exc:
        db_session.rollback()
        logger.error(f"Google Doc export failed (user_id={user_id}): {type(exc).__name__}")
        raise APIError('Failed to export deal memo to Google Docs', status_code=502)

    logger.info(f"Exported Google Doc deal memo (user_id={user_id}, mode={mode})")
    return jsonify({
        'success': True,
        'doc_id': doc_id,
        'doc_url': doc_url,
    })


@export_bp.route('/pitchdeck/google-sheet', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def export_pitchdeck_google_sheet():
    """Append a structured pitch deck evaluation row to a designated Google Sheet."""
    if not Config.is_folder_sync_enabled():
        raise APIError('Folder sync is coming soon', status_code=403)
    user = request.current_user
    user_id = user.id
    payload = _parse_bounded_export_json()

    raw_spreadsheet_id = payload.get('spreadsheet_id')
    if not isinstance(raw_spreadsheet_id, str) or not raw_spreadsheet_id.strip():
        raise APIError('spreadsheet_id is required', status_code=400)
    try:
        spreadsheet_id = parse_spreadsheet_id(raw_spreadsheet_id)
    except ValueError:
        raise APIError('Invalid Google Sheets spreadsheet ID', status_code=400)

    raw_sheet_name = payload.get('sheet_name')
    sheet_name = None
    if raw_sheet_name is not None and raw_sheet_name != '':
        if not isinstance(raw_sheet_name, str):
            raise APIError('Invalid Google Sheets sheet name', status_code=400)
        try:
            sheet_name = validate_sheet_name(raw_sheet_name)
        except ValueError:
            raise APIError('Invalid Google Sheets sheet name', status_code=400)

    _require_company_and_summary(payload)
    conn = _get_authorized_google_drive_connection(user_id)

    try:
        result = append_pitchdeck_to_sheet(
            db_session,
            conn,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            payload=payload,
        )
    except CloudTokenError as exc:
        if exc.status_code != 401:
            raise APIError('Cloud storage authentication service unavailable', status_code=exc.status_code)
        raise APIError(
            'Google Drive authorization required',
            status_code=401,
            payload={'needs_google_auth': True},
        )
    except APIError:
        raise
    except Exception as exc:
        db_session.rollback()
        logger.error(f"Google Sheet export failed (user_id={user_id}): {type(exc).__name__}")
        raise APIError('Failed to export deal evaluation to Google Sheets', status_code=502)

    logger.info(f"Exported Google Sheet pitch deck row (user_id={user_id})")
    return jsonify({
        'success': True,
        **result,
    })


@export_bp.route('/pitchdeck/cloud-sync', methods=['POST'])
@login_required
@limiter.limit("30 per hour")
def export_pitchdeck_cloud_sync():
    """Export the currently analyzed pitch deck Deal Memo (+ optional Google Sheet row) to the user's connected cloud provider."""
    if not Config.is_folder_sync_enabled():
        raise APIError('Folder sync is coming soon', status_code=403)
    user = request.current_user
    user_id = user.id
    payload = _parse_bounded_export_json()
    _require_company_and_summary(payload)

    requested_provider = payload.get('provider')
    if requested_provider is not None and requested_provider != '':
        if not isinstance(requested_provider, str) or requested_provider not in VALID_PROVIDERS:
            raise APIError('Unsupported storage provider', status_code=400)
        conn = (
            db_session.query(CloudStorageConnection)
            .filter_by(user_id=user_id, provider=requested_provider)
            .first()
        )
    else:
        rows = db_session.query(CloudStorageConnection).filter_by(user_id=user_id).all()
        conn = next(
            (r for r in rows if (r.encrypted_access_token or r.encrypted_refresh_token)),
            None,
        )

    if conn is None or not (conn.encrypted_access_token or conn.encrypted_refresh_token):
        raise APIError(
            'Cloud storage authorization required',
            status_code=401,
            payload={'needs_reauth': True},
        )

    raw_mode = payload.get('mode')
    if raw_mode is not None and raw_mode != '':
        mode = raw_mode.strip().lower() if isinstance(raw_mode, str) else ''
        if mode not in VALID_MEMO_MODES:
            raise APIError('Invalid export mode. Must be "brief" or "extended".', status_code=400)
    else:
        mode = conn.memo_preset if conn.memo_preset in VALID_MEMO_MODES else 'brief'

    provider_name = conn.provider
    storage_provider = get_storage_provider(provider_name)

    try:
        if provider_name == 'google_drive':
            folder_ref = ensure_google_briefs_folder(
                db_session,
                conn,
                requested_folder_id=conn.output_folder_ref or None,
            )
        else:
            if not conn.output_folder_ref:
                raise APIError(
                    f'Configure an Output Folder for {provider_name} in Cloud Sync Settings first.',
                    status_code=400,
                )
            folder_ref = conn.output_folder_ref

        uploaded = storage_provider.upload_memo(
            db_session,
            conn,
            output_folder_ref=folder_ref,
            analysis_result=payload,
            memo_preset=mode,
        )
        sheet_range = None
        sheet_url = None
        if conn.auto_export_sheets and conn.sheets_spreadsheet_id:
            try:
                sheet_range = auto_export_synced_deck_to_sheet(
                    db_session,
                    conn,
                    analysis_result=payload,
                    memo_url=uploaded.url,
                )
                if sheet_range:
                    clean_sid = parse_spreadsheet_id(conn.sheets_spreadsheet_id)
                    sheet_url = f'https://docs.google.com/spreadsheets/d/{clean_sid}'
            except Exception as sheet_exc:
                logger.warning(
                    f"Non-fatal Google Sheets auto-export failed on cloud-sync "
                    f"(user_id={user_id}, provider={provider_name}): {type(sheet_exc).__name__}"
                )
    except CloudTokenError as exc:
        if exc.status_code != 401:
            raise APIError('Cloud storage authentication service unavailable', status_code=exc.status_code)
        raise APIError(
            'Cloud storage authorization required',
            status_code=401,
            payload={'needs_reauth': True, 'provider': provider_name},
        )
    except ProviderFolderError:
        raise APIError('Configured output folder is invalid or inaccessible', status_code=400)
    except ProviderUpstreamError:
        raise APIError('Failed to export deal memo to cloud storage', status_code=502)
    except APIError:
        raise
    except Exception as exc:
        db_session.rollback()
        logger.error(f"Cloud Deal Memo export failed (user_id={user_id}, provider={provider_name}): {type(exc).__name__}")
        raise APIError('Failed to export deal memo to cloud storage', status_code=502)

    logger.info(f"Exported Deal Memo via cloud-sync (user_id={user_id}, provider={provider_name}, mode={mode})")
    return jsonify({
        'success': True,
        'provider': provider_name,
        'mode': mode,
        'memo_id': uploaded.external_id,
        'memo_url': uploaded.url,
        'updated_sheet_range': sheet_range,
        'sheet_url': sheet_url,
    })


