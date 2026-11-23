"""Cloud storage provider registry and shared exports."""
from __future__ import annotations

from backend.services.storage_providers.base import (
    ProviderFolderError,
    ProviderUpstreamError,
    RemoteFileItem,
    StorageProvider,
    UploadedMemoResult,
    build_prefixed_filename,
    is_candidate_pdf,
)
from backend.services.storage_providers.dropbox import DropboxProvider
from backend.services.storage_providers.google_drive import GoogleDriveProvider
from backend.services.storage_providers.onedrive import OneDriveProvider

_PROVIDERS: dict[str, StorageProvider] = {
    'google_drive': GoogleDriveProvider(),
    'dropbox': DropboxProvider(),
    'onedrive': OneDriveProvider(),
}


def get_storage_provider(provider: str) -> StorageProvider:
    """Return the StorageProvider implementation for `provider`."""
    impl = _PROVIDERS.get(provider)
    if impl is None:
        raise ValueError(f'Unsupported storage provider: {provider}')
    return impl


__all__ = [
    'DropboxProvider',
    'GoogleDriveProvider',
    'OneDriveProvider',
    'ProviderFolderError',
    'ProviderUpstreamError',
    'RemoteFileItem',
    'StorageProvider',
    'UploadedMemoResult',
    'build_prefixed_filename',
    'get_storage_provider',
    'is_candidate_pdf',
]
