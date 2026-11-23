"""OneDrive Microsoft Graph storage provider implementation."""
from __future__ import annotations

import re
from urllib.parse import quote

import requests

from backend.services.cloud_tokens import execute_with_token_refresh
from backend.services.deal_memo_renderer import (
    build_deal_memo_filename,
    render_deal_memo_markdown,
    resolve_analyzed_date,
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

ONEDRIVE_ID_RE = re.compile(r'^[A-Za-z0-9_!.-]{3,128}$')
_ONEDRIVE_FILE_ID_RE = re.compile(r'^[A-Za-z0-9_!.-]{1,128}$')
_MAX_LIST_PAGES = 20


class OneDriveProvider(StorageProvider):
    """OneDrive implementation of StorageProvider."""

    provider_name = 'onedrive'

    def normalize_folder_ref(self, raw_ref: str) -> str:
        if not isinstance(raw_ref, str):
            raise ProviderFolderError('OneDrive folder ID must be a string')
        cleaned = raw_ref.strip()
        if not cleaned or '..' in cleaned or not ONEDRIVE_ID_RE.fullmatch(cleaned):
            raise ProviderFolderError('Invalid OneDrive folder ID')
        return cleaned

    def validate_folder(self, session_db, conn, folder_ref: str) -> str:
        canonical = self.normalize_folder_ref(folder_ref)
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, fid=canonical: safe_api_request(
                    'GET',
                    f'https://graph.microsoft.com/v1.0/me/drive/items/{fid}',
                    headers={'Authorization': f'Bearer {tok}'},
                    timeout=10,
                ),
            )
        except requests.RequestException as exc:
            raise ProviderUpstreamError('OneDrive folder validation request failed') from exc

        if resp.status_code in (400, 403, 404):
            raise ProviderFolderError('OneDrive folder not found or inaccessible')
        if resp.status_code != 200:
            raise ProviderUpstreamError('OneDrive folder validation failed')

        data = parse_json_dict(resp, error_message='Invalid OneDrive folder metadata response')
        if 'folder' not in data:
            raise ProviderFolderError('Target OneDrive item is not a folder')
        return canonical

    def _map_items(self, raw_items: list) -> list[RemoteFileItem]:
        mapped: list[RemoteFileItem] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            file_id = str(entry.get('id') or '').strip()
            name = str(entry.get('name') or '').strip()
            if not file_id or not name:
                continue
            raw_size = entry.get('size')
            size: int | None = None
            if isinstance(raw_size, int) and not isinstance(raw_size, bool):
                size = raw_size
            etag = str(entry.get('eTag') or entry.get('lastModifiedDateTime') or '')
            file_meta = entry.get('file') if isinstance(entry.get('file'), dict) else {}
            mime_type = file_meta.get('mimeType')
            mapped.append(
                RemoteFileItem(
                    file_id=file_id,
                    name=name,
                    mime_type=str(mime_type) if mime_type is not None else None,
                    size=size,
                    etag_or_modified=etag,
                    is_folder=('folder' in entry),
                )
            )
        return mapped

    def list_files(self, session_db, conn, folder_ref: str) -> list[RemoteFileItem]:
        canonical = self.normalize_folder_ref(folder_ref)
        items: list[RemoteFileItem] = []
        next_url: str | None = f'https://graph.microsoft.com/v1.0/me/drive/items/{canonical}/children'

        for page_idx in range(_MAX_LIST_PAGES):
            if not next_url:
                break
            if not next_url.startswith('https://graph.microsoft.com/'):
                break
            try:
                resp = execute_with_token_refresh(
                    session_db,
                    conn,
                    lambda tok, target=next_url: safe_api_request(
                        'GET',
                        target,
                        headers={'Authorization': f'Bearer {tok}'},
                        timeout=15,
                    ),
                )
            except requests.RequestException as exc:
                raise ProviderUpstreamError('OneDrive folder listing request failed') from exc

            if resp.status_code in (400, 403, 404) and page_idx == 0:
                raise ProviderFolderError('OneDrive folder not found or inaccessible')
            if resp.status_code != 200:
                raise ProviderUpstreamError('Failed to list OneDrive folder')

            data = parse_json_dict(resp, error_message='Invalid OneDrive listing response')
            raw_values = data.get('value')
            if not isinstance(raw_values, list):
                raise ProviderUpstreamError('Invalid OneDrive listing response')
            items.extend(self._map_items(raw_values))

            candidate_next = data.get('@odata.nextLink')
            next_url = candidate_next if isinstance(candidate_next, str) else None

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
        if '..' in (item.file_id or '') or not _ONEDRIVE_FILE_ID_RE.fullmatch(item.file_id or ''):
            raise ProviderFolderError('Invalid OneDrive file ID')
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, fid=item.file_id: safe_api_request(
                    'GET',
                    f'https://graph.microsoft.com/v1.0/me/drive/items/{fid}/content',
                    headers={'Authorization': f'Bearer {tok}'},
                    timeout=(10, 30),
                    stream=True,
                ),
            )
            return read_bounded_response(resp, max_bytes=max_bytes, total_timeout=total_timeout)
        except ResponseTooLargeError:
            raise
        except requests.RequestException as exc:
            raise ProviderUpstreamError('OneDrive file download failed') from exc

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
        filename = build_deal_memo_filename(company_name, resolve_analyzed_date(analysis_result))
        md_text = render_deal_memo_markdown(analysis_result, mode=memo_preset)

        encoded_name = quote(filename, safe='')
        url = f'https://graph.microsoft.com/v1.0/me/drive/items/{canonical_out}:/{encoded_name}:/content'
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, target=url, payload=md_text.encode('utf-8'): safe_api_request(
                    'PUT',
                    target,
                    headers={
                        'Authorization': f'Bearer {tok}',
                        'Content-Type': 'text/markdown; charset=utf-8',
                    },
                    data=payload,
                    timeout=15,
                ),
            )
        except requests.RequestException as exc:
            raise ProviderUpstreamError('OneDrive memo upload request failed') from exc

        if resp.status_code not in (200, 201):
            raise ProviderUpstreamError('OneDrive memo upload failed')

        data = parse_json_dict(resp, error_message='Invalid OneDrive upload response')
        return UploadedMemoResult(
            external_id=str(data.get('id') or filename),
            url=str(data.get('webUrl') or ''),
        )

    def _ensure_subfolder(
        self,
        session_db,
        conn,
        parent_id: str,
        subfolder: str,
    ) -> str | None:
        resp = execute_with_token_refresh(
            session_db,
            conn,
            lambda tok, pid=parent_id, sf=subfolder: safe_api_request(
                'POST',
                f'https://graph.microsoft.com/v1.0/me/drive/items/{pid}/children',
                headers={'Authorization': f'Bearer {tok}'},
                json_body={
                    'name': sf,
                    'folder': {},
                    '@microsoft.graph.conflictBehavior': 'replace',
                },
                timeout=10,
            ),
        )
        if resp.status_code not in (200, 201):
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        folder_id = data.get('id') if isinstance(data, dict) else None
        return str(folder_id) if folder_id else None

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
        if '..' in (item.file_id or '') or not _ONEDRIVE_FILE_ID_RE.fullmatch(item.file_id or ''):
            return None
        new_name = build_prefixed_filename(item.name, prefix)
        patch_body: dict[str, object] = {'name': new_name}
        if input_folder_ref:
            canonical_in = self.normalize_folder_ref(input_folder_ref)
            sub_id = self._ensure_subfolder(session_db, conn, canonical_in, subfolder)
            if sub_id:
                patch_body['parentReference'] = {'id': sub_id}

        resp = execute_with_token_refresh(
            session_db,
            conn,
            lambda tok, fid=item.file_id, body=patch_body: safe_api_request(
                'PATCH',
                f'https://graph.microsoft.com/v1.0/me/drive/items/{fid}',
                headers={'Authorization': f'Bearer {tok}'},
                json_body=body,
                timeout=10,
            ),
        )
        if resp.status_code != 200:
            return None
        return f'{subfolder}/{new_name}'
