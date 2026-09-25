"""
Media authenticity analysis routes.
"""
from flask import Blueprint, request, jsonify
import logging
import time
from datetime import datetime, UTC
import json
import re
import mimetypes
from urllib.parse import urlparse
import os
from werkzeug.utils import secure_filename
import tempfile

from backend.database import db_session
from backend.models import MediaCheck, TokenBalance
from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.services import get_gemini_service
from backend.config import Config
from backend.extensions import limiter

logger = logging.getLogger(__name__)

media_bp = Blueprint('media', __name__, url_prefix='/api/media')

# Whitelist for supported media types
ALLOWED_MIME_TYPES = {
    'image/jpeg', 'image/png', 'image/webp', 'image/gif',
    'video/mp4', 'video/webm', 'video/quicktime', 'video/x-matroska',
    'video/m4v', 'video/x-m4v', 'video/avi', 'video/x-msvideo', 'video/3gpp', 'video/ogg',
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

from backend.services.gemini.media import GeminiServiceMedia

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


def _analyze_media(
    source: str,
    input_type: str,
    mime_type: str,
    user,
    display_name: str,
) -> dict:
    """Shared media analysis pipeline used by both URL and upload routes.

    Handles: token reservation, cache flush, Gemini analysis, embedding,
    DB persistence, and auto-refund on failure.
    """
    cost = getattr(Config, 'MEDIA_ANALYSIS_COST', 1)

    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    if not token_balance or token_balance.balance < cost:
        raise APIError(f'Insufficient tokens. Media analysis costs {cost} CP.', status_code=403)

    token_balance.balance -= cost
    token_balance.last_updated = datetime.now(UTC)
    db_session.commit()

    logger.info(
        f"Media analysis started for user {user.email}: "
        f"{display_name} (Cost: {cost})"
    )
    start_time = time.time()

    gemini_service = get_gemini_service()

    try:
        # Flush previous volatile cache
        if user.current_media_cache:
            gemini_service.delete_cache(user.current_media_cache)
            user.current_media_cache = None
            db_session.commit()

        # Perform analysis
        result = gemini_service.analyze_media_authenticity(
            source, input_type=input_type, mime_type=mime_type
        )

        # Capture volatile cache if created
        if result.get('cache_name'):
            user.current_media_cache = result['cache_name']
            db_session.commit()

        # Get multimodal embedding (fingerprint)
        embedding = gemini_service.get_media_embedding(
            source, mime_type, input_type=input_type
        )

        processing_time = time.time() - start_time

        claims = result.get('claims', [])

        # Persist to database
        url_value = source if input_type == 'url' else f"upload://{display_name}"
        media_check = MediaCheck(
            user_id=user.id,
            url=url_value,
            mime_type=mime_type,
            verdict=result.get('verdict'),
            confidence=result.get('confidence'),
            reasoning=result.get('explanation'),
            criteria_json=json.dumps(result.get('criteria')),
            claims_json=json.dumps(claims) if claims else None,
            embedding_json=json.dumps(embedding),
            processing_time=processing_time,
        )
        db_session.add(media_check)
        db_session.commit()

        return {
            'success': True,
            'result': {
                'verdict': result.get('verdict'),
                'confidence': result.get('confidence'),
                'explanation': result.get('explanation'),
                'criteria': result.get('criteria'),
                'claims': claims,
                'processing_time': processing_time,
            },
            'new_balance': token_balance.balance,
        }
    except Exception:
        try:
            token_balance.balance += cost
            db_session.commit()
            logger.info(f"Refunded {cost} CP to user {user.email} due to analysis failure")
        except Exception as refund_err:
            logger.error(f"Failed to refund tokens: {refund_err}")
        raise


@media_bp.route('/analyze-url', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def analyze_url() -> tuple:
    """Analyze a public media URL for authenticity."""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            raise APIError('No URL provided', status_code=400)
        
        url = data['url'].strip()
        
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
            return jsonify(
                _analyze_media(canonical_url, 'url', 'video/mp4', request.current_user, f"YouTube ({yt_id})")
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
            # SSRF check: ensure URL points to a public, non-private IP before issuing HEAD request
            gemini_service = get_gemini_service()
            if not gemini_service._validate_url(url):
                raise APIError('Invalid or restricted URL. The address cannot be resolved or is not accessible.', status_code=400)

            import requests as http_requests
            try:
                head_resp = http_requests.head(url, timeout=5, allow_redirects=True)
                ct = head_resp.headers.get('Content-Type', '')
                # Strip parameters (e.g. "image/jpeg; charset=utf-8" → "image/jpeg")
                ct_base = ct.split(';')[0].strip().lower()
                if ct_base in ALLOWED_MIME_TYPES:
                    mime_type = ct_base
            except Exception as head_err:
                logger.warning(f"HEAD probe failed for {url[:100]}: {head_err}")

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

        return jsonify(
            _analyze_media(url, 'url', mime_type, request.current_user, url[:100])
        )

    except APIError:
        raise
    except ValueError as e:
        raise APIError(str(e), status_code=400)
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

        # Secure temporary storage
        filename = secure_filename(file.filename) or 'uploaded_media'
        fd, temp_path = tempfile.mkstemp(suffix=f"_{filename}")
        os.close(fd)
        file.save(temp_path)

        return jsonify(
            _analyze_media(temp_path, 'file', mime_type, request.current_user, filename)
        )

    except APIError:
        raise
    except ValueError as e:
        db_session.rollback()
        logger.error(f"Media validation error: {e}")
        raise APIError(str(e), status_code=400)
    except Exception as e:
        db_session.rollback()
        logger.error(f"Media upload error: {e}", exc_info=True)
        err_msg = str(e)
        if 'codec' in err_msg.lower() or 'decode' in err_msg.lower():
            user_msg = 'The video could not be decoded. Please verify the file is encoded in standard H.264 MP4, MOV, or WebM format.'
        else:
            user_msg = f'Failed to process uploaded media: {err_msg}'
        raise APIError(user_msg, status_code=400)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            logger.debug(f"Cleaned up temporary upload file: {temp_path}")
