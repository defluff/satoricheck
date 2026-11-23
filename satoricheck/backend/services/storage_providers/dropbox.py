"""Dropbox storage provider implementation using safe_api_request & token refresh."""
from __future__ import annotations

import json
from urllib.parse import unquote, urlparse

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

_MAX_LIST_PAGES = 20


class DropboxProvider(StorageProvider):
    """Dropbox implementation of StorageProvider."""

    provider_name = 'dropbox'

    def normalize_folder_ref(self, raw_ref: str) -> str:
        if not isinstance(raw_ref, str):
            raise ProviderFolderError('Dropbox folder path must be a string')
        cleaned = raw_ref.strip()
        if not cleaned or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
            raise ProviderFolderError('Invalid Dropbox folder path')

        if '://' in cleaned:
            try:
                parsed = urlparse(cleaned)
            except ValueError as exc:
                raise ProviderFolderError('Invalid Dropbox folder path') from exc
            if parsed.scheme != 'https' or (parsed.netloc or '').lower() not in (
                'www.dropbox.com',
                'www.dropbox.com:443',
            ):
                raise ProviderFolderError('Invalid Dropbox folder URL')
            url_path = unquote(parsed.path or '')
            if not url_path.startswith('/home/'):
                raise ProviderFolderError('Invalid Dropbox folder URL path')
            cleaned = url_path[len('/home'):]

        if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
            raise ProviderFolderError('Invalid Dropbox folder path')

        cleaned = '/' + cleaned.strip('/')
        segments = [seg for seg in cleaned.split('/') if seg]
        if not segments or '..' in segments or '.' in segments:
            raise ProviderFolderError('Invalid Dropbox folder path')
        canonical = '/' + '/'.join(segments)
        if len(canonical) > 255:
            raise ProviderFolderError('Dropbox folder path too long')
        return canonical

    def folders_equal(self, input_ref: str, output_ref: str) -> bool:
        return self.normalize_folder_ref(input_ref).casefold() == self.normalize_folder_ref(output_ref).casefold()

    def validate_folder(self, session_db, conn, folder_ref: str) -> str:
        canonical = self.normalize_folder_ref(folder_ref)
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, path=canonical: safe_api_request(
                    'POST',
                    'https://api.dropboxapi.com/2/files/get_metadata',
                    headers={'Authorization': f'Bearer {tok}'},
                    json_body={'path': path},
                    timeout=10,
                ),
            )
        except requests.RequestException as exc:
            raise ProviderUpstreamError('Dropbox folder validation request failed') from exc

        if resp.status_code in (400, 403, 404, 409):
            raise ProviderFolderError('Dropbox folder not found or inaccessible')
        if resp.status_code != 200:
            raise ProviderUpstreamError('Dropbox folder validation failed')

        data = parse_json_dict(resp, error_message='Invalid Dropbox folder metadata response')
        if data.get('.tag') != 'folder':
            raise ProviderFolderError('Target Dropbox item is not a folder')
        return canonical

    def _map_entries(self, entries: list) -> list[RemoteFileItem]:
        mapped: list[RemoteFileItem] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            tag = entry.get('.tag')
            name = str(entry.get('name') or '').strip()
            path_display = entry.get('path_display') or entry.get('path_lower')
            file_id = str(entry.get('id') or path_display or '').strip()
            if not file_id or not name:
                continue
            raw_size = entry.get('size')
            size: int | None = None
            if isinstance(raw_size, int) and not isinstance(raw_size, bool):
                size = raw_size
            etag = str(entry.get('rev') or entry.get('content_hash') or entry.get('server_modified') or '')
            mapped.append(
                RemoteFileItem(
                    file_id=file_id,
                    name=name,
                    mime_type=None,
                    size=size,
                    etag_or_modified=etag,
                    is_folder=(tag == 'folder'),
                    path_display=str(path_display) if path_display else None,
                )
            )
        return mapped

    def list_files(self, session_db, conn, folder_ref: str) -> list[RemoteFileItem]:
        canonical = self.normalize_folder_ref(folder_ref)
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, path=canonical: safe_api_request(
                    'POST',
                    'https://api.dropboxapi.com/2/files/list_folder',
                    headers={'Authorization': f'Bearer {tok}'},
                    json_body={'path': path, 'recursive': False, 'include_deleted': False},
                    timeout=15,
                ),
            )
        except requests.RequestException as exc:
            raise ProviderUpstreamError('Dropbox folder listing request failed') from exc

        if resp.status_code in (400, 403, 404, 409):
            raise ProviderFolderError('Dropbox folder not found or inaccessible')
        if resp.status_code != 200:
            raise ProviderUpstreamError('Failed to list Dropbox folder')

        data = parse_json_dict(resp, error_message='Invalid Dropbox listing response')
        entries = data.get('entries')
        if not isinstance(entries, list):
            raise ProviderUpstreamError('Invalid Dropbox listing response')
        items = self._map_entries(entries)

        pages = 1
        while data.get('has_more') and data.get('cursor') and pages < _MAX_LIST_PAGES:
            cursor = str(data['cursor'])
            try:
                cont_resp = execute_with_token_refresh(
                    session_db,
                    conn,
                    lambda tok, c=cursor: safe_api_request(
                        'POST',
                        'https://api.dropboxapi.com/2/files/list_folder/continue',
                        headers={'Authorization': f'Bearer {tok}'},
                        json_body={'cursor': c},
                        timeout=15,
                    ),
                )
            except requests.RequestException as exc:
                raise ProviderUpstreamError('Dropbox folder pagination failed') from exc

            if cont_resp.status_code != 200:
                raise ProviderUpstreamError('Failed to continue Dropbox folder listing')
            data = parse_json_dict(cont_resp, error_message='Invalid Dropbox pagination response')
            more_entries = data.get('entries')
            if isinstance(more_entries, list):
                items.extend(self._map_entries(more_entries))
            pages += 1

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
        target_path = item.path_display or item.file_id
        if not target_path or any(ord(ch) < 32 or ord(ch) == 127 for ch in target_path):
            raise ProviderFolderError('Invalid Dropbox file path')
        api_arg = json.dumps({'path': target_path})
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, arg=api_arg: safe_api_request(
                    'POST',
                    'https://content.dropboxapi.com/2/files/download',
                    headers={
                        'Authorization': f'Bearer {tok}',
                        'Dropbox-API-Arg': arg,
                    },
                    timeout=(10, 30),
                    stream=True,
                ),
            )
            return read_bounded_response(resp, max_bytes=max_bytes, total_timeout=total_timeout)
        except ResponseTooLargeError:
            raise
        except requests.RequestException as exc:
            raise ProviderUpstreamError('Dropbox file download failed') from exc

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

        dest_path = f"{canonical_out.rstrip('/')}/{filename}"
        api_arg = json.dumps({
            'path': dest_path,
            'mode': 'add',
            'autorename': True,
        })
        try:
            resp = execute_with_token_refresh(
                session_db,
                conn,
                lambda tok, arg=api_arg, payload=md_text.encode('utf-8'): safe_api_request(
                    'POST',
                    'https://content.dropboxapi.com/2/files/upload',
                    headers={
                        'Authorization': f'Bearer {tok}',
                        'Content-Type': 'application/octet-stream',
                        'Dropbox-API-Arg': arg,
                    },
                    data=payload,
                    timeout=15,
                ),
            )
        except requests.RequestException as exc:
            raise ProviderUpstreamError('Dropbox memo upload request failed') from exc

        if resp.status_code not in (200, 201):
            raise ProviderUpstreamError('Dropbox memo upload failed')

        data = parse_json_dict(resp, error_message='Invalid Dropbox upload response')
        returned_path = str(data.get('path_display') or dest_path)
        ext_id = str(data.get('id') or dest_path)
        return UploadedMemoResult(
            external_id=ext_id,
            url=f'https://www.dropbox.com/home{returned_path}',
        )

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
        if not input_folder_ref:
            return None
        from_path = item.path_display
        if not from_path or not from_path.startswith('/'):
            canonical_in = self.normalize_folder_ref(input_folder_ref)
            from_path = f"{canonical_in.rstrip('/')}/{item.name}"
        canonical_in = self.normalize_folder_ref(input_folder_ref)
        new_name = build_prefixed_filename(item.name, prefix)
        to_path = f"{canonical_in.rstrip('/')}/{subfolder}/{new_name}"
        resp = execute_with_token_refresh(
            session_db,
            conn,
            lambda tok, fp=from_path, tp=to_path: safe_api_request(
                'POST',
                'https://api.dropboxapi.com/2/files/move_v2',
                headers={'Authorization': f'Bearer {tok}'},
                json_body={
                    'from_path': fp,
                    'to_path': tp,
                    'autorename': True,
                },
                timeout=10,
            ),
        )
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return to_path
        meta = data.get('metadata') if isinstance(data, dict) else None
        if isinstance(meta, dict) and meta.get('path_display'):
            return str(meta['path_display'])
        return to_path
