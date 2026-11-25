"""Cloud storage OAuth routes for Google Drive, Dropbox, and OneDrive."""
from __future__ import annotations

import hmac
import logging
import re
import secrets

from flask import Blueprint, jsonify, redirect, request, session, url_for
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from backend.config import Config
from backend.database import db_session
from backend.error_handlers import APIError
from backend.extensions import limiter
from backend.models import CloudStorageConnection, record_audit_event
from backend.routes.auth import login_required, scheduler_secret_required
from backend.services.cloud_tokens import (
    CloudTokenError,
    VALID_PROVIDERS,
    build_authorize_url,
    delete_connection_and_records,
    exchange_oauth_code,
    extract_revoke_targets,
    generate_pkce_pair,
    get_provider_oauth_spec,
    get_valid_access_token,
    revoke_tokens_best_effort,
    upsert_connection_tokens,
)
from backend.services.deal_memo_renderer import VALID_MEMO_MODES, ensure_google_briefs_folder
from backend.services.folder_sync_service import (
    run_daily_watched_folders_sync,
    sync_connection,
)
from backend.services.google_sheets_service import (
    parse_spreadsheet_id,
    validate_sheet_name,
)
from backend.services.storage_providers import (
    ProviderFolderError,
    ProviderUpstreamError,
    get_storage_provider,
)

logger = logging.getLogger(__name__)

storage_oauth_bp = Blueprint('storage_oauth', __name__, url_prefix='/api/storage')
cron_bp = Blueprint('cron', __name__, url_prefix='/api/cron')


def _require_valid_provider(provider: str) -> None:
    """Validate provider name against allowlist."""
    if provider not in VALID_PROVIDERS:
        raise APIError('Unsupported storage provider', status_code=400)


def _require_folder_sync_enabled() -> None:
    """Reject cloud folder sync / OAuth requests when folder sync is omitted."""
    if not Config.is_folder_sync_enabled():
        raise APIError('Folder sync is coming soon', status_code=403)


def _build_callback_uri(provider: str) -> str:
    """Build OAuth redirect URI using CLOUD_OAUTH_REDIRECT_BASE or request host."""
    base = (Config.CLOUD_OAUTH_REDIRECT_BASE or '').strip().rstrip('/')
    if base:
        return f'{base}/api/storage/{provider}/callback'
    return url_for('storage_oauth.oauth_callback', provider=provider, _external=True)


@storage_oauth_bp.route('/connections', methods=['GET'])
@login_required
@limiter.limit("60 per minute")
def list_connections():
    """Return status for all supported cloud storage providers for the current user."""
    if not Config.is_folder_sync_enabled():
        return jsonify({
            'success': True,
            'enabled': False,
            'connections': [],
        })
    user = request.current_user
    rows = db_session.query(CloudStorageConnection).filter_by(user_id=user.id).all()
    by_provider = {row.provider: row for row in rows}
    connections = [
        by_provider[prov].to_status_dict()
        if prov in by_provider
        else CloudStorageConnection.default_status_dict(prov)
        for prov in VALID_PROVIDERS
    ]
    return jsonify({
        'success': True,
        'enabled': True,
        'connections': connections,
    })


@storage_oauth_bp.route('/<provider>/connect', methods=['GET'])
@login_required
@limiter.limit("20 per minute")
def oauth_connect(provider: str):
    """Initiate incremental cloud storage OAuth 2.0 + PKCE flow."""
    _require_folder_sync_enabled()
    _require_valid_provider(provider)
    spec = get_provider_oauth_spec(provider)
    if not spec['client_id'] or not spec['client_secret']:
        raise APIError('OAuth credentials are not configured for this provider', status_code=400)

    user = request.current_user
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce_pair()
    session[f'storage_oauth_{provider}'] = {
        'state': state,
        'code_verifier': code_verifier,
        'user_id': user.id,
    }

    redirect_uri = _build_callback_uri(provider)
    authorize_url = build_authorize_url(provider, redirect_uri, state, code_challenge)
    logger.info(f"Initiating storage OAuth connect (user_id={user.id}, provider={provider})")
    return redirect(authorize_url)


@storage_oauth_bp.route('/<provider>/callback', methods=['GET'])
@login_required
@limiter.limit("20 per minute")
def oauth_callback(provider: str):
    """Handle cloud storage OAuth 2.0 callback without mutating User or session JWT."""
    _require_folder_sync_enabled()
    _require_valid_provider(provider)
    user = request.current_user
    stored = session.pop(f'storage_oauth_{provider}', None)

    oauth_error = request.args.get('error')
    if oauth_error:
        safe_err = re.sub(r'[^a-zA-Z0-9_.-]', '_', oauth_error)[:64]
        logger.warning(f"Storage OAuth callback error={safe_err} (user_id={user.id}, provider={provider})")
        if oauth_error == 'access_denied':
            return redirect('/?storage_error=access_denied')
        return redirect('/?storage_error=oauth_failed')

    code = request.args.get('code')
    state = request.args.get('state')
    if (
        not isinstance(code, str)
        or not code
        or not isinstance(state, str)
        or not state
        or not isinstance(stored, dict)
        or stored.get('user_id') != user.id
        or not isinstance(stored.get('state'), str)
        or not hmac.compare_digest(state.encode('utf-8'), stored['state'].encode('utf-8'))
        or not isinstance(stored.get('code_verifier'), str)
    ):
        logger.warning(f"Storage OAuth callback invalid state/code (user_id={user.id}, provider={provider})")
        return redirect('/?storage_error=invalid_state')

    redirect_uri = _build_callback_uri(provider)
    try:
        token_payload = exchange_oauth_code(provider, code, redirect_uri, stored['code_verifier'])
        spec = get_provider_oauth_spec(provider)
        upsert_connection_tokens(
            db_session,
            user.id,
            provider,
            token_payload,
            default_scopes=spec.get('scopes'),
        )
    except Exception as exc:
        logger.warning(
            f"Storage OAuth token exchange failed (user_id={user.id}, provider={provider}): {type(exc).__name__}"
        )
        return redirect('/?storage_error=oauth_failed')

    logger.info(f"Storage OAuth connected (user_id={user.id}, provider={provider})")
    return redirect(f'/?storage_connected={provider}')


@storage_oauth_bp.route('/<provider>/status', methods=['GET'])
@login_required
@limiter.limit("60 per minute")
def connection_status(provider: str):
    """Return connection and sync configuration status for one provider."""
    _require_folder_sync_enabled()
    _require_valid_provider(provider)
    user = request.current_user
    conn = (
        db_session.query(CloudStorageConnection)
        .filter_by(user_id=user.id, provider=provider)
        .first()
    )
    if conn is None:
        return jsonify(CloudStorageConnection.default_status_dict(provider))
    return jsonify(conn.to_status_dict())


@storage_oauth_bp.route('/<provider>/disconnect', methods=['POST'])
@login_required
@limiter.limit("20 per minute")
def disconnect_provider(provider: str):
    """Best-effort revoke tokens upstream and delete connection + sync records."""
    _require_folder_sync_enabled()
    _require_valid_provider(provider)
    user = request.current_user
    conn = (
        db_session.query(CloudStorageConnection)
        .filter_by(user_id=user.id, provider=provider)
        .first()
    )

    if conn is not None:
        revoke_target = extract_revoke_targets(conn)
        delete_connection_and_records(db_session, conn, commit=False)
        record_audit_event(
            db_session,
            'storage.disconnected',
            user_id=user.id,
            provider=provider,
            detail='user_disconnect',
        )
        db_session.commit()

        if revoke_target:
            revoke_tokens_best_effort(
                provider,
                access_token=revoke_target.get('access_token'),
                refresh_token=revoke_target.get('refresh_token'),
            )
        logger.info(f"Storage OAuth disconnected (user_id={user.id}, provider={provider})")

    return jsonify({
        'success': True,
        'provider': provider,
        'connected': False,
    })


@storage_oauth_bp.route('/<provider>/picker-config', methods=['GET'])
@login_required
@limiter.limit("30 per minute")
def get_picker_config(provider: str):
    """Return Google Picker API key, GCP project number (app_id), and valid OAuth access token."""
    _require_folder_sync_enabled()
    _require_valid_provider(provider)
    if provider != 'google_drive':
        raise APIError('Google Picker is only supported for Google Drive', status_code=400)

    user = request.current_user
    conn = (
        db_session.query(CloudStorageConnection)
        .filter_by(user_id=user.id, provider='google_drive')
        .first()
    )
    if conn is None or not (conn.encrypted_access_token or conn.encrypted_refresh_token):
        raise APIError(
            'Cloud storage connection not found',
            status_code=401,
            payload={'needs_reauth': True},
        )

    api_key = (Config.GOOGLE_PICKER_API_KEY or '').strip()
    app_id = Config.get_google_picker_app_id()
    if not api_key or not app_id:
        raise APIError('Google Picker is not configured on this server', status_code=503)

    try:
        access_token = get_valid_access_token(db_session, conn)
    except CloudTokenError as exc:
        if exc.status_code == 401:
            raise APIError(
                'Cloud storage re-authorization required',
                status_code=401,
                payload={'needs_reauth': True},
            )
        raise APIError('Cloud storage authentication unavailable', status_code=exc.status_code)

    return jsonify({
        'success': True,
        'api_key': api_key,
        'app_id': app_id,
        'access_token': access_token,
    }), 200


@storage_oauth_bp.route('/<provider>/config', methods=['PUT'])
@login_required
@limiter.limit("30 per minute")
def update_storage_config(provider: str):
    """Validate and save watched input/output folders, memo preset, daily sync, and Sheets config."""
    _require_folder_sync_enabled()
    _require_valid_provider(provider)
    user = request.current_user
    conn = (
        db_session.query(CloudStorageConnection)
        .filter_by(user_id=user.id, provider=provider)
        .first()
    )
    if conn is None or not (conn.encrypted_access_token or conn.encrypted_refresh_token):
        raise APIError(
            'Cloud storage connection not found',
            status_code=401,
            payload={'needs_reauth': True},
        )

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError('Request body must be a JSON object', status_code=400)

    raw_input = data['input_folder_ref'] if 'input_folder_ref' in data else conn.input_folder_ref
    raw_output = data['output_folder_ref'] if 'output_folder_ref' in data else conn.output_folder_ref
    has_input = isinstance(raw_input, str) and bool(raw_input.strip())
    has_output = isinstance(raw_output, str) and bool(raw_output.strip())

    if provider != 'google_drive' and (not has_input or not has_output):
        raise APIError('Both input_folder_ref and output_folder_ref are required', status_code=400)

    storage_provider = get_storage_provider(provider)
    try:
        norm_input = storage_provider.normalize_folder_ref(raw_input) if has_input else None
        norm_output = storage_provider.normalize_folder_ref(raw_output) if has_output else None
        same_folder = (
            norm_input is not None
            and norm_output is not None
            and storage_provider.folders_equal(norm_input, norm_output)
        )
    except ProviderFolderError:
        raise APIError('Invalid folder reference', status_code=400)

    if same_folder:
        raise APIError('Input and output folders must be distinct', status_code=400)

    if 'memo_preset' in data and data['memo_preset'] is not None:
        if not isinstance(data['memo_preset'], str):
            raise APIError('memo_preset must be brief or extended', status_code=400)
        memo_preset = data['memo_preset'].strip().lower()
        if memo_preset not in VALID_MEMO_MODES:
            raise APIError('memo_preset must be brief or extended', status_code=400)
    else:
        memo_preset = conn.memo_preset or 'brief'

    if 'daily_sync_enabled' in data:
        if not isinstance(data['daily_sync_enabled'], bool):
            raise APIError('daily_sync_enabled must be a boolean', status_code=400)
        daily_sync_enabled = data['daily_sync_enabled']
    else:
        daily_sync_enabled = bool(conn.daily_sync_enabled)

    if 'auto_export_sheets' in data:
        if not isinstance(data['auto_export_sheets'], bool):
            raise APIError('auto_export_sheets must be a boolean', status_code=400)
        auto_export_sheets = data['auto_export_sheets']
    else:
        auto_export_sheets = bool(conn.auto_export_sheets)

    if 'sheets_spreadsheet_id' in data:
        raw_sheet_id = data['sheets_spreadsheet_id']
        if raw_sheet_id is None or raw_sheet_id == '':
            sheets_spreadsheet_id = None
        elif not isinstance(raw_sheet_id, str):
            raise APIError('Invalid Google Sheets spreadsheet ID', status_code=400)
        else:
            try:
                sheets_spreadsheet_id = parse_spreadsheet_id(raw_sheet_id)
            except ValueError:
                raise APIError('Invalid Google Sheets spreadsheet ID', status_code=400)
    else:
        sheets_spreadsheet_id = conn.sheets_spreadsheet_id

    if 'sheets_sheet_name' in data:
        raw_sheet_name = data['sheets_sheet_name']
        if raw_sheet_name is None or raw_sheet_name == '':
            sheets_sheet_name = None
        elif not isinstance(raw_sheet_name, str):
            raise APIError('Invalid Google Sheets sheet name', status_code=400)
        else:
            try:
                sheets_sheet_name = validate_sheet_name(raw_sheet_name)
            except ValueError:
                raise APIError('Invalid Google Sheets sheet name', status_code=400)
    else:
        sheets_sheet_name = conn.sheets_sheet_name

    try:
        validated_input = (
            storage_provider.validate_folder(db_session, conn, norm_input)
            if norm_input is not None
            else None
        )
        if norm_output is not None:
            validated_output = storage_provider.validate_folder(db_session, conn, norm_output)
        else:
            if 'output_folder_ref' in data:
                conn.output_folder_ref = None
            validated_output = ensure_google_briefs_folder(db_session, conn)
        if (
            validated_input is not None
            and validated_output is not None
            and storage_provider.folders_equal(validated_input, validated_output)
        ):
            raise APIError('Input and output folders must be distinct', status_code=400)
    except ProviderFolderError:
        if provider == 'google_drive':
            raise APIError(
                'Google Drive folder not found or inaccessible. With drive.file access, select the folder using Google Picker or choose an Authenix-created folder.',
                status_code=400,
            )
        raise APIError('Configured folder was not found or is trashed', status_code=400)
    except ProviderUpstreamError:
        raise APIError('Cloud storage provider unavailable', status_code=502)
    except CloudTokenError as exc:
        if exc.status_code == 401:
            raise APIError(
                'Cloud storage re-authorization required',
                status_code=401,
                payload={'needs_reauth': True},
            )
        raise APIError('Cloud storage authentication unavailable', status_code=exc.status_code)
    except RuntimeError:
        raise APIError('Cloud storage provider unavailable', status_code=502)

    try:
        conn.input_folder_ref = validated_input
        conn.output_folder_ref = validated_output
        conn.memo_preset = memo_preset
        conn.daily_sync_enabled = daily_sync_enabled
        conn.auto_export_sheets = auto_export_sheets
        conn.sheets_spreadsheet_id = sheets_spreadsheet_id
        conn.sheets_sheet_name = sheets_sheet_name
        conn.last_error = None
        db_session.commit()
    except (StaleDataError, ObjectDeletedError):
        db_session.rollback()
        raise APIError(
            'Cloud storage re-authorization required',
            status_code=401,
            payload={'needs_reauth': True},
        )

    logger.info(f"Updated storage sync config (user_id={user.id}, provider={provider})")
    return jsonify({
        'success': True,
        **conn.to_status_dict(),
    }), 200


@storage_oauth_bp.route('/<provider>/sync', methods=['POST'])
@login_required
@limiter.limit("60 per hour")
def trigger_manual_sync(provider: str):
    """Trigger manual on-demand folder sync ('Sync now') or explicit file_ids sync for the current user."""
    _require_folder_sync_enabled()
    _require_valid_provider(provider)
    user = request.current_user
    conn = (
        db_session.query(CloudStorageConnection)
        .filter_by(user_id=user.id, provider=provider)
        .first()
    )
    if conn is None or not (conn.encrypted_access_token or conn.encrypted_refresh_token):
        raise APIError(
            'Cloud storage connection not found',
            status_code=401,
            payload={'needs_reauth': True},
        )

    body = request.get_json(silent=True)
    file_ids: list[str] | None = None
    if isinstance(body, dict) and 'file_ids' in body and body['file_ids'] is not None:
        raw_ids = body['file_ids']
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 25 or not all(
            isinstance(fid, str) and fid.strip() for fid in raw_ids
        ):
            raise APIError('file_ids must be a non-empty list of up to 25 file ID strings', status_code=400)
        file_ids = [fid.strip() for fid in raw_ids]

    try:
        summary = sync_connection(db_session, conn, file_ids=file_ids)
    except ProviderFolderError:
        raise APIError('Configured sync folders are invalid or missing', status_code=400)
    except ProviderUpstreamError:
        raise APIError('Cloud storage provider unavailable', status_code=502)
    except CloudTokenError as exc:
        if exc.status_code == 401:
            raise APIError(
                'Cloud storage re-authorization required',
                status_code=401,
                payload={'needs_reauth': True},
            )
        raise APIError('Cloud storage authentication unavailable', status_code=exc.status_code)

    logger.info(
        f"Manual storage sync finished (user_id={user.id}, provider={provider}, "
        f"processed={summary['processed']}, errors={summary['errors']})"
    )
    return jsonify(summary), 200


@cron_bp.route('/sync-watched-folders', methods=['POST'])
@scheduler_secret_required
def sync_watched_folders():
    """Cloud Scheduler endpoint to sync all daily-sync-enabled cloud connections."""
    if not Config.is_folder_sync_enabled():
        return jsonify({
            'success': True,
            'enabled': False,
            'connections_checked': 0,
            'processed': 0,
            'skipped_duplicate': 0,
            'skipped_invalid': 0,
            'skipped_insufficient_cp': 0,
            'errors': 0,
        }), 200
    summary = run_daily_watched_folders_sync(db_session)
    logger.info(
        f"Daily watched folder cron completed (connections={summary['connections_checked']}, "
        f"processed={summary['processed']}, errors={summary['errors']})"
    )
    return jsonify(summary), 200
