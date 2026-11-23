"""Google Drive storage provider implementation using safe_api_request & token refresh."""
from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

from backend.services.cloud_tokens import execute_with_token_refresh
from backend.services.deal_memo_renderer import (
    FOLDER_ID_RE,
    build_deal_memo_title,
    render_deal_memo_html,
    resolve_analyzed_date,
    upload_google_doc_multipart,
)
from backend.services.safe_http import ResponseTooLargeError, read_bounded_response, safe_api_request
from backend.services.storage_providers.base import (
    ProviderFolderError,
    ProviderUpstreamError,
    RemoteFileItem,
    StorageProvider,
    UploadedMemoResult,
    build_prefixed_filename,
    parse_json_dict,
)

_DRIVE_FOLDER_URL_PATH_RE = re.compile(r'^/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]{3,128})/?$')
_DRIVE_FILE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_MAX_LIST_PAGES = 20


class GoogleDriveProvider(StorageProvider):
    """Google Drive implementation of StorageProvider."""

    provider_name = 'google_drive'

    def normalize_folder_ref(self, raw_ref: str) -> str:
        if not isinstance(raw_ref, str):
            raise ProviderFolderError('Google Drive folder reference must be a string')
        cleaned = raw_ref.strip()
        if not cleaned or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
            raise ProviderFolderError('Invalid Google Drive folder reference')

        if '://' in cleaned or cleaned.startswith('drive.google.com/'):
            candidate = cleaned if '://' in cleaned else f'https://{cleaned}'
            try:
                parsed = urlparse(candidate)
            except ValueError as exc:
                raise ProviderFolderError('Invalid Google Drive folder reference') from exc
            if parsed.scheme != 'https' or (parsed.netloc or '').lower() not in (
                'drive.google.com',
                'drive.google.com:443',
            ):
                raise ProviderFolderError('Invalid Google Drive folder URL')
            match = _DRIVE_FOLDER_URL_PATH_RE.fullmatch(parsed.path or '')
            if not match:
                raise ProviderFolderError('Invalid Google Drive folder URL path')
            folder_id = match.group(1)
        else:
            folder_id = cleaned

        if not FOLDER_ID_RE.fullmatch(folder_id):
            raise ProviderFolderError('Invalid Google Drive folder ID')
        return folder_id

    def validate_folder(self, session_db, conn, folder_ref: str) -> str:
        canonical = self.normalize_folder_ref(folder_ref)
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, fid=canonical: safe_api_request(
                    'GET',
                    f'https://www.googleapis.com/drive/v3/files/{fid}',
                    headers={'Authorization': f'Bearer {tok}'},
                    params={'fields': 'id,name,mimeType,trashed'},
                    timeout=10,
                ),
            )
        except requests.RequestException as exc:
            raise ProviderUpstreamError('Google Drive folder validation request failed') from exc

        if resp.status_code in (400, 403, 404):
            raise ProviderFolderError('Google Drive folder not found or inaccessible')
        if resp.status_code != 200:
            raise ProviderUpstreamError('Google Drive folder validation failed')

        data = parse_json_dict(resp, error_message='Invalid Google Drive folder metadata response')
        if data.get('trashed') is True:
            raise ProviderFolderError('Google Drive folder is trashed')
        mime_type = data.get('mimeType')
        if mime_type is not None and mime_type != 'application/vnd.google-apps.folder':
            raise ProviderFolderError('Target Google Drive item is not a folder')
        return canonical

    def list_files(self, session_db, conn, folder_ref: str) -> list[RemoteFileItem]:
        canonical = self.normalize_folder_ref(folder_ref)
        items: list[RemoteFileItem] = []
        page_token: str | None = None

        for _ in range(_MAX_LIST_PAGES):
            params: dict[str, object] = {
                'q': f"'{canonical}' in parents and trashed = false",
                'fields': 'nextPageToken,files(id,name,mimeType,size,modifiedTime,md5Checksum)',
                'pageSize': 100,
            }
            if page_token:
                params['pageToken'] = page_token

            try:
                resp = execute_with_token_refresh(
                    session_db,
                    conn,
                    lambda tok, p=params: safe_api_request(
                        'GET',
                        'https://www.googleapis.com/drive/v3/files',
                        headers={'Authorization': f'Bearer {tok}'},
                        params=p,
                        timeout=15,
                    ),
                )
            except requests.RequestException as exc:
                raise ProviderUpstreamError('Google Drive folder listing request failed') from exc

            if resp.status_code in (400, 403, 404):
                raise ProviderFolderError('Google Drive folder not found or inaccessible')
            if resp.status_code != 200:
                raise ProviderUpstreamError('Failed to list Google Drive folder')

            data = parse_json_dict(resp, error_message='Invalid Google Drive listing response')
            raw_files = data.get('files')
            if not isinstance(raw_files, list):
                raise ProviderUpstreamError('Invalid Google Drive listing response')

            for entry in raw_files:
                if not isinstance(entry, dict):
                    continue
                file_id = str(entry.get('id') or '').strip()
                name = str(entry.get('name') or '').strip()
                if not file_id or not name:
                    continue
                mime_type = entry.get('mimeType')
                raw_size = entry.get('size')
                size: int | None = None
                if raw_size is not None and not isinstance(raw_size, bool):
                    size_str = str(raw_size).strip()
                    if size_str.isascii() and size_str.isdigit():
                        size = int(size_str)
                etag = str(entry.get('md5Checksum') or entry.get('modifiedTime') or '')
                items.append(
                    RemoteFileItem(
                        file_id=file_id,
                        name=name,
                        mime_type=str(mime_type) if mime_type is not None else None,
                        size=size,
                        etag_or_modified=etag,
                        is_folder=(mime_type == 'application/vnd.google-apps.folder'),
                    )
                )

            next_token = data.get('nextPageToken')
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token

        return items

    def download_file(
        self,
        session_db,
        conn,
        item: RemoteFileItem,
        *,
        max_bytes: int,
        total_timeout: float = 60.0,
    ) -> bytes:
        if not _DRIVE_FILE_ID_RE.fullmatch(item.file_id or ''):
            raise ProviderFolderError('Invalid Google Drive file ID')
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, fid=item.file_id: safe_api_request(
                    'GET',
                    f'https://www.googleapis.com/drive/v3/files/{fid}',
                    headers={'Authorization': f'Bearer {tok}'},
                    params={'alt': 'media'},
                    timeout=(10, 30),
                    stream=True,
                ),
            )
            return read_bounded_response(resp, max_bytes=max_bytes, total_timeout=total_timeout)
        except ResponseTooLargeError:
            raise
        except requests.RequestException as exc:
            raise ProviderUpstreamError('Google Drive file download failed') from exc

    def upload_memo(
        self,
        session_db,
        conn,
        *,
        output_folder_ref: str,
        analysis_result: dict,
        memo_preset: str,
    ) -> UploadedMemoResult:
        canonical_out = self.normalize_folder_ref(output_folder_ref)
        company_name = str(analysis_result.get('company_name') or 'Unknown Company')
        title = build_deal_memo_title(company_name, resolve_analyzed_date(analysis_result))
        html_body = render_deal_memo_html(analysis_result, mode=memo_preset)
        doc_id, doc_url = upload_google_doc_multipart(
            session_db,
            conn,
            title=title,
            html_body=html_body,
            folder_id=canonical_out,
        )
        return UploadedMemoResult(external_id=doc_id, url=doc_url)

    def fetch_files_by_ids(
        self,
        session_db,
        conn,
        file_ids: list[str],
    ) -> list[RemoteFileItem]:
        items: list[RemoteFileItem] = []
        seen: set[str] = set()
        for raw_id in file_ids:
            if not isinstance(raw_id, str):
                raise ProviderFolderError('Invalid Google Drive file ID')
            fid = raw_id.strip()
            if not fid or not _DRIVE_FILE_ID_RE.fullmatch(fid):
                raise ProviderFolderError('Invalid Google Drive file ID')
            if fid in seen:
                continue
            seen.add(fid)
            try:
                resp = execute_with_token_refresh(
                    session_db,
                    conn,
                    lambda tok, target_id=fid: safe_api_request(
                        'GET',
                        f'https://www.googleapis.com/drive/v3/files/{target_id}',
                        headers={'Authorization': f'Bearer {tok}'},
                        params={'fields': 'id,name,mimeType,size,modifiedTime,md5Checksum,trashed'},
                        timeout=10,
                    ),
                )
            except requests.RequestException as exc:
                raise ProviderUpstreamError('Google Drive file metadata request failed') from exc

            if resp.status_code in (400, 403, 404):
                raise ProviderFolderError('Selected Google Drive file not found or inaccessible')
            if resp.status_code != 200:
                raise ProviderUpstreamError('Failed to fetch Google Drive file metadata')

            entry = parse_json_dict(resp, error_message='Invalid Google Drive file metadata response')
            if entry.get('trashed') is True:
                continue
            name = str(entry.get('name') or fid).strip()
            mime_type = entry.get('mimeType')
            raw_size = entry.get('size')
            size: int | None = None
            if raw_size is not None and not isinstance(raw_size, bool):
                size_str = str(raw_size).strip()
                if size_str.isascii() and size_str.isdigit():
                    size = int(size_str)
            etag = str(entry.get('md5Checksum') or entry.get('modifiedTime') or 'v1')
            items.append(
                RemoteFileItem(
                    file_id=fid,
                    name=name,
                    mime_type=str(mime_type) if mime_type is not None else None,
                    size=size,
                    etag_or_modified=etag,
                    is_folder=(mime_type == 'application/vnd.google-apps.folder'),
                )
            )
        return items

    def archive_processed_file(
        self,
        session_db,
        conn,
        item: RemoteFileItem,
        *,
        input_folder_ref: str | None,
        subfolder: str,
        prefix: str,
    ) -> str | None:
        if not _DRIVE_FILE_ID_RE.fullmatch(item.file_id or ''):
            return None
        new_name = build_prefixed_filename(item.name, prefix)
        resp = execute_with_token_refresh(
            session_db,
            conn,
            lambda tok, fid=item.file_id, nm=new_name: safe_api_request(
                'PATCH',
                f'https://www.googleapis.com/drive/v3/files/{fid}',
                headers={'Authorization': f'Bearer {tok}'},
                json_body={'name': nm},
                params={'fields': 'id,name'},
                timeout=10,
            ),
        )
        if resp.status_code != 200:
            return None
        return new_name
