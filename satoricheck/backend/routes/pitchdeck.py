"""
Pitchdeck analysis routes.
Handles PDF upload and analysis using Gemini Vision.
"""
from flask import Blueprint, request, jsonify
import base64
import logging

from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.services.pitchdeck_service import PitchdeckService

# Lazy import to avoid circular dependency if possible, or use current_app
from backend.extensions import limiter

logger = logging.getLogger(__name__)

pitchdeck_bp = Blueprint('pitchdeck', __name__, url_prefix='/api/pitchdeck')


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
            
        # 2. Calculate Cost (1 CP per 10 slides, Min 1)
        # Examples: 8 slides -> 1 CP. 12 slides -> 2 CP. 55 slides -> 6 CP.
        cost = max(1, math.ceil(page_count / 10))
        
        logger.info(f"[Pitchdeck] Pricing: {page_count} pages -> {cost} CP")

        # 3. Check Balance
        if not user.token_balance:
            from backend.models import TokenBalance
            # Auto-create if missing (edge case)
            user.token_balance = TokenBalance(user_id=user.id, balance=0)
            logger.warning(f"[Pitchdeck] Created missing TokenBalance for user {user.id}")

        if user.token_balance.balance < cost:
            raise APIError(f'Insufficient tokens. Analysis requires {cost} CP.', status_code=403)
            
        # 4. Deduct Tokens (Optimistic Reservation)
        user.token_balance.balance -= cost
        from backend.database import db_session
        db_session.commit()
        # ---------------------
        
        # Analyze with service
        service = PitchdeckService()
        result = service.analyze_pitch_deck(pdf_bytes)
        
        # Privacy: Log ID instead of email
        logger.info(f"[Pitchdeck] Analysis complete for user {user.id}. Cost: {cost} CP")
        
        return jsonify({
            'success': True,
            'cost_incurred': cost, 
            **result
        })
        
    except ValueError as e:
        # Validation errors from service
        raise APIError(str(e), status_code=400)
    except TimeoutError as e:
        raise APIError(str(e), status_code=504)
    except APIError:
        raise
    except Exception as e:
        logger.error(f"[Pitchdeck] Analysis error: {e}", exc_info=True)
        raise APIError('Analysis failed. Please try again.', status_code=500)


@pitchdeck_bp.route('/verify-market', methods=['POST'])
@login_required
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
        
        # Validate at least one claim to verify
        if not verifiable_claims and not market_size and not competition:
            raise APIError('No claims to verify', status_code=400)
        
        user = request.current_user
        logger.info(f"[Pitchdeck] Verify request - claims: {len(verifiable_claims)}, cache: {bool(cache_name)}")
        
        # --- PRICING: 1 CP flat per market verification ---
        from backend.database import db_session
        from backend.models import TokenBalance
        
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance:
            raise APIError('No token balance found', status_code=403)
        
        cost = 1  # Flat rate: 1 CP per verification call
        if token_balance.balance < cost:
            raise APIError(f'Insufficient tokens. Verification requires {cost} CP.', status_code=403)
        
        token_balance.balance -= cost
        db_session.commit()
        
        # Verify claims using service
        try:
            service = PitchdeckService()
            findings = service.verify_market_claims(
                verifiable_claims=verifiable_claims,
                market_size=market_size,
                competition=competition,
                industry=industry,
                cache_name=cache_name
            )
        except Exception as e:
            # Refund on service failure
            token_balance.balance += cost
            db_session.commit()
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


