"""
Pitchdeck analysis routes.
Handles PDF upload and analysis using Gemini Vision.
"""
from flask import Blueprint, request, jsonify
import base64
import logging

from backend.routes.auth import login_required
from backend.routes.tokens import reserve_flat_tokens, refund_flat_tokens
from backend.error_handlers import APIError
from backend.services import get_pitchdeck_service
from backend.services.pitchdeck_service import PitchdeckService

# Lazy import to avoid circular dependency if possible, or use current_app
from backend.extensions import limiter

logger = logging.getLogger(__name__)

pitchdeck_bp = Blueprint('pitchdeck', __name__, url_prefix='/api/pitchdeck')

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
        
        pdf_data_base64 = data.get('pdf_data')
        
        if not pdf_data_base64:
            raise APIError('pdf_data is required', status_code=400)
        
        # Decode base64
        try:
            pdf_bytes = base64.b64decode(pdf_data_base64)
        except Exception as e:
            logger.warning(f"Invalid base64 data: {e}")
            raise APIError('Invalid base64-encoded PDF data', status_code=400)
        
        # --- PRICING LOGIC ---
        user = request.current_user
        
        # 1. Estimate Page Count (Lean Regex)
        # Finds top-level /Count N in PDF trailer/catalog. Fallback to 1 if not found.
        page_count = 1
        try:
            import re
            import math
            # Search for /Count 123 in first 5kb (linearized) or last 5kb (standard)
            # Simplest approach: search entire bytestream with a safe limit or just search
            match = re.search(rb'/Count\s+(\d+)', pdf_bytes)
            if match:
                page_count = int(match.group(1).decode('utf-8'))
        except Exception as e:
            logger.warning(f"[Pitchdeck] Page count regex failed: {e}. Defaulting to 1 page.")
            page_count = 1
            
        if page_count > 100:
            raise APIError('Pitch deck exceeds maximum allowed size (100 slides). Please upload core deck.', status_code=400)

        # 2. Calculate Cost (1 CP per 10 slides, Min 1, Max 6 CP for up to 60 standard slides)
        # Examples: 8 slides -> 1 CP. 12 slides -> 2 CP. 55 slides -> 6 CP.
        effective_pages = min(page_count, 60)
        cost = max(1, math.ceil(effective_pages / 10))
        
        logger.info(f"[Pitchdeck] Pricing: {page_count} pages (effective {effective_pages}) -> {cost} CP")

        # 3. Atomically Reserve Tokens (Optimistic Reservation)
        from backend.database import db_session
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

            # 5. Flush Previous Cache (Volatile Caching)
            if user.current_pitchdeck_cache:
                gemini_svc.delete_cache(user.current_pitchdeck_cache)
                user.current_pitchdeck_cache = None
                db_session.commit()

            service = _resolve_pitchdeck_service()
            result = service.analyze_pitch_deck(pdf_bytes)
            
            # Use the cache_name from the result
            if result.get('cache_name'):
                user.current_pitchdeck_cache = result['cache_name']
                db_session.commit()
            
            # Privacy: Log ID instead of email
            logger.info(f"[Pitchdeck] Analysis complete for user {user.id}. Cost: {cost} CP")
            
            return jsonify({
                'success': True,
                'cost_incurred': cost, 
                **result
            })
            
        except (ValueError, TimeoutError) as e:
            # Revert deduction on known failures
            logger.warning(f"[Pitchdeck] Refunding {cost} CP due to expected error: {e}")
            refund_flat_tokens(user.id, cost)
            status_code = 400 if isinstance(e, ValueError) else 504
            raise APIError(str(e), status_code=status_code)
            
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
        
        verifiable_claims = data.get('verifiable_claims', [])
        market_size = data.get('market_size')
        competition = data.get('competition', [])
        industry = data.get('industry')
        cache_name = data.get('cache_name')
        company = data.get('company') or data.get('company_name')
        summary = data.get('summary')
        
        # Validate at least one claim to verify
        if not verifiable_claims and not market_size and not competition:
            raise APIError('No claims to verify', status_code=400)
        
        user = request.current_user
        logger.info(f"[Pitchdeck] Verify request - claims: {len(verifiable_claims)}, company: {company}, cache: {bool(cache_name)}")
        
        # --- PRICING: 1 CP flat per market verification ---
        cost = 1  # Flat rate: 1 CP per verification call
        token_balance = reserve_flat_tokens(
            user.id,
            cost,
            error_message=f'Insufficient tokens. Verification requires {cost} CP.'
        )
        
        # Verify claims using service
        try:
            service = _resolve_pitchdeck_service()
            findings = service.verify_market_claims(
                verifiable_claims=verifiable_claims,
                market_size=market_size,
                competition=competition,
                industry=industry,
                cache_name=cache_name,
                company=company,
                summary=summary
            )
        except Exception as e:
            # Refund on service failure
            refund_flat_tokens(user.id, cost)
            raise
        
        logger.info(f"[Pitchdeck] Verification complete for user {user.id}: {len(findings)} findings. Cost: {cost} CP")
        
        return jsonify({
            'success': True,
            'findings': findings,
            'new_balance': token_balance.balance
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"[Pitchdeck] Verification error: {e}", exc_info=True)
        raise APIError('Verification failed. Please try again.', status_code=500)


