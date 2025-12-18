"""
Fact-checking routes.
Handles text analysis and fact verification using Gemini API.
"""
from flask import Blueprint, request, jsonify
import logging
import time
from datetime import datetime
import json

from backend.database import db_session
from backend.models import FactCheck, TokenBalance
from backend.routes.auth import login_required
from backend.error_handlers import APIError
from backend.services import get_gemini_service
from backend.config import Config

logger = logging.getLogger(__name__)

factcheck_bp = Blueprint('factcheck', __name__, url_prefix='/api/factcheck')





@factcheck_bp.route('/analyze', methods=['POST'])
@login_required
def analyze_claim():
    """Analyze text for factual claims."""
    start_time = time.time()
    
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        text = data.get('text')
        
        if not text or not text.strip():
            raise APIError('No text provided')
        
        text = text.strip()
        user = request.current_user
        
        # Calculate word count
        word_count = len(text.split())
        
        # Get token balance
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance:
            # Should not happen for authenticated users usually, but handle it
            raise APIError('No token balance found', status_code=403)

        # FAIR PRICING LOGIC: Accumulate words
        # 1 CP per 250 words. calculate total unbilled words
        current_unbilled = token_balance.unbilled_words or 0
        total_unbilled = current_unbilled + word_count
        
        # Calculate tokens to deduct
        token_cost = total_unbilled // 250
        remainder_words = total_unbilled % 250
        
        # Check balance
        # Users must have at least 0 balance to operate, but if cost > 0 they need sufficient funds
        if token_balance.balance < token_cost:
            raise APIError(
                f'Insufficient tokens. Need {token_cost} CP, you have {token_balance.balance} CP',
                status_code=403
            )
        
        # Deduct tokens and update unbilled words
        token_balance.balance -= token_cost
        token_balance.unbilled_words = remainder_words
        token_balance.last_updated = datetime.utcnow()
        
        # Get optional context and smart_agent flag
        context = data.get('context')
        smart_agent = data.get('smart_agent', False)
        
        # Calculate effective text for analysis (with context if provided)
        analysis_text = text
        if context:
            analysis_text = f"[Context: {context}]\n\n{text}"
            logger.info(f"Using context window: {context[:50]}...")
        
        logger.info(f"Analyzing claim for user {user.email}: {text[:100]}... (Base cost: {token_cost} CP, Unbilled: {remainder_words})")
        
        # Get global Gemini service
        gemini_service = get_gemini_service()
        
        # Smart Agent: Pre-analysis to identify claims (if enabled)
        if smart_agent:
            try:
                # First pass: identify distinct claims
                claims_result = gemini_service.identify_claims(text)
                if claims_result and len(claims_result) > 1:
                    logger.info(f"Smart Agent identified {len(claims_result)} distinct claims")
                    # Double cost only on successful pre-analysis
                    token_cost *= 2
                    logger.info(f"Smart Agent successful - 2x token cost applied: {token_cost} CP")
                    # For now, we still send as one analysis but with claim separation hints
                    analysis_text = f"[Pre-analysis: {len(claims_result)} claims identified]\n\n{analysis_text}"
            except Exception as e:
                logger.warning(f"Smart Agent pre-analysis failed, continuing with standard cost: {e}")
        
        # Record start time
        start_time = time.time()
        
        # Analyze with Gemini
        try:
            result = gemini_service.analyze_claim(analysis_text)
        except Exception as e:
            # Refund tokens if analysis fails
            logger.error(f"Gemini analysis failed: {e}", exc_info=True)
            token_balance.balance += token_cost
            token_balance.unbilled_words = current_unbilled
            db_session.commit()
            raise APIError('Analysis temporarily unavailable. Please try again.')
        
        # Calculate processing time
        processing_time = time.time() - start_time
        
        # Save fact check to database
        fact_check = FactCheck(
            user_id=user.id,
            claim_text=text,
            word_count=word_count,
            tokens_used=token_cost,
            is_claim=result['is_claim'],
            verdict=result['verdict'],
            explanation=result['explanation'],
            fallacy=result.get('fallacy'),
            sources=json.dumps(result.get('sources', [])),
            timestamp=datetime.utcnow(),
            processing_time=processing_time
        )
        db_session.add(fact_check)
        
        db_session.commit()
        
        logger.info(f"Fact check completed. Verdict: {result['verdict']}, Cost: {token_cost} CP, Time: {processing_time:.2f}s")
        
        return jsonify({
            'success': True,
            'result': {
                'id': fact_check.id,
                'is_claim': result['is_claim'],
                'verdict': result['verdict'],
                'explanation': result['explanation'],
                'fallacy': result.get('fallacy'),
                'sources': result.get('sources', []),
                'tokens_used': token_cost,
                'word_count': word_count,
                'processing_time': processing_time
            },
            'new_balance': token_balance.balance
        })
        
    except APIError:
        db_session.rollback()
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Fact check error: {e}", exc_info=True)
        raise APIError('Failed to analyze claim')


@factcheck_bp.route('/history', methods=['GET'])
@login_required
def get_fact_check_history():
    """Get user's fact-check history."""
    user = request.current_user
    
    # Get limit from query params
    limit = request.args.get('limit', 50, type=int)
    limit = min(limit, 100)  # Cap at 100
    
    # Get fact checks
    fact_checks = db_session.query(FactCheck).filter_by(
        user_id=user.id
    ).order_by(
        FactCheck.timestamp.desc()
    ).limit(limit).all()
    
    return jsonify({
        'success': True,
        'fact_checks': [
            {
                'id': fc.id,
                'claim_text': fc.claim_text,
                'verdict': fc.verdict,
                'explanation': fc.explanation,
                'fallacy': fc.fallacy,
                'sources': json.loads(fc.sources) if fc.sources else [],
                'tokens_used': fc.tokens_used,
                'timestamp': fc.timestamp.isoformat()
            }
            for fc in fact_checks
        ]
    })


@factcheck_bp.route('/identify-claims', methods=['POST'])
@login_required
def identify_claims():
    """Smart Agent: Identify distinct claims in text before fact-checking."""
    try:
        data = request.get_json()
        
        if not data or not data.get('text'):
            raise APIError('No text provided')
        
        text = data['text'].strip()
        user = request.current_user
        
        # Get global Gemini service
        gemini_service = get_gemini_service()
        
        # Identify claims
        logger.info(f"Smart Agent identifying claims for user {user.email}: {text[:100]}...")
        claims = gemini_service.identify_claims(text)
        
        logger.info(f"Smart Agent found {len(claims)} claims")
        
        return jsonify({
            'success': True,
            'claims': claims
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Identify claims error: {e}", exc_info=True)
        raise APIError('Failed to identify claims')

