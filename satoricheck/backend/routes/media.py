"""
Media authenticity analysis routes.
"""
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
from urllib.parse import urlparse

from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename

from backend.database import db_session
from backend.models import MediaCheck
from backend.routes.auth import login_required
from backend.routes.tokens import (
    ensure_sufficient_tokens,
    refund_flat_tokens,
    reserve_flat_tokens,
    trigger_post_action_refill_if_empty,
)
from backend.error_handlers import APIError
from backend.services import get_gemini_service, safe_http
from backend.services.gemini.media import GeminiServiceMedia
from backend.config import Config
from backend.extensions import limiter

logger = logging.getLogger(__name__)

media_bp = Blueprint('media', __name__, url_prefix='/api/media')

# Whitelist for supported media types
ALLOWED_MIME_TYPES = {
    'image/jpeg', 'image/png', 'image/webp', 'image/gif',
    'video/mp4', 'video/webm', 'video/quicktime', 'video/x-matroska',
    'video/m4v', 'video/x-m4v', 'video/avi', 'video/x-msvideo', 'video/3gpp',
    'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/webm', 'audio/ogg',
    'audio/x-m4a', 'audio/aac', 'audio/m4a'
}

# Simple regex for initial URL validation
URL_REGEX = re.compile(
    r'^https?://'  # http:// or https://
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # domain...
    r'localhost|'  # localhost...
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
    r'(?::\d+)?'  # optional port
    r'(?:/?|[/?]\S+)$', re.IGNORECASE)

# Hostnames of platforms that serve HTML pages (not direct media files).
# Maps hostname substrings to human-readable names for error messages.
_PLATFORM_HOSTS = {
    'dailymotion.com': 'Dailymotion',
    'facebook.com': 'Facebook',
    'fb.watch': 'Facebook',
    'instagram.com': 'Instagram',
    'reddit.com': 'Reddit',
    'tiktok.com': 'TikTok',
    'twitch.tv': 'Twitch',
    'vimeo.com': 'Vimeo',
    'x.com': 'X (Twitter)',
    'twitter.com': 'X (Twitter)',
}


def _detect_platform(hostname: str) -> str | None:
    """Return the platform name if the hostname matches a known video/social site."""
    hostname = hostname.lower().removeprefix('www.')
    for domain, name in _PLATFORM_HOSTS.items():
        if hostname == domain or hostname.endswith(f'.{domain}'):
            return name
    return None


VIDEO_SHORT_MAX_BYTES = GeminiServiceMedia.VIDEO_HIGH_FPS_MAX_BYTES  # 5 MB
VIDEO_MEDIUM_MAX_BYTES = GeminiServiceMedia.MAX_INLINE_MEDIA_BYTES   # 14 MB

_SAFE_MEDIA_VALUE_ERRORS = frozenset({
    'YouTube video is unavailable, private, or does not exist.',
    'Invalid or restricted URL',
    'Media analysis returned an empty response',
    'Media analysis returned an incomplete response',
})


def _calculate_media_cp_cost(mime_type: str, file_size_bytes: int | None = None) -> int:
    """Compute Check Point (CP) cost from MIME type and file byte size."""
    mime = (mime_type or '').lower()
    if mime.startswith('audio/'):
        return Config.MEDIA_CP_AUDIO
    if mime.startswith('video/'):
        if file_size_bytes is None or file_size_bytes > VIDEO_MEDIUM_MAX_BYTES:
            return Config.MEDIA_CP_VIDEO_LONG
        if file_size_bytes <= VIDEO_SHORT_MAX_BYTES:
            return Config.MEDIA_CP_VIDEO_SHORT
        return Config.MEDIA_CP_VIDEO_MEDIUM
    return Config.MEDIA_CP_IMAGE


def _analyze_media(
    source: str | bytes,
    input_type: str,
    mime_type: str,
    user,
    display_name: str,
    *,
    file_size_bytes: int | None = None,
    start_seconds: int = 0,
    record_url: str | None = None,
) -> dict:
    """Shared media analysis pipeline used by both URL and upload routes.

    Handles: file-size tiered cost calculation, token reservation, Gemini forensic
    analysis, DB persistence, and auto-refund on failure.
    """
    cost = _calculate_media_cp_cost(mime_type, file_size_bytes)

    reserve_flat_tokens(
        user.id,
        cost,
        error_message=f'Insufficient tokens. Media analysis costs {cost} CP.'
    )

    logger.info(
        f"Media analysis started for user_id={user.id}: "
        f"{input_type} ({mime_type}, Cost: {cost} CP)"
    )
    start_time = time.time()

    gemini_service = get_gemini_service()

    try:
        analysis_kwargs = {'input_type': input_type, 'mime_type': mime_type}
        if start_seconds > 0:
            analysis_kwargs['start_seconds'] = start_seconds

        result = gemini_service.analyze_media_authenticity(source, **analysis_kwargs)

        processing_time = time.time() - start_time

        claims = result.get('claims', [])

        # Persist to database
        url_value = record_url or (source if input_type == 'url' else f"upload://{display_name}")
        media_check = MediaCheck(
            user_id=user.id,
            url=url_value,
            mime_type=mime_type,
            verdict=result.get('verdict'),
            confidence=result.get('confidence'),
            reasoning=result.get('explanation'),
            criteria_json=json.dumps(result.get('criteria')),
            claims_json=json.dumps(claims) if claims else None,
            embedding_json=None,
            processing_time=processing_time,
        )
        db_session.add(media_check)
        db_session.commit()

        new_balance, refill_failed = trigger_post_action_refill_if_empty(user.id)

        payload = {
            'success': True,
            'result': {
                'verdict': result.get('verdict'),
                'confidence': result.get('confidence'),
                'explanation': result.get('explanation'),
                'criteria': result.get('criteria'),
                'claims': claims,
                'processing_time': processing_time,
            },
            'cost_incurred': cost,
            'new_balance': new_balance,
        }
        if refill_failed:
            payload['auto_refill_failed'] = True
        return payload
    except Exception:
        refund_flat_tokens(user.id, cost)
        logger.info(f"Refunded {cost} CP to user_id={user.id} due to analysis failure")
        raise


@media_bp.route('/analyze-url', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def analyze_url() -> tuple:
    """Analyze a public media URL for authenticity."""
    try:
        data = request.get_json()
        if not data:
            raise APIError('No URL provided', status_code=400)
        if not isinstance(data, dict):
            raise APIError('Invalid request payload', status_code=400)
        if 'url' not in data:
            raise APIError('No URL provided', status_code=400)

        raw_url = data['url']
        if not isinstance(raw_url, str):
            raise APIError('URL must be a string', status_code=400)

        url = raw_url.strip()

        # Early regex validation
        if not URL_REGEX.match(url):
            raise APIError('Invalid URL format. Must start with http:// or https://', status_code=400)

        # YouTube URL handling: Gemini natively supports public YouTube URLs via Part.from_uri
        parsed_url = urlparse(url)
        hostname = (parsed_url.hostname or '').lower().removeprefix('www.')
        if hostname in ('youtube.com', 'm.youtube.com', 'youtu.be') or hostname.endswith('.youtube.com'):
            yt_id = GeminiServiceMedia.extract_youtube_id(url)
            if not yt_id:
                raise APIError(
                    'Invalid YouTube URL. Please provide a link to a specific YouTube video '
                    '(e.g. https://www.youtube.com/watch?v=... or https://youtu.be/...).',
                    status_code=400
                )
            canonical_url = f"https://www.youtube.com/watch?v={yt_id}"
            yt_start = GeminiServiceMedia.extract_youtube_start_seconds(url)

            # Duration is unknown before Gemini fetches the URI, so YouTube bills the full window.
            yt_cost = Config.MEDIA_CP_VIDEO_LONG
            ensure_sufficient_tokens(
                request.current_user.id,
                yt_cost,
                f'Insufficient tokens. Media analysis costs {yt_cost} CP.',
                error_status=403,
            )

            gemini_service = get_gemini_service()
            gemini_service.validate_youtube_video(canonical_url)

            record_url = f"{canonical_url}&t={yt_start}s" if yt_start > 0 else canonical_url
            return jsonify(
                _analyze_media(
                    canonical_url,
                    'url',
                    'video/mp4',
                    request.current_user,
                    f"YouTube ({yt_id})",
                    start_seconds=yt_start,
                    record_url=record_url,
                )
            )

        # Determine MIME type: try URL extension first, then HEAD request
        parsed_path = parsed_url.path
        mime_type, _ = mimetypes.guess_type(parsed_path)

        # Fallback: extension-based heuristic for common suffixes
        if not mime_type:
            lower_path = parsed_path.lower()
            if lower_path.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                mime_type = 'image/jpeg'
            elif lower_path.endswith(('.mp4', '.mov', '.webm', '.mkv')):
                mime_type = 'video/mp4'
            elif lower_path.endswith(('.mp3', '.m4a', '.aac')):
                mime_type = 'audio/mpeg'
            elif lower_path.endswith('.wav'):
                mime_type = 'audio/wav'
            elif lower_path.endswith('.ogg'):
                mime_type = 'audio/ogg'

        # Fallback: probe Content-Type via HEAD (handles CDN/API URLs with no extension)
        if not mime_type:
            # Fail fast before making outbound network probes for zero-balance accounts
            ensure_sufficient_tokens(
                request.current_user.id,
                Config.MEDIA_CP_IMAGE,
                f'Insufficient tokens. Media analysis costs at least {Config.MEDIA_CP_IMAGE} CP.',
                error_status=403,
            )

            try:
                # safe_request validates every redirect hop and pins DNS against rebinding.
                head_resp = safe_http.safe_request('HEAD', url, timeout=5)
                head_resp.close()
                ct = head_resp.headers.get('Content-Type', '')
                # Strip parameters (e.g. "image/jpeg; charset=utf-8" → "image/jpeg")
                ct_base = ct.split(';')[0].strip().lower()
                if ct_base in ALLOWED_MIME_TYPES:
                    mime_type = ct_base
            except safe_http.UnsafeURLError:
                raise APIError(
                    'Invalid or restricted URL. The address cannot be resolved or is not accessible.',
                    status_code=400,
                )
            except Exception as head_err:
                logger.warning(f"HEAD probe failed for host={parsed_url.hostname}: {type(head_err).__name__}")

        if not mime_type or mime_type not in ALLOWED_MIME_TYPES:
            # Check for known video/social platforms and give a helpful error
            hostname = urlparse(url).hostname or ''
            platform = _detect_platform(hostname)
            if platform:
                raise APIError(
                    f'{platform} links are not yet supported. '
                    'Please paste a direct image, video, or audio URL '
                    '(e.g. right-click media → "Copy media address").',
                    status_code=400
                )
            raise APIError(
                'This URL type is not supported. '
                'Please paste a direct link to an image, video, or audio file '
                '(e.g. right-click media → "Copy link address"). '
                'Supported formats: JPEG, PNG, WebP, MP4, MOV, WebM, MP3, WAV, OGG, M4A.',
                status_code=400
            )

        if mime_type.startswith(('video/', 'audio/')):
            min_cost = (
                Config.MEDIA_CP_VIDEO_SHORT
                if mime_type.startswith('video/')
                else Config.MEDIA_CP_AUDIO
            )
            ensure_sufficient_tokens(
                request.current_user.id,
                min_cost,
                f'Insufficient tokens. Media analysis costs {min_cost} CP.',
                error_status=403,
            )

            media_bytes = safe_http.download_bounded(
                url,
                max_bytes=GeminiServiceMedia.MAX_REMOTE_MEDIA_BYTES,
                timeout=30,
                total_timeout=GeminiServiceMedia.REMOTE_MEDIA_TOTAL_TIMEOUT_S,
            )
            return jsonify(
                _analyze_media(
                    media_bytes,
                    'bytes',
                    mime_type,
                    request.current_user,
                    url[:100],
                    file_size_bytes=len(media_bytes),
                    record_url=url,
                )
            )

        return jsonify(
            _analyze_media(url, 'url', mime_type, request.current_user, url[:100])
        )

    except APIError:
        raise
    except safe_http.UnsafeURLError as e:
        db_session.rollback()
        logger.warning(f"Blocked unsafe media URL: {e}")
        raise APIError(
            'Invalid or restricted URL. The address cannot be resolved or is not accessible.',
            status_code=400,
        )
    except safe_http.ResponseTooLargeError as e:
        db_session.rollback()
        logger.warning(f"Media URL download limit exceeded: {e}")
        raise APIError(
            'Remote media file exceeds the maximum allowed size or download time limit.',
            status_code=400,
        )
    except ValueError as e:
        db_session.rollback()
        msg = str(e)
        logger.warning(f"Media URL validation error: {msg}")
        if msg in _SAFE_MEDIA_VALUE_ERRORS:
            raise APIError(msg, status_code=400)
        raise APIError(
            'Media URL could not be processed. Please verify the URL and try again.',
            status_code=400,
        )
    except Exception as e:
        db_session.rollback()
        logger.error(f"Media route error: {e}", exc_info=True)
        raise APIError('Analysis failed. Please ensure the URL is public and try again.', status_code=503)


@media_bp.route('/analyze-upload', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def analyze_upload() -> tuple:
    """Analyze an uploaded media file for authenticity."""
    temp_path = None
    try:
        if 'file' not in request.files:
            raise APIError('No file uploaded', status_code=400)

        file = request.files['file']
        if file.filename == '':
            raise APIError('No file selected', status_code=400)

        # MIME type normalization & filename-based fallback
        mime_type = file.content_type
        if mime_type:
            mime_type = mime_type.split(';')[0].strip().lower()

        if not mime_type or mime_type == 'application/octet-stream' or mime_type not in ALLOWED_MIME_TYPES:
            guessed, _ = mimetypes.guess_type(file.filename)
            if guessed and guessed in ALLOWED_MIME_TYPES:
                mime_type = guessed
            else:
                lower_fn = file.filename.lower()
                if lower_fn.endswith(('.mp4', '.m4v')):
                    mime_type = 'video/mp4'
                elif lower_fn.endswith('.mov'):
                    mime_type = 'video/quicktime'
                elif lower_fn.endswith('.webm'):
                    mime_type = 'video/webm'
                elif lower_fn.endswith('.mkv'):
                    mime_type = 'video/x-matroska'
                elif lower_fn.endswith(('.jpg', '.jpeg')):
                    mime_type = 'image/jpeg'
                elif lower_fn.endswith('.png'):
                    mime_type = 'image/png'
                elif lower_fn.endswith('.webp'):
                    mime_type = 'image/webp'
                elif lower_fn.endswith('.mp3'):
                    mime_type = 'audio/mpeg'
                elif lower_fn.endswith('.wav'):
                    mime_type = 'audio/wav'
                elif lower_fn.endswith(('.m4a', '.aac')):
                    mime_type = 'audio/m4a'

        if not mime_type or mime_type not in ALLOWED_MIME_TYPES:
            raise APIError(
                f'Unsupported media format ({mime_type or "unknown"}). '
                'Supported formats: MP4, MOV, WebM, MKV, MP3, WAV, OGG, M4A, JPEG, PNG, WebP.',
                status_code=400
            )

        # Fail fast before writing up to 50 MB to disk for underfunded accounts
        min_upload_cost = (
            Config.MEDIA_CP_VIDEO_SHORT
            if mime_type.startswith('video/')
            else (Config.MEDIA_CP_AUDIO if mime_type.startswith('audio/') else Config.MEDIA_CP_IMAGE)
        )
        ensure_sufficient_tokens(
            request.current_user.id,
            min_upload_cost,
            f'Insufficient tokens. Media analysis costs {min_upload_cost} CP.',
            error_status=403,
        )

        # Secure temporary storage
        filename = secure_filename(file.filename) or 'uploaded_media'
        fd, temp_path = tempfile.mkstemp(suffix=f"_{filename}")
        os.close(fd)
        file.save(temp_path)
        file_size_bytes = os.path.getsize(temp_path)

        return jsonify(
            _analyze_media(
                temp_path,
                'file',
                mime_type,
                request.current_user,
                filename,
                file_size_bytes=file_size_bytes,
            )
        )

    except APIError:
        raise
    except safe_http.UnsafeURLError as e:
        db_session.rollback()
        logger.warning(f"Blocked unsafe media reference in upload: {e}")
        raise APIError(
            'Invalid or restricted URL. The address cannot be resolved or is not accessible.',
            status_code=400,
        )
    except safe_http.ResponseTooLargeError as e:
        db_session.rollback()
        logger.warning(f"Media upload size/time limit exceeded: {e}")
        raise APIError(
            'Media file exceeds the maximum allowed size or processing time limit.',
            status_code=400,
        )
    except ValueError as e:
        db_session.rollback()
        msg = str(e)
        logger.error(f"Media validation error: {msg}")
        if msg in _SAFE_MEDIA_VALUE_ERRORS:
            raise APIError(msg, status_code=400)
        raise APIError(
            'Failed to process uploaded media. Please try again or use a different file.',
            status_code=400,
        )
    except Exception as e:
        db_session.rollback()
        logger.error(f"Media upload error: {e}", exc_info=True)
        # Never echo raw exception text: it can leak temp paths, SDK internals, or upstream URLs.
        err_msg = str(e).lower()
        if 'codec' in err_msg or 'decode' in err_msg:
            user_msg = 'The video could not be decoded. Please verify the file is encoded in standard H.264 MP4, MOV, or WebM format.'
        else:
            user_msg = 'Failed to process uploaded media. Please try again or use a different file.'
        raise APIError(user_msg, status_code=400)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            logger.debug(f"Cleaned up temporary upload file: {temp_path}")
