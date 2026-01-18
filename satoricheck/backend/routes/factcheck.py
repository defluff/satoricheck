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
        
        # Get optional context and smart_agent flag
        context = data.get('context')
        smart_agent = data.get('smart_agent', False)
        
        # Calculate effective text for analysis
        analysis_text = text
        if context:
            analysis_text = f"[Context: {context}]\n\n{text}"
            logger.info(f"Using context window: {context[:50]}...")
            
        # Get global Gemini service (needed for pre-analysis)
        gemini_service = get_gemini_service()

        # Calculate base tokens (Word Accumulation Model)
        token_cost = (total_unbilled // Config.WORDS_PER_CP) * Config.TOKENS_PER_CP_UNIT
        remainder_words = total_unbilled % Config.WORDS_PER_CP
        
        # Smart Agent: Apply multiplier BEFORE deduction
        if smart_agent:
            token_cost *= 2
            logger.info(f"Smart Agent enabled - 2x cost applied: {token_cost} CP")
            
            try:
                # Pre-analysis to identify distinct claims
                claims_result = gemini_service.identify_claims(text)
                if claims_result and len(claims_result) > 1:
                    logger.info(f"Smart Agent identified {len(claims_result)} distinct claims")
                    analysis_text = f"[Pre-analysis: {len(claims_result)} claims identified]\n\n{analysis_text}"
            except Exception as e:
                logger.warning(f"Smart Agent pre-analysis failed, continuing with enhanced analysis: {e}")
        
        # Check balance (Checks FINAL calculated cost)
        if token_balance.balance < token_cost:
            raise APIError(
                f'Insufficient tokens. Need {token_cost} CP, you have {token_balance.balance} CP',
                status_code=403
            )
        
        # Deduct tokens (Deducts FINAL calculated cost)
        token_balance.balance -= token_cost
        token_balance.unbilled_words = remainder_words
        token_balance.last_updated = datetime.utcnow()
        
        logger.info(f"Analyzing claim for user {user.email}: {text[:100]}... (Cost: {token_cost} CP)")
        
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
        
        # Grok social context (Smart Mode only, with graceful degradation)
        # NOTE: Check triggers on BOTH the processed text AND the original (analysis_text may contain context with triggers)
        grok_result = None
        logger.info(f"Grok check: smart_agent={smart_agent}, GROK_ENABLED={Config.GROK_ENABLED}")
        if smart_agent and Config.GROK_ENABLED:
            from backend.services.grok_service import should_fire_grok, get_grok_service
            # Check triggers on both the direct claim AND the full analysis text (may include original with @handles etc)
            trigger = should_fire_grok(text, result) or should_fire_grok(analysis_text, result)
            logger.info(f"Grok trigger check: {trigger} for text: {text[:50]}...")
            if trigger:
                try:
                    grok_service = get_grok_service()
                    # Search using the direct claim text for better relevance
                    grok_result = grok_service.search_social(text)
                    logger.info(f"Grok social search: found={grok_result.get('found', False)}")
                except Exception as e:
                    logger.warning(f"Grok API failed (graceful degradation): {e}")
                    # Continue with Gemini-only result - user still gets value
        
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
            source_reliability=result.get('source_reliability', 'MEDIUM'),
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
                'source_reliability': result.get('source_reliability', 'MEDIUM'),
                # Meta-Truth fields for quote claims
                'is_quote_claim': result.get('is_quote_claim', False),
                'quote_attribution': result.get('quote_attribution'),
                'quote_verified': result.get('quote_verified'),
                'quote_source': result.get('quote_source'),
                'meta_truth_verdict': result.get('meta_truth_verdict', result['verdict']),
                # Social context (Grok)
                'social': grok_result,
                # Usage stats
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


@factcheck_bp.route('/analyze-ai', methods=['POST'])
@login_required
def analyze_ai():
    """Analyze text for AI-generation likelihood (like GPT Zero)."""
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
        
        # Minimum text length check
        word_count = len(text.split())
        if word_count < 20:
            raise APIError('Text too short. Please provide at least 20 words for accurate AI detection.')
        
        # Get token balance
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance:
            raise APIError('No token balance found', status_code=403)
        
        # Same token logic as fact-checking (1 CP per 250 words)
        current_unbilled = token_balance.unbilled_words or 0
        total_unbilled = current_unbilled + word_count
        
        # Calculate tokens
        token_cost = (total_unbilled // Config.WORDS_PER_CP) * Config.TOKENS_PER_CP_UNIT
        remainder_words = total_unbilled % Config.WORDS_PER_CP
        
        # Check balance
        if token_cost > 0 and token_balance.balance < token_cost:
            raise APIError(f'Insufficient tokens. Need {token_cost} CP, have {token_balance.balance}', status_code=402)
        
        # Get Gemini service and analyze
        gemini_service = get_gemini_service()
        result = gemini_service.analyze_ai_content(text)
        
        # Deduct tokens if applicable
        if token_cost > 0:
            token_balance.balance -= token_cost
            token_balance.unbilled_words = remainder_words
            logger.info(f"AI Detection deducted {token_cost} CP from user {user.email}")
        else:
            token_balance.unbilled_words = total_unbilled
        
        db_session.commit()
        
        elapsed = time.time() - start_time
        logger.info(f"AI Detection completed for user {user.email}: {result['ai_probability']}% in {elapsed:.2f}s")
        
        return jsonify({
            'success': True,
            'ai_probability': result['ai_probability'],
            'confidence': result['confidence'],
            'ai_indicators': result['ai_indicators'],
            'human_indicators': result['human_indicators'],
            'explanation': result['explanation'],
            'tokens_used': token_cost,
            'new_balance': token_balance.balance
        })
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"AI Detection error: {e}", exc_info=True)
        raise APIError('AI detection service unavailable')

