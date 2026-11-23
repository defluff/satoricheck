"""Abstract interface and shared types for cloud storage providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

_ALLOWED_PDF_MIME_TYPES = frozenset({
    'application/pdf',
    'application/x-pdf',
    'application/octet-stream',
})


class ProviderFolderError(ValueError):
    """Raised when a configured folder reference is malformed, missing, trashed, or not a folder."""


class ProviderUpstreamError(RuntimeError):
    """Raised on transient upstream HTTP/network errors without leaking raw response bodies."""


@dataclass(frozen=True)
class RemoteFileItem:
    """Normalized metadata for a remote file or folder entry."""

    file_id: str
    name: str
    mime_type: str | None
    size: int | None
    etag_or_modified: str
    is_folder: bool = False
    path_display: str | None = None


@dataclass(frozen=True)
class UploadedMemoResult:
    """Normalized metadata for an uploaded deal memo."""

    external_id: str
    url: str


def is_candidate_pdf(item: RemoteFileItem) -> bool:
    """Return True if the remote item is a non-folder PDF candidate for sync pre-flight."""
    if item.is_folder:
        return False
    if not item.name or not item.name.lower().endswith('.pdf'):
        return False
    if item.mime_type:
        mime = item.mime_type.split(';', 1)[0].strip().lower()
        if mime and mime not in _ALLOWED_PDF_MIME_TYPES:
            return False
    return True


def build_prefixed_filename(name: str, prefix: str) -> str:
    """Prefix a filename idempotently (e.g., '[Analyzed] deck.pdf'), preserving extension."""
    clean_name = (name or 'deck.pdf').strip()
    for known in ('[Analyzed] ', '[Skipped - Invalid] '):
        if clean_name.startswith(known):
            clean_name = clean_name[len(known):].lstrip()
    return f'{prefix}{clean_name}'[:250]


def parse_json_dict(resp, *, error_message: str) -> dict:
    """Parse a JSON object response safely, raising ProviderUpstreamError if invalid."""
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderUpstreamError(error_message) from exc
    if not isinstance(data, dict):
        raise ProviderUpstreamError(error_message)
    return data


class StorageProvider(ABC):
    """Unified interface for cloud storage folder validation, listing, download, and memo upload."""

    provider_name: str

    @abstractmethod
    def normalize_folder_ref(self, raw_ref: str) -> str:
        """Normalize and syntactically validate a user-supplied folder reference or URL."""

    def folders_equal(self, input_ref: str, output_ref: str) -> bool:
        """Return True when two folder references resolve to the same canonical folder."""
        return self.normalize_folder_ref(input_ref) == self.normalize_folder_ref(output_ref)

    @abstractmethod
    def validate_folder(self, session_db, conn, folder_ref: str) -> str:
        """Verify live folder existence and non-trashed folder type upstream."""

    @abstractmethod
    def list_files(self, session_db, conn, folder_ref: str) -> list[RemoteFileItem]:
        """List immediate children inside `folder_ref`."""

    @abstractmethod
    def download_file(
        self,
        session_db,
        conn,
        item: RemoteFileItem,
        *,
        max_bytes: int,
        total_timeout: float = 60.0,
    ) -> bytes:
        """Stream-download `item` into memory with strict `max_bytes` and `total_timeout` bounds."""

    @abstractmethod
    def upload_memo(
        self,
        session_db,
        conn,
        *,
        output_folder_ref: str,
        analysis_result: dict,
        memo_preset: str,
    ) -> UploadedMemoResult:
        """Render and upload a deal memo into `output_folder_ref`."""

    def fetch_files_by_ids(
        self,
        session_db,
        conn,
        file_ids: list[str],
    ) -> list[RemoteFileItem]:
        """Fetch metadata for explicit file IDs (e.g. selected via Google Picker under drive.file)."""
        raise ProviderFolderError(f'{self.provider_name} does not support explicit file_ids sync')

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
        """Best-effort rename and move a processed/skipped file into `<input_folder_ref>/<subfolder>/`."""
        return None
