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

logger = logging.getLogger(__name__)

media_bp = Blueprint('media', __name__, url_prefix='/api/media')

# Whitelist for supported media types
ALLOWED_MIME_TYPES = {
    'image/jpeg', 'image/png', 'image/webp', 'image/gif',
    'video/mp4', 'video/webm', 'video/quicktime', 'video/x-matroska'
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
    'youtu.be': 'YouTube',
    'youtube.com': 'YouTube',
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
            
        # Determine MIME type: try URL extension first, then HEAD request
        parsed_path = urlparse(url).path
        mime_type, _ = mimetypes.guess_type(parsed_path)

        # Fallback: extension-based heuristic for common suffixes
        if not mime_type:
            lower_path = parsed_path.lower()
            if lower_path.endswith(('.jpg', '.jpeg', '.png', '.webp')):
                mime_type = 'image/jpeg'
            elif lower_path.endswith(('.mp4', '.mov', '.webm')):
                mime_type = 'video/mp4'

        # Fallback: probe Content-Type via HEAD (handles CDN/API URLs with no extension)
        if not mime_type:
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
                    'Please paste a direct image or video URL '
                    '(e.g. right-click an image → "Copy image address").',
                    status_code=400
                )
            raise APIError(
                'This URL type is not supported. '
                'Please paste a direct link to an image or video file '
                '(e.g. right-click an image → "Copy image address"). '
                'Supported formats: JPEG, PNG, WebP, MP4, MOV, WebM.',
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
def analyze_upload() -> tuple:
    """Analyze an uploaded media file for authenticity."""
    temp_path = None
    try:
        if 'file' not in request.files:
            raise APIError('No file uploaded', status_code=400)
        
        file = request.files['file']
        if file.filename == '':
            raise APIError('No file selected', status_code=400)
        
        mime_type = file.content_type
        if not mime_type or mime_type not in ALLOWED_MIME_TYPES:
            raise APIError(f'Unsupported media type: {mime_type}', status_code=400)

        # Secure temporary storage
        filename = secure_filename(file.filename)
        fd, temp_path = tempfile.mkstemp(suffix=f"_{filename}")
        os.close(fd)
        file.save(temp_path)

        return jsonify(
            _analyze_media(temp_path, 'file', mime_type, request.current_user, filename)
        )

    except APIError:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Media upload error: {e}", exc_info=True)
        raise APIError('Failed to process uploaded media')
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
            logger.debug(f"Cleaned up temporary upload file: {temp_path}")
