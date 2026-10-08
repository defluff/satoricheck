"""
Fact-checking routes.
Handles text analysis and fact verification using Gemini API.
"""
from flask import Blueprint, request, jsonify
import hashlib
import json
import logging
import re
import time
from datetime import datetime, UTC, timedelta

from backend.database import db_session
from backend.models import FactCheck, MediaCheck, TokenBalance
from backend.routes.auth import login_required, scheduler_secret_required
from backend.error_handlers import APIError
from backend.services import get_gemini_service
from backend.config import Config
from backend.extensions import limiter

logger = logging.getLogger(__name__)

factcheck_bp = Blueprint('factcheck', __name__, url_prefix='/api/factcheck')

MAX_TEXT_LENGTH = 50000






@factcheck_bp.route('/analyze', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def analyze_claim():
    """Analyze text for factual claims."""
    try:
        data = request.get_json()
        
        if not data:
            raise APIError('No data provided')
        
        text = data.get('text')
        
        if not text or not text.strip():
            raise APIError('No text provided')
        
        text = text.strip()
        if len(text) > MAX_TEXT_LENGTH:
            raise APIError(f'Text exceeds maximum limit of {MAX_TEXT_LENGTH:,} characters.')
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
        
        # Get optional context
        context = data.get('context')
        
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
        
        # Check balance (Checks FINAL calculated cost and requires at least 1 CP to initiate check)
        min_required = max(1, token_cost)
        if token_balance.balance < min_required:
            raise APIError(
                f'Insufficient tokens. Need {min_required} CP, you have {token_balance.balance} CP',
                status_code=403
            )
        
        # Deduct tokens (Deducts FINAL calculated cost)
        token_balance.balance -= token_cost
        token_balance.unbilled_words = remainder_words
        token_balance.last_updated = datetime.now(UTC)
        
        logger.info(f"Analyzing claim for user {user.email}: {text[:100]}... (Cost: {token_cost} CP)")
        
        # Record start time
        start_time = time.time()
        
        # Always use the agentic path (smart_agent is now the default, not a flag)
        try:
            # Check if cache_name is explicitly passed in the request payload
            active_cache = data.get('cache_name')
            try:
                result = gemini_service.analyze_claim(analysis_text, smart_agent=True, cache_name=active_cache)
            except Exception:
                if active_cache:
                    logger.warning(f"Explicit cache {active_cache} failed, retrying without cache")
                    result = gemini_service.analyze_claim(analysis_text, smart_agent=True, cache_name=None)
                else:
                    raise
            
            # If agentic mode was used, 'social' might be in the result already
            # or integrated into the explanation. Check if we need to structure it.
            grok_result = result.get('social_context') # New field from agent?
            
        except Exception as e:
            # Refund tokens if analysis fails
            logger.error(f"Gemini analysis failed: {e}", exc_info=True)
            token_balance.balance += token_cost
            token_balance.unbilled_words = current_unbilled
            db_session.commit()
            raise APIError('Analysis temporarily unavailable. Please try again.', status_code=503)
        
        # Calculate processing time
        processing_time = time.time() - start_time
        
        # Save fact check to database
        source = result.get('source', request.get_json().get('source', 'factcheck'))
        source_id = result.get('source_id', request.get_json().get('source_id'))
        
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
            source=source,
            source_id=source_id,
            timestamp=datetime.now(UTC),
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



@factcheck_bp.route('/create-context-cache', methods=['POST'])
@login_required
def create_context_cache():
    """Pre-create a Gemini context cache for reuse across micro-batches."""
    try:
        data = request.get_json()
        if not data or not data.get('text'):
            raise APIError('No text provided')
        
        text = data['text'].strip()
        
        # Only cache if text is large enough (>4KB)
        if len(text) <= 4000:
            return jsonify({'success': True, 'cache_name': None})
        
        gemini_service = get_gemini_service()
        cache_name = gemini_service.create_cache(text, ttl_minutes=5)
        
        logger.info(f"Pre-created context cache: {cache_name}")
        return jsonify({'success': True, 'cache_name': cache_name})
        
    except APIError:
        raise
    except Exception as e:
        logger.error(f"Cache creation error: {e}", exc_info=True)
        # Non-fatal: frontend can proceed without cache
        return jsonify({'success': True, 'cache_name': None})


@factcheck_bp.route('/analyze-batch', methods=['POST'])
@login_required
@limiter.limit("10 per minute")
def analyze_batch_claims():
    """Analyze multiple claims in a batch with caching."""
    start_time = time.time()
    
    try:
        data = request.get_json()
        if not data or 'claims' not in data:
            raise APIError('No claims provided')
            
        claims = data['claims']
        if not isinstance(claims, list) or not claims:
            raise APIError('Claims must be a non-empty list')
            
        # Hard limit processing size per request to prevent timeouts
        if len(claims) > 20:
             raise APIError('Batch size too large. Maximum 20 claims per request.')
            
        user = request.current_user
        context = data.get('context')
        cache_name = data.get('cache_name')  # Pre-created cache from /create-context-cache
        
        # 1. Cache Lookup
        # We look for RECENT exact matches to avoid stale checks if world events change
        # But for now, simple exact match on text is a good start.
        
        results = []  # Final list of results (cached + new)
        claims_to_process = [] # (original_index, claim_text)
        
        # Helper to find cache
        from sqlalchemy import and_
        
        for i, claim_text in enumerate(claims):
            if not claim_text or not claim_text.strip():
                continue
                
            clean_text = claim_text.strip()
            
            # Check cache
            cached = db_session.query(FactCheck).filter(
                and_(
                    FactCheck.claim_text == clean_text,
                    FactCheck.user_id == user.id,
                )
            ).order_by(FactCheck.timestamp.desc()).first()
            
            if cached:
                logger.info(f"Batch Cache HIT: {clean_text[:30]}...")
                results.append({
                    'index': i,
                    'is_cached': True,
                    'result': {
                        'id': cached.id,
                        'is_claim': cached.is_claim,
                        'verdict': cached.verdict,
                        'explanation': cached.explanation,
                        'fallacy': cached.fallacy,
                        'sources': json.loads(cached.sources) if cached.sources else [],
                        'source_reliability': cached.source_reliability,
                        'tokens_used': 0 # Cached results are free!
                    }
                })
            else:
                claims_to_process.append((i, clean_text))
        
        # 2. Process New Claims
        if claims_to_process:
            logger.info(f"Batch processing {len(claims_to_process)} new claims")
            
            # Calculate cost ONLY for new claims
            texts_to_analyze = [c[1] for c in claims_to_process]
            total_word_count = sum(len(t.split()) for t in texts_to_analyze)
            
            # Token Balance Check
            token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
            if not token_balance:
                raise APIError('No token balance found', status_code=403)
                
            current_unbilled = token_balance.unbilled_words or 0
            total_unbilled = current_unbilled + total_word_count
            
            # Cost calculation
            token_cost = (total_unbilled // Config.WORDS_PER_CP) * Config.TOKENS_PER_CP_UNIT
            remainder_words = total_unbilled % Config.WORDS_PER_CP
            
            # Batch analysis is now the standard path — no cost multiplier
            token_cost = max(1, token_cost) if token_cost > 0 else 0
            
            min_required = max(1, token_cost)
            if token_balance.balance < min_required:
                 raise APIError(f'Insufficient tokens. Need {min_required} CP, you have {token_balance.balance} CP', status_code=403)
                 
            # Deduct
            token_balance.balance -= token_cost
            token_balance.unbilled_words = remainder_words
            token_balance.last_updated = datetime.now(UTC)
            
            # Call API with Context (only reuse cache_name if explicitly provided).
            # Guard against stale caches (expired TTL): retry without cache on failure.
            gemini_service = get_gemini_service()
            try:
                effective_cache = cache_name
                try:
                    api_results = gemini_service.analyze_claims_batch(
                        texts_to_analyze, context=context, cache_name=effective_cache
                    )
                except Exception:
                    if effective_cache:
                        logger.warning(f"Volatile cache {effective_cache} failed, retrying without cache")
                        api_results = gemini_service.analyze_claims_batch(
                            texts_to_analyze, context=context, cache_name=None
                        )
                    else:
                        raise
                
            except Exception as e:
                # Refund
                token_balance.balance += token_cost
                token_balance.unbilled_words = current_unbilled
                db_session.commit()
                logger.error(f"Batch analysis failed: {e}")
                raise APIError("Batch analysis service unavailable")

            # Process API Results
            # Distribute cost roughly evenly for recording purposes? 
            # Or just assign to the batch. We store individual records.
            # We'll assign cost proportional to word count or just split evenly? 
            # Let's split evenly for simplicity, it's metadata only.
            cost_per_item = token_cost / len(api_results) if api_results else 0
            
            # Extract metadata for tracking
            source = data.get('source', 'factcheck')
            source_id = data.get('source_id')

            for idx, res in enumerate(api_results):
                original_index = claims_to_process[idx][0]
                text = claims_to_process[idx][1]
                
                # Save to DB
                fact_check = FactCheck(
                    user_id=user.id,
                    claim_text=text,
                    word_count=len(text.split()),
                    tokens_used=cost_per_item, 
                    is_claim=res.get('is_claim', True),
                    verdict=res.get('verdict', 'COULD_NOT_VERIFY'),
                    explanation=res.get('explanation', ''),
                    fallacy=res.get('fallacy'),
                    sources=json.dumps(res.get('sources', [])),
                    source_reliability=res.get('source_reliability', 'MEDIUM'),
                    source=source,
                    source_id=source_id,
                    timestamp=datetime.now(UTC),
                    processing_time=(time.time() - start_time) / len(api_results)
                )
                db_session.add(fact_check)
                db_session.flush() # Get ID
                
                # NOTE: Grok/social search is now handled INSIDE the agentic loop via
                # the search_social tool. The agent decides when to call it based on
                # claim type (viral, breaking news, quotes). No sequential post-processing.
                # Social context is embedded in the explanation if agent used the tool.

                results.append({
                    'index': original_index,
                    'is_cached': False,
                    'result': {
                        'id': fact_check.id,
                        'is_claim': fact_check.is_claim,
                        'verdict': fact_check.verdict,
                        'explanation': fact_check.explanation,
                        'fallacy': fact_check.fallacy,
                        'sources': res.get('sources', []),
                        'tokens_used': cost_per_item,
                        # Meta-Truth fields
                        'is_quote_claim': res.get('is_quote_claim', False),
                        'quote_attribution': res.get('quote_attribution'),
                        'quote_verified': res.get('quote_verified'),
                        'quote_source': res.get('quote_source'),
                        'meta_truth_verdict': res.get('meta_truth_verdict'),
                        # Social context embedded in explanation if agent used search_social
                        'social': res.get('social_context'),
                    }
                })
                
            db_session.commit()
            
        # Re-sort results to match original order
        results.sort(key=lambda x: x['index'])
        
        # Extract just the result objects
        final_results = [r['result'] for r in results]
        
        return jsonify({
            'success': True,
            'results': final_results,
            'new_balance': db_session.query(TokenBalance).filter_by(user_id=user.id).first().balance
        })

    except APIError:
        db_session.rollback()
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Batch error: {e}", exc_info=True)
        raise APIError('Batch analysis failed')


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
        'fact_checks': [fc.to_dict() for fc in fact_checks]
    })


@factcheck_bp.route('/identify-claims', methods=['POST'])
@login_required
def identify_claims():
    """Smart Agent: Identify distinct claims in text before fact-checking.
    
    Note: This endpoint is unmetered because the 2x Smart Agent multiplier
    on /analyze-batch covers the extra API calls. A balance guard prevents
    abuse by users who call identify without ever proceeding to batch.
    """
    try:
        data = request.get_json()
        
        if not data or not data.get('text'):
            raise APIError('No text provided')
        
        text = data['text'].strip()
        user = request.current_user
        
        # Guard: User must have at least 1 CP to use Smart Agent pipeline
        token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
        if not token_balance or token_balance.balance < 1:
            raise APIError('Insufficient tokens to use Smart Agent', status_code=403)
        
        # Get global Gemini service
        gemini_service = get_gemini_service()
        
        # Identify claims
        logger.info(f"Smart Agent identifying claims for user {user.id}: {text[:100]}...")
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
@limiter.limit("30 per minute")
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
        if len(text) > MAX_TEXT_LENGTH:
            raise APIError(f'Text exceeds maximum limit of {MAX_TEXT_LENGTH:,} characters.')
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
        
        # Check balance - require at least 1 CP to initiate AI detection
        min_required = max(1, token_cost)
        if token_balance.balance < min_required:
            raise APIError(f'Insufficient tokens. Need {min_required} CP, have {token_balance.balance} CP', status_code=402)
        
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
            'detected_register': result.get('detected_register', 'general'),
            'is_short_text': result.get('is_short_text', False),
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


# In-memory session dedup cache. Works for single-worker deployments.
# TODO: Migrate to Redis or DB-backed cache for multi-instance Cloud Run scaling.
#       Duplicates across workers are non-critical (wasted compute, not incorrect behavior).
_STREAM_DEDUPE_CACHE = {}  # session_key -> list of (normalized_hash, timestamp)

def _is_claim_duplicate_in_session(session_key: str, claim_text: str, window_seconds: int = 180) -> bool:
    """Check if normalized claim was already seen in this session's recent window."""
    now = time.time()
    clean = re.sub(r'[^\w\s]', '', claim_text.lower()).strip()
    norm_key = ' '.join(clean.split())
    if not norm_key:
        return True
    norm_hash = hashlib.sha256(norm_key.encode('utf-8')).hexdigest()

    # Evict expired session keys if dictionary grows large
    if len(_STREAM_DEDUPE_CACHE) > 500:
        stale_keys = [k for k, v in _STREAM_DEDUPE_CACHE.items() if not v or (now - v[-1][1] > window_seconds)]
        for k in stale_keys:
            _STREAM_DEDUPE_CACHE.pop(k, None)

    entries = _STREAM_DEDUPE_CACHE.setdefault(session_key, [])
    # Evict expired entries older than window_seconds
    entries[:] = [(h, t) for (h, t) in entries if now - t < window_seconds]

    for (h, _) in entries:
        if h == norm_hash:
            return True

    # Limit maximum entries per session
    if len(entries) > 200:
        entries.pop(0)

    entries.append((norm_hash, now))
    return False


ALLOWED_STREAM_AUDIO_MIMES = frozenset({
    'audio/webm', 'audio/wav', 'audio/mp3', 'audio/mpeg',
    'audio/ogg', 'audio/m4a', 'audio/x-m4a', 'audio/aac'
})


@factcheck_bp.route('/stream-audio', methods=['POST'])
@login_required
@limiter.limit("60 per minute")
def stream_audio():
    """Ingest live audio slice, transcribe, extract falsifiable claims, and triage.

    Single-pass multimodal ingestion using Gemini Flash.
    Calculates word count accumulation for CP billing.
    Filters rhetorical hyperbole and conversational banter.
    Deduplicates claims across overlapping chunk boundaries.
    """
    user = request.current_user
    start_time = time.time()

    # 1. Token balance check - require at least 1 CP to stream
    token_balance = db_session.query(TokenBalance).filter_by(user_id=user.id).first()
    if not token_balance or token_balance.balance < 1:
        current_bal = token_balance.balance if token_balance else 0
        raise APIError(
            'Insufficient tokens. Minimum 1 CP required for live stream verification.',
            status_code=403,
            payload={'code': 'INSUFFICIENT_FUNDS', 'balance': current_bal}
        )

    # 2. Extract audio bytes and MIME type
    audio_bytes = None
    mime_type = 'audio/webm'

    if 'audio' in request.files:
        audio_file = request.files['audio']
        audio_bytes = audio_file.read()
        if audio_file.content_type:
            mime_type = audio_file.content_type.split(';')[0].strip().lower()
    elif 'file' in request.files:
        audio_file = request.files['file']
        audio_bytes = audio_file.read()
        if audio_file.content_type:
            mime_type = audio_file.content_type.split(';')[0].strip().lower()
    else:
        # Check raw body
        raw_data = request.get_data()
        if raw_data:
            audio_bytes = raw_data
            if request.content_type:
                mime_type = request.content_type.split(';')[0].strip().lower()

    if not audio_bytes:
        raise APIError('No audio data provided in request', status_code=400)

    # Allow custom mime_type override from form
    form_mime = request.form.get('mime_type')
    if form_mime:
        mime_type = form_mime.split(';')[0].strip().lower()

    if mime_type not in ALLOWED_STREAM_AUDIO_MIMES:
        raise APIError(
            f'Unsupported audio format: {mime_type}. Supported: webm, wav, mp3, mpeg, ogg, m4a.',
            status_code=400
        )

    session_id = (
        request.form.get('session_id') or
        request.headers.get('X-Stream-Session-Id') or
        f"user_{user.id}"
    )
    context = request.form.get('context')
    auto_verify_param = request.form.get('auto_verify', 'true').strip().lower()
    auto_verify = auto_verify_param in ('1', 'true', 'yes')

    gemini_service = get_gemini_service()

    try:
        # 3. Multimodal single-pass transcription + claim extraction
        ingest_result = gemini_service.transcribe_and_extract_claims_from_audio(
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            context=context
        )

        transcript = ingest_result.get('transcript', '')
        raw_claims = ingest_result.get('claims', [])
        word_count = ingest_result.get('word_count', 0)

        # 4. Word-accumulation token billing
        current_unbilled = token_balance.unbilled_words or 0
        total_unbilled = current_unbilled + word_count
        token_cost = (total_unbilled // Config.WORDS_PER_CP) * Config.TOKENS_PER_CP_UNIT
        remainder_words = total_unbilled % Config.WORDS_PER_CP

        if token_cost > 0:
            token_balance.balance = max(0, token_balance.balance - token_cost)
            token_balance.unbilled_words = remainder_words
        else:
            token_balance.unbilled_words = total_unbilled

        token_balance.last_updated = datetime.now(UTC)
        db_session.commit()

        # 5. Sliding window deduplication
        candidate_claims = []
        for c in raw_claims:
            claim_text = c.get('claim', '').strip() if isinstance(c, dict) else str(c).strip()
            if not claim_text:
                continue
            if not _is_claim_duplicate_in_session(session_id, claim_text):
                candidate_claims.append({
                    'claim': claim_text,
                    'timestamp': c.get('timestamp') if isinstance(c, dict) else None,
                    'quote': c.get('quote') if isinstance(c, dict) else None
                })

        # 6. Flash Triage (drop hyperbole and banter)
        verified_claims = []
        if candidate_claims:
            claim_texts = [c['claim'] for c in candidate_claims]
            triage_results = gemini_service.triage_for_stream(claim_texts, stream_context=context)

            claims_to_verify = []
            for idx, c_item in enumerate(candidate_claims):
                triage_info = triage_results[idx] if idx < len(triage_results) else {}
                priority = str(triage_info.get('priority', 'NORMAL')).lower()
                strategy = triage_info.get('strategy', 'SEARCH_VERIFY')
                is_hyperbole = bool(triage_info.get('is_hyperbole', False))

                c_item['priority'] = priority
                c_item['strategy'] = strategy
                c_item['is_hyperbole'] = is_hyperbole

                # Filter: SKIP or rhetorical hyperbole without empirical anchors
                if priority != 'skip' and not is_hyperbole:
                    claims_to_verify.append(c_item)

            # 7. Verification: Cross-User Global Cache Check & Agentic Verification
            if claims_to_verify and auto_verify:
                uncached_claims = []
                recent_cutoff = datetime.now(UTC) - timedelta(hours=48)

                for c_item in claims_to_verify:
                    clean_c = c_item['claim']
                    # Global cache lookup: matches any recent check across all users
                    cached_fc = db_session.query(FactCheck).filter(
                        FactCheck.claim_text == clean_c,
                        FactCheck.timestamp >= recent_cutoff
                    ).order_by(FactCheck.timestamp.desc()).first()

                    if cached_fc:
                        logger.info(f"Stream verification Global Cache HIT: {clean_c[:40]}")
                        verified_claims.append({
                            'claim': clean_c,
                            'timestamp': c_item.get('timestamp'),
                            'verdict': cached_fc.verdict,
                            'explanation': cached_fc.explanation,
                            'fallacy': cached_fc.fallacy,
                            'sources': json.loads(cached_fc.sources) if cached_fc.sources else [],
                            'source_reliability': cached_fc.source_reliability,
                            'is_cached': True
                        })
                    else:
                        uncached_claims.append(c_item)

                if uncached_claims:
                    try:
                        batch_res = gemini_service.analyze_claims_batch(
                            [c['claim'] for c in uncached_claims],
                            context=context
                        )
                        fc_records_to_persist = []
                        for i, c_item in enumerate(uncached_claims):
                            verdict_info = batch_res[i] if i < len(batch_res) else {}
                            v_record = {
                                'claim': c_item['claim'],
                                'timestamp': c_item.get('timestamp'),
                                'verdict': verdict_info.get('verdict', 'COULD_NOT_VERIFY'),
                                'explanation': verdict_info.get('explanation', ''),
                                'fallacy': verdict_info.get('fallacy'),
                                'sources': verdict_info.get('sources', []),
                                'source_reliability': verdict_info.get('source_reliability', 'MEDIUM'),
                                'is_cached': False
                            }
                            verified_claims.append(v_record)

                            # Stage for batch persistence to seed global cache
                            fc_records_to_persist.append(FactCheck(
                                user_id=user.id,
                                claim_text=c_item['claim'],
                                is_claim=verdict_info.get('is_claim', True),
                                verdict=v_record['verdict'],
                                explanation=v_record['explanation'],
                                fallacy=v_record['fallacy'],
                                sources=json.dumps(v_record['sources']),
                                source_reliability=v_record['source_reliability'],
                                processing_time=time.time() - start_time
                            ))

                        # Single commit for all verified claims
                        if fc_records_to_persist:
                            try:
                                db_session.add_all(fc_records_to_persist)
                                db_session.commit()
                            except Exception as persist_err:
                                db_session.rollback()
                                logger.warning(f"Could not persist stream factchecks: {persist_err}")
                    except Exception as verify_err:
                        logger.error(f"Live verification batch failed: {verify_err}")

        elapsed = time.time() - start_time
        logger.info(
            f"Stream slice processed for {user.email}: "
            f"{word_count} words, {len(candidate_claims)} candidate claims, "
            f"{len(verified_claims)} verified in {elapsed:.2f}s"
        )

        return jsonify({
            'success': True,
            'transcript': transcript,
            'word_count': word_count,
            'candidate_claims': candidate_claims,
            'verified_claims': verified_claims,
            'tokens_used': token_cost,
            'balance': token_balance.balance,
            'processing_time': elapsed
        })

    except APIError:
        raise
    except Exception as e:
        db_session.rollback()
        logger.error(f"Stream audio processing error: {e}", exc_info=True)
        raise APIError('Failed to process stream audio slice', status_code=500)


@factcheck_bp.route('/cleanup-expired', methods=['POST'])
@scheduler_secret_required
def cleanup_expired_checks():
    """Purge fact-checks and media-checks older than 7 days.

    Cloud Scheduler endpoint for automated GDPR data retention.
    Requires X-Scheduler-Secret header matching Config.SCHEDULER_SECRET.
    """
    cutoff = datetime.now(UTC) - timedelta(days=7)
    try:
        fc_count = db_session.query(FactCheck).filter(FactCheck.timestamp < cutoff).delete()
        mc_count = db_session.query(MediaCheck).filter(MediaCheck.timestamp < cutoff).delete()
        db_session.commit()
        logger.info(f"Cleanup: purged {fc_count} fact-checks, {mc_count} media-checks older than 7 days")
        return jsonify({
            'success': True,
            'deleted_fact_checks': fc_count,
            'deleted_media_checks': mc_count
        }), 200
    except Exception as e:
        db_session.rollback()
        logger.error(f"Cleanup expired job failed: {e}", exc_info=True)
        raise APIError('Database cleanup failed', status_code=500)



