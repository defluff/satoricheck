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

from backend.database import db_session
from backend.models import TokenBalance, MediaCheck
from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.services import get_gemini_service
from backend.config import Config

import os
from werkzeug.utils import secure_filename
import tempfile

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

@media_bp.route('/analyze-url', methods=['POST'])
@login_required
def analyze_url() -> tuple:
    """Analyze a public media URL for authenticity."""
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            raise APIError('No URL provided', status_code=400)
        
        url = data['url'].strip()
        
        # 1. Early Regex Validation
        if not URL_REGEX.match(url):
            raise APIError('Invalid URL format. Must start with http:// or https://', status_code=400)
            
        # Optional: guess MIME type from URL path
        parsed_path = urlparse(url).path
        mime_type, _ = mimetypes.guess_type(parsed_path)
        if not mime_type:
            # Fallback based on common extensions
            if parsed_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                mime_type = 'image/jpeg'
            elif parsed_path.lower().endswith(('.mp4', '.mov', '.webm')):
                mime_type = 'video/mp4'
            else:
                raise APIError(
                    'Could not determine media type from URL. '
                    'Supported: .jpg, .jpeg, .png, .webp, .mp4, .mov, .webm',
                    status_code=400
                )
        
        user = request.current_user
        
        # 2. Token Balance Check
        cost = getattr(Config, 'MEDIA_ANALYSIS_COST', 1)
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance or token_balance.balance < cost:
            raise APIError(f'Insufficient tokens. Media analysis costs {cost} CP.', status_code=403)
        
        gemini_service = get_gemini_service()
        
        # 3. Deduct Token
        token_balance.balance -= cost
        token_balance.last_updated = datetime.now(UTC)
        db_session.commit() # Commit deduction before expensive call
        
        logger.info(f"Media analysis started for user {user.email}: {url[:100]} (Cost: {cost})")
        
        start_time = time.time()
        
        # 4. Flush Previous Cache (Volatile Caching)
        if user.current_media_cache:
            gemini_service.delete_cache(user.current_media_cache)
            user.current_media_cache = None
            db_session.commit()

        try:
            # 5. Perform Analysis
            # This calls gemini_service which handles SSRF protection internally
            result = gemini_service.analyze_media_authenticity(url, input_type='url', mime_type=mime_type)
            
            # Capture volatile cache if created
            if result.get('cache_name'):
                user.current_media_cache = result['cache_name']
                db_session.commit()
            
            # 5. Get Multimodal Embedding (Fingerprint)
            embedding = gemini_service.get_media_embedding(url, mime_type, input_type='url')
            
            processing_time = time.time() - start_time
            
            # 6. Persist to Database
            media_check = MediaCheck(
                user_id=user.id,
                url=url,
                mime_type=mime_type,
                verdict=result.get('verdict'),
                confidence=result.get('confidence'),
                reasoning=result.get('explanation'), # Changed from reasoning to explanation
                criteria_json=json.dumps(result.get('criteria')),
                embedding_json=json.dumps(embedding),
                processing_time=processing_time
            )
            db_session.add(media_check)
            db_session.commit()
            
            return jsonify({
                'success': True,
                'result': {
                    'verdict': result.get('verdict'),
                    'confidence': result.get('confidence'),
                    'explanation': result.get('explanation'), # Changed from reasoning to explanation
                    'criteria': result.get('criteria'),
                    'processing_time': processing_time
                },
                'new_balance': token_balance.balance
            })

        except Exception as e:
            # 7. Refund Token on Failure
            try:
                token_balance.balance += cost
                db_session.commit()
                logger.info(f"Refunded {cost} tokens to user {user.email} due to analysis failure")
            except Exception as refund_err:
                logger.error(f"Failed to refund tokens: {refund_err}")
                
            if isinstance(e, ValueError):
                raise APIError(str(e), status_code=400)
            
            logger.error(f"Media analysis failed: {e}", exc_info=True)
            raise APIError('Analysis failed. Please ensure the URL is public and try again.', status_code=503)

    except APIError:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Media route error: {e}", exc_info=True)
        raise APIError('Failed to process media request')

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

        # 1. Token Balance Check
        user = request.current_user
        cost = getattr(Config, 'MEDIA_ANALYSIS_COST', 1)
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance or token_balance.balance < cost:
            raise APIError(f'Insufficient tokens. Media analysis costs {cost} CP.', status_code=403)
        
        # 2. Secure temporary storage
        filename = secure_filename(file.filename)
        fd, temp_path = tempfile.mkstemp(suffix=f"_{filename}")
        os.close(fd)
        
        file.save(temp_path)
        
        # 3. Deduct Token
        token_balance.balance -= cost
        token_balance.last_updated = datetime.now(UTC)
        db_session.commit() # Commit deduction before expensive call
        
        logger.info(f"Media upload analysis started for user {user.email}: {filename}")
        start_time = time.time()
        
        try:
            gemini_service = get_gemini_service()
            
            # 4. Flush Previous Cache (Volatile Caching)
            if user.current_media_cache:
                gemini_service.delete_cache(user.current_media_cache)
                user.current_media_cache = None
                db_session.commit()

            # 5. Perform Analysis
            result = gemini_service.analyze_media_authenticity(temp_path, input_type='file', mime_type=mime_type)
            
            # Capture volatile cache if created
            if result.get('cache_name'):
                user.current_media_cache = result['cache_name']
                db_session.commit()

            # 6. Get Multimodal Embedding (Fingerprint)
            embedding = gemini_service.get_media_embedding(temp_path, mime_type, input_type='file')
            
            processing_time = time.time() - start_time
            
            # 7. Persist to Database
            media_check = MediaCheck(
                user_id=user.id,
                url=f"upload://{filename}",
                mime_type=mime_type,
                verdict=result.get('verdict'),
                confidence=result.get('confidence'),
                reasoning=result.get('explanation'),
                criteria_json=json.dumps(result.get('criteria')),
                embedding_json=json.dumps(embedding),
                processing_time=processing_time
            )
            db_session.add(media_check)
            db_session.commit()
            
            return jsonify({
                'success': True,
                'result': {
                    'verdict': result.get('verdict'),
                    'confidence': result.get('confidence'),
                    'explanation': result.get('explanation'),
                    'criteria': result.get('criteria'),
                    'processing_time': processing_time
                },
                'new_balance': token_balance.balance
            })

        except Exception as e:
            # 7. Refund Token on Failure
            try:
                token_balance.balance += cost
                db_session.commit()
                logger.info(f"Refunded {cost} tokens to user {user.email} due to upload analysis failure")
            except Exception as refund_err:
                logger.error(f"Failed to refund tokens: {refund_err}")
            
            logger.error(f"Media upload analysis failed: {e}", exc_info=True)
            raise APIError(f'Analysis failed: {str(e)}', status_code=500)

        finally:
            # 8. Local Cleanup
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
                logger.info(f"Cleaned up temporary upload file: {temp_path}")

    except APIError:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Media upload error: {e}", exc_info=True)
        raise APIError('Failed to process uploaded media')
