"""Encrypted cloud storage OAuth token lifecycle and revocation helpers.

Handles HKDF-SHA256 + Fernet encryption at rest, OAuth PKCE authorization URL
construction, form-encoded code exchange and token refresh via safe_http,
InvalidToken / invalid_grant recovery, and best-effort upstream revocation.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, UTC
import hashlib
import logging
import secrets
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from backend.config import Config
from backend.models import CloudStorageConnection, SyncedDeckRecord, record_audit_event
from backend.services.safe_http import safe_api_request

logger = logging.getLogger(__name__)

VALID_PROVIDERS = ('google_drive', 'dropbox', 'onedrive')
_HKDF_INFO = b'authenix-google-drive-token-v1'
_EXPIRY_SKEW_SECONDS = 60
_MAX_TOKEN_EXPIRY_SECONDS = 10 * 365 * 86400


class CloudTokenError(Exception):
    """Raised when stored credentials are missing, corrupted, or require re-auth."""

    def __init__(
        self,
        message: str,
        *,
        event_type: str = 'storage.reauth_required',
        status_code: int = 401,
    ):
        super().__init__(message)
        self.message = message
        self.event_type = event_type
        self.status_code = status_code


def _derive_fernet() -> Fernet:
    """Derive a Fernet instance dynamically from Config at call time."""
    secret = Config.GOOGLE_TOKEN_ENCRYPTION_KEY or Config.SECRET_KEY
    if not secret:
        raise ValueError('Token encryption key is not configured')
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    )
    key_bytes = hkdf.derive(secret.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(key_bytes))


def encrypt_token(plaintext: str) -> str:
    """Encrypt an OAuth token string using HKDF-SHA256 + Fernet."""
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError('Token plaintext must be a non-empty string')
    return _derive_fernet().encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a stored Fernet ciphertext; raises InvalidToken on key mismatch/corruption."""
    if not isinstance(ciphertext, str) or not ciphertext:
        raise InvalidToken('Empty encrypted token')
    return _derive_fernet().decrypt(ciphertext.encode('ascii')).decode('utf-8')


def normalize_utc(dt: datetime | None) -> datetime | None:
    """Attach UTC tzinfo when SQLite returns naive datetimes."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_provider_oauth_spec(provider: str) -> dict:
    """Return OAuth endpoints, credentials, and scopes for a supported provider."""
    if provider == 'google_drive':
        return {
            'client_id': Config.GOOGLE_CLIENT_ID,
            'client_secret': Config.GOOGLE_CLIENT_SECRET,
            'authorize_url': 'https://accounts.google.com/o/oauth2/v2/auth',
            'token_url': 'https://oauth2.googleapis.com/token',
            'revoke_url': 'https://oauth2.googleapis.com/revoke',
            'scopes': Config.get_google_drive_scopes(),
            'extra_authorize_params': {
                'access_type': 'offline',
                'prompt': 'consent',
                'include_granted_scopes': 'true',
            },
        }
    if provider == 'dropbox':
        return {
            'client_id': Config.DROPBOX_CLIENT_ID,
            'client_secret': Config.DROPBOX_CLIENT_SECRET,
            'authorize_url': 'https://www.dropbox.com/oauth2/authorize',
            'token_url': 'https://api.dropboxapi.com/oauth2/token',
            'revoke_url': 'https://api.dropboxapi.com/2/auth/token/revoke',
            'scopes': None,
            'extra_authorize_params': {
                'token_access_type': 'offline',
            },
        }
    if provider == 'onedrive':
        return {
            'client_id': Config.ONEDRIVE_CLIENT_ID,
            'client_secret': Config.ONEDRIVE_CLIENT_SECRET,
            'authorize_url': 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
            'token_url': 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
            'revoke_url': None,
            'scopes': 'Files.ReadWrite offline_access',
            'extra_authorize_params': {},
        }
    raise ValueError(f'Unsupported storage provider: {provider}')


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a high-entropy PKCE (code_verifier, S256 code_challenge) pair."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return code_verifier, code_challenge


def build_authorize_url(
    provider: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    """Build the provider's OAuth 2.0 authorization URL with CSRF state and PKCE S256."""
    spec = get_provider_oauth_spec(provider)
    params = {
        'client_id': spec['client_id'],
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'state': state,
        'code_challenge': code_challenge,
        'code_challenge_method': 'S256',
    }
    if spec['scopes']:
        params['scope'] = spec['scopes']
    params.update(spec['extra_authorize_params'])
    return f"{spec['authorize_url']}?{urlencode(params)}"


def exchange_oauth_code(
    provider: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict:
    """Exchange an authorization code for OAuth tokens using safe_api_request."""
    spec = get_provider_oauth_spec(provider)
    form_data = {
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri,
        'client_id': spec['client_id'] or '',
        'client_secret': spec['client_secret'] or '',
        'code_verifier': code_verifier,
    }
    resp = safe_api_request('POST', spec['token_url'], data=form_data, timeout=10)
    if resp.status_code != 200:
        raise ValueError(f'OAuth token exchange failed with status {resp.status_code}')
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get('access_token'):
        raise ValueError('OAuth token response missing access_token')
    return payload


def _compute_token_expiry(expires_in_raw) -> datetime:
    """Compute UTC expiry timestamp from expires_in seconds (default 3600s)."""
    try:
        seconds = int(expires_in_raw)
        if seconds <= 0:
            seconds = 3600
    except (TypeError, ValueError, OverflowError):
        seconds = 3600
    seconds = min(seconds, _MAX_TOKEN_EXPIRY_SECONDS)
    return datetime.now(UTC) + timedelta(seconds=seconds)


def upsert_connection_tokens(
    session_db,
    user_id: int,
    provider: str,
    token_payload: dict,
    *,
    default_scopes: str | None = None,
) -> CloudStorageConnection:
    """Create or update encrypted CloudStorageConnection tokens, preserving refresh token if omitted."""
    access_token = token_payload.get('access_token')
    if not access_token or not isinstance(access_token, str):
        raise ValueError('Token payload missing access_token')

    conn = (
        session_db.query(CloudStorageConnection)
        .filter_by(user_id=user_id, provider=provider)
        .first()
    )
    if conn is None:
        conn = CloudStorageConnection(
            user_id=user_id,
            provider=provider,
        )
        session_db.add(conn)

    conn.encrypted_access_token = encrypt_token(access_token)
    refresh_token = token_payload.get('refresh_token')
    if isinstance(refresh_token, str) and refresh_token:
        conn.encrypted_refresh_token = encrypt_token(refresh_token)

    conn.token_expiry = _compute_token_expiry(token_payload.get('expires_in'))
    granted_scope = token_payload.get('scope')
    if isinstance(granted_scope, str) and granted_scope.strip():
        conn.granted_scopes = granted_scope.strip()
    elif default_scopes and not conn.granted_scopes:
        conn.granted_scopes = default_scopes
    conn.last_error = None

    record_audit_event(
        session_db,
        'storage.connected',
        user_id=user_id,
        provider=provider,
        detail='oauth_connected',
    )
    session_db.commit()
    return conn


def delete_connection_and_records(session_db, conn: CloudStorageConnection, *, commit: bool = False) -> None:
    """Delete SyncedDeckRecord children before deleting the CloudStorageConnection."""
    session_db.query(SyncedDeckRecord).filter_by(
        connection_id=conn.id,
        user_id=conn.user_id,
    ).delete()
    session_db.delete(conn)
    if commit:
        session_db.commit()


def _purge_connection_on_auth_failure(
    session_db,
    conn: CloudStorageConnection,
    *,
    event_type: str,
    detail: str,
    message: str,
) -> CloudTokenError:
    """Purge unusable connection/records, write non-PII AuditLog, commit, and return error."""
    try:
        user_id = conn.user_id
        provider = conn.provider
        delete_connection_and_records(session_db, conn, commit=False)
        record_audit_event(
            session_db,
            event_type,
            user_id=user_id,
            provider=provider,
            detail=detail,
        )
        session_db.commit()
    except (StaleDataError, ObjectDeletedError):
        session_db.rollback()
    return CloudTokenError(message, event_type=event_type, status_code=401)


def refresh_connection_token(session_db, conn: CloudStorageConnection) -> str:
    """Refresh an expired/rejected access token; purge and raise CloudTokenError on failure."""
    try:
        _ = (conn.id, conn.user_id)
        encrypted_refresh_token = conn.encrypted_refresh_token
        provider = conn.provider
    except (StaleDataError, ObjectDeletedError) as exc:
        session_db.rollback()
        raise CloudTokenError(
            'Cloud storage authorization required',
            event_type='storage.reauth_required',
            status_code=401,
        ) from exc

    if not encrypted_refresh_token:
        raise _purge_connection_on_auth_failure(
            session_db,
            conn,
            event_type='storage.reauth_required',
            detail='missing_refresh_token',
            message='Cloud storage re-authorization required',
        )

    try:
        refresh_token = decrypt_token(encrypted_refresh_token)
    except InvalidToken:
        raise _purge_connection_on_auth_failure(
            session_db,
            conn,
            event_type='storage.invalid_token',
            detail='invalid_refresh_token',
            message='Stored cloud credentials could not be decrypted; re-authorization required',
        )

    spec = get_provider_oauth_spec(provider)
    form_data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'client_id': spec['client_id'] or '',
        'client_secret': spec['client_secret'] or '',
    }
    resp = safe_api_request('POST', spec['token_url'], data=form_data, timeout=10)

    if resp.status_code in (400, 401):
        raise _purge_connection_on_auth_failure(
            session_db,
            conn,
            event_type='storage.reauth_required',
            detail='refresh_rejected',
            message='Cloud storage authorization expired or revoked',
        )
    if resp.status_code != 200:
        raise CloudTokenError(
            'Cloud storage token refresh failed',
            event_type='storage.reauth_required',
            status_code=502,
        )

    try:
        payload = resp.json()
    except ValueError:
        payload = None

    new_access_token = payload.get('access_token') if isinstance(payload, dict) else None
    if not isinstance(new_access_token, str) or not new_access_token:
        raise _purge_connection_on_auth_failure(
            session_db,
            conn,
            event_type='storage.reauth_required',
            detail='missing_access_token_on_refresh',
            message='Cloud storage authorization expired or revoked',
        )

    try:
        conn.encrypted_access_token = encrypt_token(new_access_token)
        new_refresh = payload.get('refresh_token')
        if isinstance(new_refresh, str) and new_refresh:
            conn.encrypted_refresh_token = encrypt_token(new_refresh)
        conn.token_expiry = _compute_token_expiry(payload.get('expires_in'))
        conn.last_error = None
        session_db.commit()
    except (StaleDataError, ObjectDeletedError) as exc:
        session_db.rollback()
        raise CloudTokenError(
            'Cloud storage authorization required',
            event_type='storage.reauth_required',
            status_code=401,
        ) from exc
    return new_access_token


def get_valid_access_token(session_db, conn: CloudStorageConnection) -> str:
    """Return a valid plaintext access token, decrypting or proactively refreshing as needed."""
    try:
        _ = (conn.id, conn.user_id, conn.provider)
        encrypted_access_token = conn.encrypted_access_token
        token_expiry = conn.token_expiry
    except (StaleDataError, ObjectDeletedError) as exc:
        session_db.rollback()
        raise CloudTokenError(
            'Cloud storage authorization required',
            event_type='storage.reauth_required',
            status_code=401,
        ) from exc

    if not encrypted_access_token:
        return refresh_connection_token(session_db, conn)

    try:
        access_token = decrypt_token(encrypted_access_token)
    except InvalidToken:
        raise _purge_connection_on_auth_failure(
            session_db,
            conn,
            event_type='storage.invalid_token',
            detail='invalid_access_token',
            message='Stored cloud credentials could not be decrypted; re-authorization required',
        )

    expiry_utc = normalize_utc(token_expiry)
    if expiry_utc is not None and expiry_utc <= datetime.now(UTC) + timedelta(seconds=_EXPIRY_SKEW_SECONDS):
        return refresh_connection_token(session_db, conn)

    return access_token


def execute_with_token_refresh(session_db, conn: CloudStorageConnection, call_fn):
    """Execute an upstream HTTP call with single-retry 401 token refresh."""
    token = get_valid_access_token(session_db, conn)
    resp = call_fn(token)
    if resp.status_code != 401:
        return resp

    try:
        resp.close()
    except Exception:
        pass

    new_token = refresh_connection_token(session_db, conn)
    retry_resp = call_fn(new_token)
    if retry_resp.status_code == 401:
        try:
            retry_resp.close()
        except Exception:
            pass
        raise _purge_connection_on_auth_failure(
            session_db,
            conn,
            event_type='storage.reauth_required',
            detail='rejected_after_refresh',
            message='Cloud storage authorization rejected after token refresh',
        )
    return retry_resp


def extract_revoke_targets(conn: CloudStorageConnection) -> dict | None:
    """Safely extract plaintext tokens for best-effort revocation before row deletion."""
    if conn is None:
        return None
    access_token = None
    refresh_token = None
    if conn.encrypted_access_token:
        try:
            access_token = decrypt_token(conn.encrypted_access_token)
        except InvalidToken:
            access_token = None
    if conn.encrypted_refresh_token:
        try:
            refresh_token = decrypt_token(conn.encrypted_refresh_token)
        except InvalidToken:
            refresh_token = None
    if not access_token and not refresh_token:
        return None
    return {
        'provider': conn.provider,
        'access_token': access_token,
        'refresh_token': refresh_token,
    }


def revoke_tokens_best_effort(
    provider: str,
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
) -> None:
    """Revoke provider OAuth tokens best-effort with timeout=5; never raises."""
    if provider == 'onedrive':
        return
    try:
        if provider == 'google_drive':
            token = refresh_token or access_token
            if not token:
                return
            safe_api_request(
                'POST',
                'https://oauth2.googleapis.com/revoke',
                data={'token': token},
                timeout=5,
            )
        elif provider == 'dropbox':
            token = access_token or refresh_token
            if not token:
                return
            safe_api_request(
                'POST',
                'https://api.dropboxapi.com/2/auth/token/revoke',
                headers={'Authorization': f'Bearer {token}'},
                timeout=5,
            )
    except Exception as exc:
        logger.warning(f"Best-effort token revocation failed for provider={provider}: {type(exc).__name__}")
