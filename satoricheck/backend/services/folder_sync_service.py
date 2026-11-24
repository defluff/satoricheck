"""Multi-provider watched folder sync and CP billing engine."""
from __future__ import annotations

from datetime import datetime, UTC
import logging
import math

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from backend.error_handlers import APIError
from backend.models import CloudStorageConnection, SyncedDeckRecord, TokenBalance
from backend.routes.tokens import (
    refund_flat_tokens,
    reserve_flat_tokens,
    trigger_post_action_refill_if_empty,
)
from backend.services import get_pitchdeck_service
from backend.services.cloud_tokens import CloudTokenError
from backend.services.safe_http import ResponseTooLargeError
from backend.services.storage_providers import (
    ProviderFolderError,
    ProviderUpstreamError,
    RemoteFileItem,
    get_storage_provider,
    is_candidate_pdf,
)

logger = logging.getLogger(__name__)

MAX_SYNC_FILES_PER_RUN = 5


def calculate_sync_cp(page_count: int) -> int:
    """Compute total Check Points (analysis CP [2..12] + 1 CP sync/upload fee)."""
    from backend.services.gemini.pitchdeck import GeminiServicePitchdeck
    effective_pages = min(max(1, page_count), GeminiServicePitchdeck.MAX_SLIDES_STANDARD)
    analysis_cp = max(2, math.ceil(effective_pages / 10) * 2)
    return analysis_cp + 1


def _get_user_balance_value(session_db, user_id: int) -> int:
    """Return persisted TokenBalance.balance for user_id without auto-creating a row."""
    bal = session_db.query(TokenBalance).filter_by(user_id=user_id).first()
    return int(bal.balance) if bal is not None else 0


def _connection_exists(session_db, conn_id: int) -> bool:
    """Query DB directly (bypassing identity_map staleness) to confirm connection still exists."""
    return (
        session_db.query(CloudStorageConnection.id)
        .filter_by(id=conn_id)
        .first()
        is not None
    )


def _build_summary_dict(
    item: RemoteFileItem,
    *,
    status: str,
    cp_charged: int = 0,
    exported_doc_url: str | None = None,
    exported_sheet_range: str | None = None,
    error_message: str | None = None,
) -> dict:
    """Build an API sync record summary dict without requiring a persisted ORM row."""
    return {
        'external_file_id': item.file_id[:255],
        'file_name': item.name[:255],
        'status': status,
        'cp_charged': cp_charged,
        'exported_doc_url': exported_doc_url[:512] if exported_doc_url else None,
        'exported_sheet_range': exported_sheet_range[:128] if exported_sheet_range else None,
        'error_message': error_message[:255] if error_message else None,
    }


def _upsert_synced_record(
    session_db,
    *,
    user_id: int,
    connection_id: int,
    item: RemoteFileItem,
    existing: SyncedDeckRecord | None = None,
    existing_id: int | None = None,
    status: str,
    cp_charged: int = 0,
    exported_doc_url: str | None = None,
    exported_sheet_range: str | None = None,
    error_message: str | None = None,
) -> SyncedDeckRecord | None:
    """Create or update a SyncedDeckRecord in place; return None if parent connection was deleted."""
    if not _connection_exists(session_db, connection_id):
        return None

    now = datetime.now(UTC)
    safe_file_id = item.file_id[:255]
    safe_file_name = item.name[:255]
    safe_etag = item.etag_or_modified[:255]
    safe_doc_url = exported_doc_url[:512] if exported_doc_url else None
    safe_sheet_range = exported_sheet_range[:128] if exported_sheet_range else None
    safe_error = error_message[:255] if error_message else None

    target_existing_id = (
        existing_id
        if existing_id is not None
        else (existing.id if existing is not None else None)
    )
    live_existing = None
    if target_existing_id is not None:
        live_existing = (
            session_db.query(SyncedDeckRecord)
            .filter_by(id=target_existing_id)
            .populate_existing()
            .first()
        )

    if live_existing is not None:
        live_existing.file_name = safe_file_name
        live_existing.status = status
        live_existing.cp_charged = cp_charged
        live_existing.exported_doc_url = safe_doc_url
        live_existing.exported_sheet_range = safe_sheet_range
        live_existing.error_message = safe_error
        live_existing.processed_at = now
        rec = live_existing
    else:
        rec = SyncedDeckRecord(
            user_id=user_id,
            connection_id=connection_id,
            external_file_id=safe_file_id,
            file_name=safe_file_name,
            file_etag_or_modified=safe_etag,
            status=status,
            exported_doc_url=safe_doc_url,
            exported_sheet_range=safe_sheet_range,
            cp_charged=cp_charged,
            error_message=safe_error,
            processed_at=now,
        )
        session_db.add(rec)

    try:
        session_db.commit()
        return rec
    except IntegrityError:
        session_db.rollback()
        if not _connection_exists(session_db, connection_id):
            return None
        raise


def _record_to_summary(rec: SyncedDeckRecord) -> dict:
    """Serialize a SyncedDeckRecord for API sync responses."""
    return {
        'external_file_id': rec.external_file_id,
        'file_name': rec.file_name,
        'status': rec.status,
        'cp_charged': rec.cp_charged,
        'exported_doc_url': rec.exported_doc_url,
        'exported_sheet_range': rec.exported_sheet_range,
        'error_message': rec.error_message,
    }


def _try_auto_export_sheets(
    session_db,
    conn: CloudStorageConnection,
    *,
    analysis_result: dict,
    memo_url: str,
) -> str | None:
    """Non-fatal M4 Google Sheets auto-export hook; never rolls back a completed memo."""
    user_id = None
    provider = None
    try:
        user_id = conn.user_id
        provider = conn.provider
        if not conn.auto_export_sheets or not conn.sheets_spreadsheet_id:
            return None
        from backend.services.google_sheets_service import auto_export_synced_deck_to_sheet
        return auto_export_synced_deck_to_sheet(
            session_db,
            conn,
            analysis_result=analysis_result,
            memo_url=memo_url,
        )
    except Exception as exc:
        logger.warning(
            f"Non-fatal Google Sheets auto-export failed "
            f"(user_id={user_id}, provider={provider}): {type(exc).__name__}"
        )
        return None


def _try_archive_processed_file(
    session_db,
    conn: CloudStorageConnection,
    provider,
    item: RemoteFileItem,
    *,
    input_folder_ref: str | None,
    subfolder: str,
    prefix: str,
) -> str | None:
    """Non-fatal best-effort rename/move of a processed or skipped PDF into Done/ or Skipped/."""
    user_id = None
    prov_name = None
    try:
        user_id = conn.user_id
        prov_name = conn.provider
        return provider.archive_processed_file(
            session_db,
            conn,
            item,
            input_folder_ref=input_folder_ref,
            subfolder=subfolder,
            prefix=prefix,
        )
    except Exception as exc:
        logger.warning(
            f"Non-fatal inbox archive move failed "
            f"(user_id={user_id}, provider={prov_name}, subfolder={subfolder}): {type(exc).__name__}"
        )
        return None


def sync_connection(
    session_db,
    conn: CloudStorageConnection,
    *,
    max_files: int | None = None,
    file_ids: list[str] | None = None,
) -> dict:
    """Sync new/retryable PDF pitch decks for a single CloudStorageConnection."""
    conn_id = conn.id
    conn_user_id = conn.user_id
    conn_provider = conn.provider
    input_folder_ref = conn.input_folder_ref
    output_folder_ref = conn.output_folder_ref
    memo_preset = conn.memo_preset or 'brief'

    if not (conn.encrypted_access_token or conn.encrypted_refresh_token):
        raise CloudTokenError('Cloud storage connection requires authorization', status_code=401)
    if file_ids is not None:
        if not output_folder_ref and conn_provider == 'google_drive':
            from backend.services.deal_memo_renderer import ensure_google_briefs_folder
            output_folder_ref = ensure_google_briefs_folder(session_db, conn)
        elif not output_folder_ref:
            raise ProviderFolderError('Output folder must be configured')
    elif not input_folder_ref or not output_folder_ref:
        raise ProviderFolderError('Input and output folders must be configured')

    provider = get_storage_provider(conn_provider)

    try:
        if file_ids is None and input_folder_ref:
            provider.validate_folder(session_db, conn, input_folder_ref)
        provider.validate_folder(session_db, conn, output_folder_ref)
    except ProviderFolderError:
        if _connection_exists(session_db, conn_id):
            live_conn = (
                session_db.query(CloudStorageConnection)
                .filter_by(id=conn_id)
                .populate_existing()
                .first()
            )
            if live_conn is not None:
                live_conn.last_error = 'Configured folder is invalid or inaccessible'
                session_db.commit()
        raise

    try:
        if file_ids is not None:
            items = provider.fetch_files_by_ids(session_db, conn, file_ids)
        else:
            items = provider.list_files(session_db, conn, input_folder_ref)
    except ProviderUpstreamError as exc:
        logger.warning(
            f"Folder listing failed (user_id={conn_user_id}, provider={conn_provider}): {type(exc).__name__}"
        )
        if _connection_exists(session_db, conn_id):
            live_conn = (
                session_db.query(CloudStorageConnection)
                .filter_by(id=conn_id)
                .populate_existing()
                .first()
            )
            if live_conn is not None:
                live_conn.last_error = 'Failed to list input folder'
                live_conn.last_synced_at = datetime.now(UTC)
                session_db.commit()
        return {
            'success': True,
            'provider': conn_provider,
            'processed': 0,
            'skipped_duplicate': 0,
            'skipped_invalid': 0,
            'skipped_insufficient_cp': 0,
            'errors': 1,
            'new_balance': _get_user_balance_value(session_db, conn_user_id),
            'records': [],
        }

    pdf_items = [item for item in items if is_candidate_pdf(item)]
    pitchdeck_svc = get_pitchdeck_service()

    processed = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    skipped_insufficient_cp = 0
    errors = 0
    candidate_count = 0
    records_summary: list[dict] = []

    for item in pdf_items:
        existing = (
            session_db.query(SyncedDeckRecord)
            .filter_by(
                connection_id=conn_id,
                external_file_id=item.file_id[:255],
                file_etag_or_modified=item.etag_or_modified[:255],
            )
            .first()
        )
        if existing is not None and existing.status in ('completed', 'skipped_invalid'):
            skipped_duplicate += 1
            continue
        existing_id = existing.id if existing is not None else None

        if max_files is not None and candidate_count >= max_files:
            break
        candidate_count += 1

        if item.size is not None and (item.size == 0 or item.size > pitchdeck_svc.MAX_FILE_SIZE):
            err_msg = 'File is empty or exceeds 25MB limit'
            rec = _upsert_synced_record(
                session_db,
                user_id=conn_user_id,
                connection_id=conn_id,
                item=item,
                existing_id=existing_id,
                status='skipped_invalid',
                cp_charged=0,
                error_message=err_msg,
            )
            _try_archive_processed_file(
                session_db,
                conn,
                provider,
                item,
                input_folder_ref=input_folder_ref,
                subfolder='Skipped',
                prefix='[Skipped - Invalid] ',
            )
            skipped_invalid += 1
            if rec is None:
                records_summary.append(_build_summary_dict(item, status='skipped_invalid', cp_charged=0, error_message=err_msg))
                break
            records_summary.append(_record_to_summary(rec))
            continue

        pdf_bytes: bytes | None = None
        try:
            try:
                pdf_bytes = provider.download_file(
                    session_db,
                    conn,
                    item,
                    max_bytes=pitchdeck_svc.MAX_FILE_SIZE,
                )
            except ResponseTooLargeError:
                err_msg = 'File exceeds 25MB limit'
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='skipped_invalid',
                    cp_charged=0,
                    error_message=err_msg,
                )
                _try_archive_processed_file(
                    session_db,
                    conn,
                    provider,
                    item,
                    input_folder_ref=input_folder_ref,
                    subfolder='Skipped',
                    prefix='[Skipped - Invalid] ',
                )
                skipped_invalid += 1
                if rec is None:
                    records_summary.append(_build_summary_dict(item, status='skipped_invalid', cp_charged=0, error_message=err_msg))
                    break
                records_summary.append(_record_to_summary(rec))
                continue
            except ProviderUpstreamError as exc:
                logger.warning(
                    f"Download failed (user_id={conn_user_id}, provider={conn_provider}, "
                    f"file_id={item.file_id}): {type(exc).__name__}"
                )
                err_msg = 'Failed to download file from provider'
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='error',
                    cp_charged=0,
                    error_message=err_msg,
                )
                errors += 1
                if rec is None:
                    records_summary.append(_build_summary_dict(item, status='error', cp_charged=0, error_message=err_msg))
                    break
                records_summary.append(_record_to_summary(rec))
                continue

            if not pdf_bytes or not pitchdeck_svc._is_valid_pdf(pdf_bytes):
                err_msg = 'Invalid PDF file'
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='skipped_invalid',
                    cp_charged=0,
                    error_message=err_msg,
                )
                _try_archive_processed_file(
                    session_db,
                    conn,
                    provider,
                    item,
                    input_folder_ref=input_folder_ref,
                    subfolder='Skipped',
                    prefix='[Skipped - Invalid] ',
                )
                skipped_invalid += 1
                if rec is None:
                    records_summary.append(_build_summary_dict(item, status='skipped_invalid', cp_charged=0, error_message=err_msg))
                    break
                records_summary.append(_record_to_summary(rec))
                continue

            page_count = pitchdeck_svc._estimate_page_count(pdf_bytes)
            if page_count > pitchdeck_svc.MAX_SLIDES_HARDCAP:
                err_msg = 'Deck exceeds 100 slide limit'
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='skipped_invalid',
                    cp_charged=0,
                    error_message=err_msg,
                )
                _try_archive_processed_file(
                    session_db,
                    conn,
                    provider,
                    item,
                    input_folder_ref=input_folder_ref,
                    subfolder='Skipped',
                    prefix='[Skipped - Invalid] ',
                )
                skipped_invalid += 1
                if rec is None:
                    records_summary.append(_build_summary_dict(item, status='skipped_invalid', cp_charged=0, error_message=err_msg))
                    break
                records_summary.append(_record_to_summary(rec))
                continue

            total_cp = calculate_sync_cp(page_count)
            try:
                reserve_flat_tokens(
                    conn_user_id,
                    total_cp,
                    auto_create=False,
                    transaction_description=f'Folder sync deck analysis ({conn_provider})',
                    session=session_db,
                )
            except APIError:
                err_msg = 'Insufficient Check Points'
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='insufficient_cp',
                    cp_charged=0,
                    error_message=err_msg,
                )
                skipped_insufficient_cp += 1
                if rec is None:
                    records_summary.append(_build_summary_dict(item, status='insufficient_cp', cp_charged=0, error_message=err_msg))
                    break
                records_summary.append(_record_to_summary(rec))
                continue

            try:
                analysis = pitchdeck_svc.analyze_pitch_deck(pdf_bytes)
                uploaded = provider.upload_memo(
                    session_db,
                    conn,
                    output_folder_ref=output_folder_ref,
                    analysis_result=analysis,
                    memo_preset=memo_preset,
                )
                exported_sheet_range = _try_auto_export_sheets(
                    session_db,
                    conn,
                    analysis_result=analysis,
                    memo_url=uploaded.url,
                )
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='completed',
                    cp_charged=total_cp,
                    exported_doc_url=uploaded.url,
                    exported_sheet_range=exported_sheet_range,
                    error_message=None,
                )
                _try_archive_processed_file(
                    session_db,
                    conn,
                    provider,
                    item,
                    input_folder_ref=input_folder_ref,
                    subfolder='Done',
                    prefix='[Analyzed] ',
                )
                trigger_post_action_refill_if_empty(conn_user_id, session=session_db)
                processed += 1
                if rec is None:
                    records_summary.append(
                        _build_summary_dict(
                            item,
                            status='completed',
                            cp_charged=total_cp,
                            exported_doc_url=uploaded.url,
                            exported_sheet_range=exported_sheet_range,
                            error_message=None,
                        )
                    )
                    break
                records_summary.append(_record_to_summary(rec))
            except CloudTokenError as exc:
                session_db.rollback()
                refund_flat_tokens(
                    conn_user_id,
                    total_cp,
                    transaction_description=f'Refund failed folder sync ({conn_provider})',
                    session=session_db,
                )
                if exc.status_code == 401:
                    raise
                err_msg = 'Storage authorization unavailable'
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='error',
                    cp_charged=0,
                    error_message=err_msg,
                )
                errors += 1
                if rec is None:
                    records_summary.append(_build_summary_dict(item, status='error', cp_charged=0, error_message=err_msg))
                    break
                records_summary.append(_record_to_summary(rec))
            except Exception as exc:
                logger.warning(
                    f"Deck sync failed (user_id={conn_user_id}, provider={conn_provider}, "
                    f"file_id={item.file_id}): {type(exc).__name__}"
                )
                session_db.rollback()
                refund_flat_tokens(
                    conn_user_id,
                    total_cp,
                    transaction_description=f'Refund failed folder sync ({conn_provider})',
                    session=session_db,
                )
                err_msg = f'Sync failed ({type(exc).__name__})'
                rec = _upsert_synced_record(
                    session_db,
                    user_id=conn_user_id,
                    connection_id=conn_id,
                    item=item,
                    existing_id=existing_id,
                    status='error',
                    cp_charged=0,
                    error_message=err_msg,
                )
                errors += 1
                if rec is None:
                    records_summary.append(_build_summary_dict(item, status='error', cp_charged=0, error_message=err_msg))
                    break
                records_summary.append(_record_to_summary(rec))
        finally:
            pdf_bytes = None

    if _connection_exists(session_db, conn_id):
        live_conn = (
            session_db.query(CloudStorageConnection)
            .filter_by(id=conn_id)
            .populate_existing()
            .first()
        )
        if live_conn is not None:
            live_conn.last_synced_at = datetime.now(UTC)
            live_conn.last_error = None if errors == 0 else 'One or more decks failed to sync'
            session_db.commit()

    return {
        'success': True,
        'provider': conn_provider,
        'processed': processed,
        'skipped_duplicate': skipped_duplicate,
        'skipped_invalid': skipped_invalid,
        'skipped_insufficient_cp': skipped_insufficient_cp,
        'errors': errors,
        'new_balance': _get_user_balance_value(session_db, conn_user_id),
        'records': records_summary,
    }


def run_daily_watched_folders_sync(session_db) -> dict:
    """Cron runner iterating all daily-sync-enabled CloudStorageConnection rows."""
    connections = (
        session_db.query(CloudStorageConnection)
        .filter(
            CloudStorageConnection.daily_sync_enabled.is_(True),
            or_(
                CloudStorageConnection.encrypted_access_token.isnot(None),
                CloudStorageConnection.encrypted_refresh_token.isnot(None),
            ),
        )
        .all()
    )

    targets = [(c.id, c.user_id, c.provider) for c in connections]

    connections_checked = 0
    processed = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    skipped_insufficient_cp = 0
    errors = 0

    for conn_id, conn_user_id, conn_provider in targets:
        connections_checked += 1
        try:
            if not _connection_exists(session_db, conn_id):
                continue
            conn = (
                session_db.query(CloudStorageConnection)
                .filter_by(id=conn_id)
                .populate_existing()
                .first()
            )
            if conn is None:
                continue
            summary = sync_connection(session_db, conn, max_files=MAX_SYNC_FILES_PER_RUN)
            processed += summary['processed']
            skipped_duplicate += summary['skipped_duplicate']
            skipped_invalid += summary['skipped_invalid']
            skipped_insufficient_cp += summary['skipped_insufficient_cp']
            errors += summary['errors']
        except (CloudTokenError, ProviderFolderError) as exc:
            session_db.rollback()
            logger.warning(
                f"Daily folder sync skipped connection (user_id={conn_user_id}, "
                f"provider={conn_provider}): {type(exc).__name__}"
            )
            errors += 1
        except Exception as exc:
            session_db.rollback()
            logger.warning(
                f"Daily folder sync unexpected failure (user_id={conn_user_id}, "
                f"provider={conn_provider}): {type(exc).__name__}"
            )
            errors += 1

    return {
        'success': True,
        'connections_checked': connections_checked,
        'processed': processed,
        'skipped_duplicate': skipped_duplicate,
        'skipped_invalid': skipped_invalid,
        'skipped_insufficient_cp': skipped_insufficient_cp,
        'errors': errors,
    }
