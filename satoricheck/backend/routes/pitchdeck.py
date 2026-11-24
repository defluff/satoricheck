"""
Pitchdeck analysis routes.
Handles PDF upload and analysis using Gemini Vision.
"""
from flask import Blueprint, request, jsonify
import base64
import hmac
import logging
import math
import re

from backend.routes.auth import login_required
from backend.routes.tokens import (
    ensure_sufficient_tokens,
    refund_flat_tokens,
    reserve_flat_tokens,
    trigger_post_action_refill_if_empty,
)
from backend.error_handlers import APIError
from backend.services import get_pitchdeck_service
from backend.services.pitchdeck_service import PitchdeckService

# Lazy import to avoid circular dependency if possible, or use current_app
from backend.extensions import limiter

logger = logging.getLogger(__name__)

pitchdeck_bp = Blueprint('pitchdeck', __name__, url_prefix='/api/pitchdeck')

_SAFE_PITCHDECK_VALUE_ERRORS = frozenset({
    'PDF data is required',
    'PDF data is required - file is empty',
    'Invalid PDF - not a valid PDF file',
    'No response from analysis',
    'Malformed response structure',
    'Failed to parse analysis response',
})

_SAFE_PITCHDECK_VALUE_ERROR_PATTERNS = (
    re.compile(r'File too large\. Maximum size is \d+MB'),
    re.compile(r'Pitch deck exceeds maximum allowed size \(\d+ slides\)\. Maximum allowed is \d+ slides\.'),
)

_OriginalPitchdeckService = PitchdeckService


def _resolve_pitchdeck_service() -> PitchdeckService:
    """Return the singleton PitchdeckService, or instantiate if patched in tests."""
    if PitchdeckService is not _OriginalPitchdeckService:
        return PitchdeckService()
    return get_pitchdeck_service()


@pitchdeck_bp.route('/analyze', methods=['POST'])
@login_required
@limiter.limit("10 per hour")
def analyze_pitch_deck():
    """
    Analyze a pitch deck PDF.
    
    Request body:
        pdf_data: Base64-encoded PDF file
        
    Returns:
        JSON with analysis results (company_name, summary, usp, etc.)
    """
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided', status_code=400)
        if not isinstance(data, dict):
            raise APIError('Invalid request payload', status_code=400)
        
        pdf_data_base64 = data.get('pdf_data')

        if not pdf_data_base64:
            raise APIError('pdf_data is required', status_code=400)
        if not isinstance(pdf_data_base64, str):
            raise APIError('Invalid base64-encoded PDF data', status_code=400)

        # --- PRICING & INPUT VALIDATION (pre-decode guards) ---
        user = request.current_user
        service = _resolve_pitchdeck_service()

        max_b64_len = (service.MAX_FILE_SIZE * 4 // 3) + 4
        if len(pdf_data_base64) > max_b64_len:
            raise APIError(
                f'File too large. Maximum size is {service.MAX_FILE_SIZE // (1024 * 1024)}MB',
                status_code=400,
            )

        from backend.database import db_session
        ensure_sufficient_tokens(
            user.id,
            2,
            'Insufficient tokens. Analysis requires 2 CP.',
            error_status=403,
        )

        # Decode base64 strictly before inspecting PDF bytes
        try:
            pdf_bytes = base64.b64decode(pdf_data_base64, validate=True)
        except Exception as e:
            logger.warning(f"Invalid base64 data: {e}")
            raise APIError('Invalid base64-encoded PDF data', status_code=400)

        if not service._is_valid_pdf(pdf_bytes):
            raise APIError('Invalid PDF - not a valid PDF file', status_code=400)
        if len(pdf_bytes) > service.MAX_FILE_SIZE:
            raise APIError(
                f'File too large. Maximum size is {service.MAX_FILE_SIZE // (1024 * 1024)}MB',
                status_code=400,
            )

        page_count = service._estimate_page_count(pdf_bytes)
        if page_count > service.MAX_SLIDES_HARDCAP:
            raise APIError('Pitch deck exceeds maximum allowed size (100 slides). Please upload core deck.', status_code=400)

        # Calculate Cost (2 CP per 10 slides, Min 2, Max 12 CP for up to 60 standard slides)
        # Examples: 8 slides -> 2 CP. 12 slides -> 4 CP. 55 slides -> 12 CP.
        effective_pages = min(page_count, service.MAX_SLIDES_STANDARD)
        cost = max(2, math.ceil(effective_pages / 10) * 2)
        
        logger.info(f"[Pitchdeck] Pricing: {page_count} pages (effective {effective_pages}) -> {cost} CP")

        # Atomically Reserve Tokens (Optimistic Reservation)
        reserve_flat_tokens(
            user.id,
            cost,
            error_message=f'Insufficient tokens. Analysis requires {cost} CP.',
            auto_create=True
        )
        # ---------------------
        
        # Analyze with service
        try:
            from backend.services import get_gemini_service
            gemini_svc = get_gemini_service()

            # Flush Previous Cache (Volatile Caching)
            if user.current_pitchdeck_cache:
                gemini_svc.delete_cache(user.current_pitchdeck_cache)
                user.current_pitchdeck_cache = None
                db_session.commit()

            result = service.analyze_pitch_deck(pdf_bytes)
            
            # Use the cache_name from the result
            if result.get('cache_name'):
                user.current_pitchdeck_cache = result['cache_name']
                db_session.commit()
            
            # Privacy: Log ID instead of email
            logger.info(f"[Pitchdeck] Analysis complete for user {user.id}. Cost: {cost} CP")
            new_balance, refill_failed = trigger_post_action_refill_if_empty(user.id)

            resp_payload = {
                'success': True,
                'cost_incurred': cost,
                'new_balance': new_balance,
                **result,
            }
            if refill_failed:
                resp_payload['auto_refill_failed'] = True
            return jsonify(resp_payload)
            
        except (ValueError, TimeoutError) as e:
            # Revert deduction on known failures
            logger.warning(f"[Pitchdeck] Refunding {cost} CP due to expected error: {e}")
            refund_flat_tokens(user.id, cost)
            if isinstance(e, TimeoutError):
                raise APIError('Analysis timed out. Please try again.', status_code=504)
            msg = str(e)
            if msg in _SAFE_PITCHDECK_VALUE_ERRORS or any(
                pattern.fullmatch(msg) for pattern in _SAFE_PITCHDECK_VALUE_ERROR_PATTERNS
            ):
                raise APIError(msg, status_code=400)
            raise APIError(
                'Pitch deck could not be processed. Please verify the PDF and try again.',
                status_code=400,
            )
            
        except Exception as e:
            # Revert deduction on unexpected failures
            logger.error(f"[Pitchdeck] Refunding {cost} CP due to unexpected error: {e}", exc_info=True)
            refund_flat_tokens(user.id, cost)
            raise APIError('Analysis failed. Please try again.', status_code=500)
            
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Pitchdeck analysis error: {e}", exc_info=True)
        raise APIError('Analysis failed')


@pitchdeck_bp.route('/verify-market', methods=['POST'])
@login_required
@limiter.limit("20 per hour")
def verify_market_claims():
    """
    Fact-check market claims from a pitch deck analysis.
    
    Request body:
        verifiable_claims: Array of structured claim objects (preferred)
        market_size: Claimed market size string (legacy fallback)
        competition: List of competitor names (legacy fallback)
        industry: Industry category for context
        
    Returns:
        JSON with findings array containing verdicts and sources
    """
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided', status_code=400)
        if not isinstance(data, dict):
            raise APIError('Invalid request payload', status_code=400)
        
        verifiable_claims = data.get('verifiable_claims', [])
        industry = data.get('industry')
        requested_cache = data.get('cache_name')
        company = data.get('company') if data.get('company') is not None else data.get('company_name')
        summary = data.get('summary')

        service = _resolve_pitchdeck_service()
        max_company_len = getattr(service, 'MAX_COMPANY_NAME_LENGTH', 200)
        max_field_len = getattr(service, 'MAX_FIELD_LENGTH', 5000)

        # Validate top-level optional string bounds before reserving tokens
        if company is not None:
            if not isinstance(company, str):
                raise APIError('company must be a string', status_code=400)
            if len(company) > max_company_len:
                raise APIError(
                    f'company exceeds maximum length of {max_company_len} characters',
                    status_code=400,
                )

        if industry is not None:
            if not isinstance(industry, str):
                raise APIError('industry must be a string', status_code=400)
            if len(industry) > max_field_len:
                raise APIError(
                    f'industry exceeds maximum length of {max_field_len} characters',
                    status_code=400,
                )

        if summary is not None:
            if not isinstance(summary, str):
                raise APIError('summary must be a string', status_code=400)
            if len(summary) > max_field_len:
                raise APIError(
                    f'summary exceeds maximum length of {max_field_len} characters',
                    status_code=400,
                )

        # Validate claim list bounds, item schemas, and non-empty valid claims
        if not verifiable_claims or not isinstance(verifiable_claims, list):
            raise APIError('No claims to verify', status_code=400)
        if len(verifiable_claims) > 10:
            raise APIError('Too many claims. Maximum 10 claims per request.', status_code=400)

        valid_claims = []
        for item in verifiable_claims:
            if not isinstance(item, dict):
                raise APIError('Each claim entry must be an object', status_code=400)

            claim_val = item.get('claim')
            if claim_val is not None:
                if not isinstance(claim_val, str):
                    raise APIError('Claim text must be a string', status_code=400)
                if len(claim_val) > 1000:
                    raise APIError('Claim exceeds maximum length of 1000 characters', status_code=400)

            for field_name in ('context', 'source_cited', 'suggested_question'):
                field_val = item.get(field_name)
                if field_val is not None:
                    if not isinstance(field_val, str):
                        raise APIError(f'{field_name} must be a string', status_code=400)
                    if len(field_val) > 500:
                        raise APIError(
                            f'{field_name} exceeds maximum length of 500 characters',
                            status_code=400,
                        )

            for field_name in ('category', 'verifiability'):
                field_val = item.get(field_name)
                if field_val is not None:
                    if not isinstance(field_val, str):
                        raise APIError(f'{field_name} must be a string', status_code=400)
                    if len(field_val) > 50:
                        raise APIError(
                            f'{field_name} exceeds maximum length of 50 characters',
                            status_code=400,
                        )

            if isinstance(claim_val, str) and len(claim_val.strip()) >= 5:
                valid_claims.append(item)

        if not valid_claims:
            raise APIError('No valid claims to verify', status_code=400)

        user = request.current_user
        # Enforce cache ownership: all users share one GEMINI_API_KEY, so never forward
        # an unverified client-supplied cachedContents ID that could belong to another tenant.
        cache_name = None
        if requested_cache and isinstance(requested_cache, str):
            owned_cache = user.current_pitchdeck_cache
            if owned_cache and hmac.compare_digest(requested_cache, owned_cache):
                cache_name = owned_cache
            else:
                logger.warning(
                    f"[Pitchdeck] Ignoring unauthorized or stale cache_name for user {user.id}"
                )

        logger.info(f"[Pitchdeck] Verify request - claims: {len(valid_claims)}, company: {company}, cache: {bool(cache_name)}")

        # --- PRICING: 1 CP flat per market verification ---
        cost = 1  # Flat rate: 1 CP per verification call
        reserve_flat_tokens(
            user.id,
            cost,
            error_message=f'Insufficient tokens. Verification requires {cost} CP.'
        )

        # Verify claims using service
        try:
            findings = service.verify_market_claims(
                verifiable_claims=valid_claims,
                industry=industry,
                cache_name=cache_name,
                company=company,
                summary=summary
            )
        except Exception as e:
            # Refund on service failure
            refund_flat_tokens(user.id, cost)
            logger.error(f"[Pitchdeck] Verification upstream failure (refunded {cost} CP): {e}", exc_info=True)
            raise APIError('Verification temporarily unavailable. Please try again.', status_code=503)
        
        logger.info(f"[Pitchdeck] Verification complete for user {user.id}: {len(findings)} findings. Cost: {cost} CP")
        new_balance, refill_failed = trigger_post_action_refill_if_empty(user.id)

        resp_payload = {
            'success': True,
            'findings': findings,
            'new_balance': new_balance,
        }
        if refill_failed:
            resp_payload['auto_refill_failed'] = True
        return jsonify(resp_payload)
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"[Pitchdeck] Verification error: {e}", exc_info=True)
        raise APIError('Verification failed. Please try again.', status_code=500)


