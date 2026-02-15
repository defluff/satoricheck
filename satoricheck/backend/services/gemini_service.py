"""
Google Gemini API integration service.
"""
import json
import logging
import re
import requests
from enum import Enum
from backend.config import Config

logger = logging.getLogger(__name__)


# =============================================================================
# FUNNEL ARCHITECTURE TYPES (for future streaming expansion)
# =============================================================================
class ClaimPriority(str, Enum):
    """Priority levels for stream claim processing.
    
    Used by the Funnel architecture to prioritize claims from live streams.
    """
    IMMEDIATE = "immediate"  # Verify now (high-virality, breaking news)
    NORMAL = "normal"        # Verify during stream
    DEFERRED = "deferred"    # Batch after stream ends
    SKIP = "skip"            # Duplicate or trivial claim


class GeminiService:
    """Service for interacting with Google Gemini API."""
    
    # Models
    MODEL_SMART = "gemini-3-flash-preview"
    MODEL_FAST = "gemini-3-flash-preview"
    MODEL_TRIAGE = "gemini-3-flash-preview"  # Fast triage with low token budget
    
    # Timeouts (seconds)
    TIMEOUT_SMART = 30
    TIMEOUT_FAST = 30
    TIMEOUT_TRIAGE = 10  # Triage should be quick
    
    # Batch sizing: conservative limit to stay within maxOutputTokens (8192)
    MAX_CLAIMS_PER_PROMPT = 8
    
    def __init__(self):
        """Initialize Gemini service."""
        if not Config.GEMINI_API_KEY:
            logger.error("GEMINI_API_KEY is not set!")
        else:
            logger.info("✓ Gemini API key configured")
        self.api_key = Config.GEMINI_API_KEY
    
    def _get_api_url(self, model):
        """Get API URL for a specific model."""
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"
    
    def _validate_url(self, url):
        """Check if a URL is reachable (returns 200). Quick HEAD request with timeout."""
        if not url or not isinstance(url, str):
            return False
        if not url.startswith('http://') and not url.startswith('https://'):
            return False
        try:
            # Use HEAD request for speed (no body download)
            response = requests.head(url, timeout=3, allow_redirects=True, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; SatoriCheck/1.0)'
            })
            return response.status_code < 400
        except Exception:
            return False
    
    def _validate_sources(self, sources):
        """Filter sources to only include live URLs. Enforces 1-5 sources."""
        # Note: This method only does HTTP checks, so no model inference needed here.
        if not sources or not isinstance(sources, list):
            return []
        
        valid_sources = []
        for url in sources[:10]:  # Check up to 10 to find 5 valid
            if self._validate_url(url):
                valid_sources.append(url)
                if len(valid_sources) >= 5:  # Max 5 valid sources
                    break
        
        return valid_sources
    
    
    def _analyze_claim_safe(self, text, smart_agent=False, cache_name=None):
        """
        Thread-safe wrapper for analyze_claim.
        Ensures database session is removed after execution.
        """
        try:
            return self.analyze_claim(text, smart_agent, cache_name)
        finally:
            # Critical: Remove thread-local session to prevent connection leaks
            from backend.database import db_session
            db_session.remove()

    def analyze_claim(self, text, smart_agent=False, cache_name=None):
        """
        Public entry point for claim analysis.
        Args:
            text: The text to analyze
            smart_agent: If True, uses Agentic Loop with Thinking Mode.
            cache_name: Optional resource name of a Gemini Context Cache.
        """
        if smart_agent:
            return self._analyze_claim_agentic(text, cache_name)
        return self._analyze_claim_standard(text)

    def _analyze_claim_agentic(self, text, cache_name=None):
        """
        Agentic analysis loop with Thinking Mode and Tool Use.
        Includes internal retries for robustness.
        Supports Context Caching if cache_name is provided.
        """
        try:
            from backend.services.grok_service import get_grok_service
            grok = get_grok_service()
            tools = [grok.get_tool_definition()]
            
            tool_config = {"function_declarations": tools}
            
            # Context Caching: If cache_name is provided, backend uses it for context
            
            prompt = self._build_fact_check_prompt(text, agentic=True)
            
            conversation_history = [
                {"role": "user", "parts": [{"text": prompt}]}
            ]
            
            max_turns = 5 
            current_turn = 0
            
            log_prefix = f"AGENTIC (Thinking{' + Cache' if cache_name else ''})"
            logger.info(f"Starting {log_prefix} analysis for: {text[:50]}...")
            
            while current_turn < max_turns:
                current_turn += 1
                
                # Payload with Thinking Config AND Optional Cache
                payload = {
                    "contents": conversation_history,
                    "tools": [tool_config],
                    "generationConfig": {
                        # Enable Thinking Mode
                        "thinkingConfig": {
                            "includeThoughts": True
                        }
                    } 
                }
                
                if cache_name:
                    payload["cachedContent"] = cache_name
                
                # INTERNAL RETRY LOOP for this specific turn
                # This prevents the whole chain from breaking due to a single 503 or Timeout
                turn_response = None
                turn_exception = None
                
                for attempt in range(2): # Try twice
                    try:
                        response = requests.post(
                            self._get_api_url(self.MODEL_SMART),
                            json=payload,
                            headers={'Content-Type': 'application/json'},
                            timeout=self.TIMEOUT_SMART
                        )
                        response.raise_for_status()
                        turn_response = response.json()
                        break # Success
                    except Exception as e:
                        logger.warning(f"Agentic turn {current_turn} failed (attempt {attempt+1}): {e}")
                        turn_exception = e
                        import time
                        time.sleep(1) # Brief pause
                
                if not turn_response:
                    raise turn_exception or Exception("Agentic turn failed after retries")

                data = turn_response
                
                if 'candidates' not in data or not data['candidates']:
                    raise ValueError("No candidates in response")
                    
                candidate = data['candidates'][0]
                content = candidate.get('content', {})
                parts = content.get('parts', [])
                
                model_reply_parts = []
                function_calls = []
                final_json_text = ""
                
                # Parse constraints & Thoughts
                for part in parts:
                    # Log Thoughts
                    if part.get('thought'): 
                        # logger.debug(f"🤖 THOUGHT: {part.get('text')}") 
                        pass # Keep logs clean unless debugging
                    
                    # Capture Text
                    if 'text' in part:
                        # Accumulate all text, we will extract JSON block later
                        text_val = part['text']
                        if not part.get('thought'): # Don't add thoughts to final text
                            final_json_text += text_val
                    
                    # Capture Function Calls
                    if 'functionCall' in part:
                        function_calls.append(part['functionCall'])
                    
                    # Add strictly to history to maintain state
                    model_reply_parts.append(part)

                conversation_history.append({
                    "role": "model",
                    "parts": model_reply_parts
                })
                
                # 1. Execute Tools
                if function_calls:
                    logger.info(f"Agent decided to call {len(function_calls)} tools.")
                    for fc in function_calls:
                        fn_name = fc.get('name')
                        fn_args = fc.get('args', {})
                        
                        tool_result = {}
                        if fn_name == 'search_social':
                            query = fn_args.get('query')
                            logger.info(f"🛠️ Executing Tool: search_social(query='{query}')")
                            tool_result = grok.search_social(query)
                        else:
                            tool_result = {"error": f"Unknown tool: {fn_name}"}
                            
                        # Add tool response
                        conversation_history.append({
                            "role": "function",
                            "parts": [{
                                "functionResponse": {
                                    "name": fn_name,
                                    "response": tool_result
                                }
                            }]
                        })
                    # Loop continues to let model see result and output next step
                    continue

                # 2. Final Result Extraction (Robust)
                if final_json_text and "verdict" in final_json_text:
                    # Robust extraction: Look for markdown code blocks first, then braces

                    clean_json = final_json_text.strip()
                    
                    # 1. Try to extract from Markdown code blocks
                    json_block_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', clean_json, re.IGNORECASE)
                    if json_block_match:
                        clean_json = json_block_match.group(1)
                    else:
                        # 2. Fallback: Find outermost curly braces
                        # This avoids cutting off if there are braces in the preamble text like "Here is the JSON { ... }"
                        # We assume the LARGEST brace pair is the JSON object
                        start_idx = clean_json.find('{')
                        end_idx = clean_json.rfind('}')
                        if start_idx != -1 and end_idx != -1:
                            clean_json = clean_json[start_idx:end_idx+1]
                    
                    # Validate parsing logic
                    try:
                        # Check if it parses
                        _ = json.loads(clean_json)
                        # Synthesize final parsing payload
                        dummy_response = {"candidates": [{"content": {"parts": [{"text": clean_json}]}}]}
                        return self._parse_response(dummy_response)
                    except json.JSONDecodeError:
                        logger.warning(f"Agent emitted invalid JSON: {clean_json[:50]}...")
                        # If parsing fails, we could force another turn or just fall through to fallback
                
                # 3. Fallback (Model stopped but no JSON and no Tool?)
                logger.warning("Agent stopped without JSON or Tool call. Forcing another turn...")
                if current_turn >= max_turns:
                    break
            
            # If we exit loop without result
            raise Exception("Agent exceeded max turns without final answer")

        except Exception as e:
            logger.error(f"Agentic loop error: {e}")
            # Fallback to standard
            return self._analyze_claim_standard(text)

    def _analyze_claim_standard(self, text):
        """
        Analyze text for factual claims using Gemini API (Smart Model).
        
        Args:
            text: The text to analyze
            
        Returns:
            Dict containing analysis results
        """
        # 1. Check Cache (Exact Match)
        try:
            from backend.database import db_session
            from backend.models import FactCheck
            
            cached = db_session.query(FactCheck).filter(
                FactCheck.claim_text == text
            ).order_by(FactCheck.timestamp.desc()).first()
            
            if cached:
                logger.info(f"Cache hit for text: {text[:50]}...")
                # Return cached sources directly - they were validated when stored
                sources = json.loads(cached.sources) if cached.sources else []
                return {
                    "is_claim": cached.is_claim,
                    "verdict": cached.verdict,
                    "explanation": cached.explanation,
                    "fallacy": cached.fallacy,
                    "sources": sources,
                    "source_reliability": cached.source_reliability
                }
        except Exception as e:
            logger.warning(f"Cache lookup failed: {e}")
            # Continue to API if cache check fails

        # 2. Call API with Retries
        max_retries = 3
        retry_delay = 1
        last_exception = None

        for attempt in range(max_retries):
            try:
                prompt = self._build_fact_check_prompt(text)
                
                logger.info(f"Sending fact-check request (attempt {attempt + 1}) for text: {text[:100]}...")
                
                # Build request payload
                payload = {
                    "contents": [{
                        "parts": [{"text": prompt}]
                    }],
                    "tools": [{"google_search": {}}]
                }
                
                # Make REST API call
                response = requests.post(
                    self._get_api_url(self.MODEL_SMART),
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=self.TIMEOUT_SMART
                )
                response.raise_for_status()
                response_data = response.json()
                
                # Parse the response
                result = self._parse_response(response_data)
                
                logger.info(f"Fact-check result: {result['verdict']}")
                
                return result
                
            except requests.exceptions.Timeout as e:
                error_msg = f"Request timeout after 30s (Attempt {attempt + 1})"
                logger.error(f"Gemini API timeout: {error_msg}", exc_info=True)
                last_exception = "Network timeout. Please try again."
                
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
            
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Network connection failed (Attempt {attempt + 1})"
                logger.error(f"Gemini API connection error: {error_msg}", exc_info=True)
                last_exception = "Network connection failed. Please check your internet connection."
                
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
            
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response else None
                
                # Log full error details
                logger.error(f"Gemini API HTTP {status_code} error (Attempt {attempt + 1}): {str(e)}", exc_info=True)
                
                if status_code == 429:
                    last_exception = "Rate limit reached."
                elif status_code and 400 <= status_code < 500:
                    last_exception = f"API request error (code {status_code})."
                elif status_code and 500 <= status_code < 600:
                    last_exception = f"Gemini service temporarily unavailable (code {status_code})."
                else:
                    last_exception = "API request failed."
                
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
            
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Gemini API response as JSON (Attempt {attempt + 1}): {str(e)}", exc_info=True)
                last_exception = "Invalid response from AI service."
                
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2
            
            except Exception as e:
                # Catch any other unexpected errors
                logger.error(f"Unexpected Gemini API error (Attempt {attempt + 1}): {str(e)}", exc_info=True)
                last_exception = "Unexpected error occurred."
                
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2

        # All retries exhausted
        logger.error(f"Gemini API failed after {max_retries} attempts. Last error: {last_exception}")
        
        # Return graceful fallback for rate limit
        if "Rate limit" in str(last_exception):
             return {
                'is_claim': True,
                'verdict': 'COULD_NOT_VERIFY',
                'explanation': 'System is experiencing high traffic. Please try again in a few minutes.',
                'fallacy': None,
                'sources': []
            }
            
        # Generic error for user
        raise Exception("Fact-check service temporarily unavailable. Please try again.")
    
    
    def create_cache(self, content: str, ttl_minutes: int = 10) -> str:
        """
        Create a Context Cache for Gemini.
        
        Args:
            content: The text content to cache
            ttl_minutes: Time-to-live in minutes (default 10)
            
        Returns:
            str: The resource name of the cache (e.g., 'cachedContents/123...')
        """
        url = "https://generativelanguage.googleapis.com/v1beta/cachedContents?key=" + self.api_key
        
        payload = {
            "model": f"models/{self.MODEL_SMART}",
            "contents": [{
                "parts": [{"text": content}],
                "role": "user"
            }],
            "ttl": f"{ttl_minutes * 60}s"
        }
        
        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            # The 'name' field contains the resource ID
            cache_name = data.get('name')
            logger.info(f"Created Gemini Cache: {cache_name} (TTL: {ttl_minutes}m)")
            return cache_name
            
        except Exception as e:
            logger.error(f"Failed to create cache: {e}")
            raise

    def generate_with_cache(self, cache_name: str, prompt: str) -> dict:
        """
        Generate content referencing an existing Context Cache.
        
        Args:
            cache_name: The resource name of the cache
            prompt: The new user prompt
            
        Returns:
            dict: Parsed response result
        """
        # Note: URL format for cached requests is slightly different or standard
        # We target the model, but payload points to cache
        url = self._get_api_url(self.MODEL_SMART)
        
        payload = {
            "contents": [{
                "parts": [{"text": prompt}],
                "role": "user"
            }],
            "cachedContent": cache_name
        }
        
        try:
            response = requests.post(
                url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=self.TIMEOUT_SMART
            )
            response.raise_for_status()
            
            # Parse result similarly to standard response
            # But we just return a simple dict for this helper for now
            data = response.json()
            
            if 'candidates' in data and data['candidates']:
                text = data['candidates'][0]['content']['parts'][0]['text']
                return {'text': text, 'raw': data}
            return {'text': '', 'raw': data}
            
        except Exception as e:
            logger.error(f"Failed to generate with cache: {e}")
            raise

    def _build_fact_check_prompt(self, text, agentic=False):
        """Build the fact-checking prompt with Meta-Truth awareness."""
        return f"""You are an elite, impartial fact-checker specializing in detecting misinformation and state propaganda.
Analyze the following text with extreme skepticism.

CRITICAL DETECTION - QUOTE CLAIMS:
If the text contains phrases like "X said", "X claimed", "X stated", "according to X", then this is a QUOTE CLAIM.
For QUOTE CLAIMS, you MUST set is_quote_claim=true and fill in ALL quote fields.

EXAMPLE - Quote Claim:
Input: "Trump said the US is a peace-loving nation"
Response should include:
- is_quote_claim: true
- quote_attribution: "Donald Trump"
- quote_verified: true (if he did say this)
- quote_source: "Rally in Pennsylvania, October 2024" (if known)
- meta_truth_verdict: "FALSE" (because the US has engaged in many wars)
- verdict: "FALSE" (matches meta_truth_verdict)

TWO-LEVEL ANALYSIS FOR QUOTES (CRITICAL):
- LEVEL 1: Did the person actually say this? (quote_verified)
- LEVEL 2: Is what they said TRUE in reality? (meta_truth_verdict)

VERDICT RULES:
1. IF LEVEL 1 is FALSE (they never said it) -> VERDICT MUST BE "FALSE" or "MISLEADING" (attribution error is a lie).
2. IF LEVEL 1 is TRUE (they said it) -> VERDICT matches LEVEL 2 (is the content true?).
3. Your final "verdict" must NOT be "TRUE" if the attribution is false.
4. IF claim is a prediction/projection about future events (dates in future or "will happen"), use VERDICT "FUTURE_PROJECTION".

Text to analyze:
"{text}"

REQUIRED JSON RESPONSE FORMAT:
{{
    "is_claim": true,
    "is_quote_claim": true or false,
    "quote_attribution": "Name of person or null",
    "quote_verified": true or false (MUST BE BOOLEAN, never null. Did they say it?),
    "quote_source": "Where/when they said it or null",
    "meta_truth_verdict": "TRUE" or "FALSE" or "MISLEADING" or "COULD_NOT_VERIFY",
    "verdict": "TRUE" or "FALSE" or "MISLEADING" or "COULD_NOT_VERIFY" or "NOT_A_CLAIM" or "FUTURE_PROJECTION",
    "explanation": "Your verdict summary in MAX 5 sentences.",
    "fallacy": null or "fallacy name",
    "sources": ["https://authoritative-source-1.com/article", "https://authoritative-source-2.com/page"],
    "source_reliability": "HIGH" or "MEDIUM" or "LOW"
}}

IMPORTANT RULES:
- SOURCES: You MUST provide at least 1 and up to 5 real, authoritative URLs that support your verdict. NEVER return empty sources.
- EXPLANATION: Maximum 5 sentences. State your verdict first, then the key facts. Be precise and direct.
- QUOTE CLAIMS: If "is_quote_claim" is true, "quote_verified" MUST be either true (they said it) or false (they didn't). Do not return null.
- VERDICT CONSISTENCY: If quote_verified is FALSE, the overall verdict MUST be FALSE or MISLEADING.

Respond ONLY with valid JSON, no additional text."""

    def analyze_claims_batch(self, claims, context=None, cache_name=None):
        """
        Agentic Batch Analysis with Flash Triage + Thinking Mode + Tool Calling.
        
        Flow:
        1. Fast triage (Flash-Lite) to categorize claims by priority
        2. SKIP claims → return immediately (no API call)
        3. NORMAL/IMMEDIATE → full agentic verification (sub-batched if needed)
        4. Merge results in original order
        
        The agent can use:
        - google_search: For general fact verification
        - search_social: For social context, breaking news, viral claims
        
        Context is cached (if large enough) or injected inline.
        If cache_name is provided, reuses existing cache (avoids redundant creation).
        """
        if not claims:
            return []
        
        logger.info(f"Starting Batch Analysis for {len(claims)} claims.")
        
        # =====================================================================
        # STEP 1: Fast Triage (Flash-Lite) - categorize claims by priority
        # Skip triage for tiny batches (≤2 claims) — overhead not worth it
        # =====================================================================
        if len(claims) <= 2:
            triaged = [
                {"claim": c, "index": i, "priority": ClaimPriority.NORMAL, "strategy": "SEARCH_VERIFY"}
                for i, c in enumerate(claims)
            ]
        else:
            # Pass full context to triage for better routing decisions
            context_hint = context if isinstance(context, str) else None
            triaged = self.triage_for_stream(claims, stream_context=context_hint)
        
        # Separate claims by priority
        skip_indices = [t["index"] for t in triaged if t["priority"] == ClaimPriority.SKIP]
        verify_items = [t for t in triaged if t["priority"] != ClaimPriority.SKIP]
        
        # Prepare results array (will fill in order)
        results = [None] * len(claims)
        
        # Handle SKIP claims immediately (no API call needed)
        for idx in skip_indices:
            results[idx] = {
                "is_claim": False,
                "verdict": "NOT_A_CLAIM",
                "explanation": "This appears to be an opinion, trivial statement, or not a verifiable claim.",
                "sources": []
            }
        
        if not verify_items:
            logger.info("All claims triaged as SKIP - no verification needed.")
            return results
        
        logger.info(f"Verifying {len(verify_items)} claims (skipped {len(skip_indices)})")
        
        # =====================================================================
        # STEP 2: Setup for agentic verification
        # =====================================================================
        from backend.services.grok_service import get_grok_service
        grok = get_grok_service()
        
        # Cache context if large enough (>4KB = ~1024 tokens)
        # If cache_name was already provided (pre-created by caller), reuse it
        inline_context = None
        
        if cache_name:
            logger.info(f"Reusing pre-created Context Cache: {cache_name}")
        elif context:
            if len(context) > 4000:
                try:
                    cache_name = self.create_cache(context, ttl_minutes=5)
                    logger.info(f"Created Context Cache: {cache_name}")
                except Exception as e:
                    logger.warning(f"Cache creation failed, using inline: {e}")
                    inline_context = context
            else:
                logger.info("Context too short for cache, injecting inline.")
                inline_context = context
        
        # Build list of claims that need verification
        claims_to_verify = [v["claim"] for v in verify_items]
        
        # =====================================================================
        # STEP 3: Sub-batch if too many claims for a single prompt
        # This prevents output-token overflow and model "forgetting" later claims
        # =====================================================================
        if len(claims_to_verify) <= self.MAX_CLAIMS_PER_PROMPT:
            sub_batches = [(verify_items, claims_to_verify)]
        else:
            sub_batches = []
            for i in range(0, len(verify_items), self.MAX_CLAIMS_PER_PROMPT):
                chunk_items = verify_items[i:i + self.MAX_CLAIMS_PER_PROMPT]
                chunk_claims = [v["claim"] for v in chunk_items]
                sub_batches.append((chunk_items, chunk_claims))
            logger.info(f"Split {len(claims_to_verify)} claims into {len(sub_batches)} sub-batches")
        
        # =====================================================================
        # STEP 4: Process each sub-batch through agentic loop
        # =====================================================================
        for batch_items, batch_claims in sub_batches:
            strategy_hints = {i + 1: v["strategy"] for i, v in enumerate(batch_items)}
            prompt = self._build_agentic_batch_prompt(batch_claims, inline_context, strategy_hints)
            batch_results = self._run_agentic_batch(
                prompt, batch_claims, grok, cache_name
            )
            
            # Merge back into results array at original indices
            for i, item in enumerate(batch_items):
                original_idx = item["index"]
                if i < len(batch_results):
                    results[original_idx] = batch_results[i]
                else:
                    results[original_idx] = {
                        "is_claim": True,
                        "verdict": "COULD_NOT_VERIFY",
                        "explanation": "Result missing from verification",
                        "sources": []
                    }
        
        return results
    
    def _run_agentic_batch(self, prompt, claims_to_verify, grok, cache_name=None):
        """
        Execute the agentic loop for a single batch of claims.
        Extracted to support sub-batching for large claim lists.
        """
        tools = {
            "function_declarations": [
                {
                    "name": "google_search",
                    "description": "Search Google for factual information to verify claims. Use for historical facts, statistics, scientific claims, and general knowledge.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "query": {
                                "type": "STRING",
                                "description": "The search query"
                            }
                        },
                        "required": ["query"]
                    }
                },
                grok.get_tool_definition()  # search_social for X/Twitter
            ]
        }
        
        conversation = [{"role": "user", "parts": [{"text": prompt}]}]
        
        payload = {
            "contents": conversation,
            "tools": [tools],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192
            }
        }
        
        if cache_name:
            payload["cachedContent"] = cache_name
        
        max_turns = 3
        url = self._get_api_url(self.MODEL_SMART)
        
        for turn in range(max_turns):
            try:
                logger.info(f"Agentic turn {turn + 1}/{max_turns}...")
                response = requests.post(
                    url,
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=120
                )
                response.raise_for_status()
                data = response.json()
                
                if 'candidates' not in data or not data['candidates']:
                    raise ValueError("No candidates in response")
                
                candidate = data['candidates'][0]
                content = candidate.get('content', {})
                parts = content.get('parts', [])
                
                # Collect function calls and text
                function_calls = []
                final_text = ""
                
                for part in parts:
                    if 'functionCall' in part:
                        function_calls.append(part['functionCall'])
                    elif 'text' in part and not part.get('thought'):
                        final_text += part['text']
                
                # Add model response to conversation
                conversation.append({"role": "model", "parts": parts})
                
                # If there are function calls, execute them
                if function_calls:
                    logger.info(f"Executing {len(function_calls)} tool calls...")
                    function_responses = []
                    
                    for fc in function_calls:
                        fn_name = fc.get('name')
                        fn_args = fc.get('args', {})
                        
                        if fn_name == 'search_social':
                            query = fn_args.get('query', '')
                            logger.info(f"🔍 Social Search: {query[:50]}...")
                            result = grok.search_social(query)
                        elif fn_name == 'google_search':
                            # Google Search is handled by the model internally
                            # We just acknowledge it
                            result = {"status": "search_executed", "query": fn_args.get('query')}
                        else:
                            result = {"error": f"Unknown tool: {fn_name}"}
                        
                        function_responses.append({
                            "functionResponse": {
                                "name": fn_name,
                                "response": result
                            }
                        })
                    
                    # Add function responses to conversation
                    conversation.append({"role": "function", "parts": function_responses})
                    payload["contents"] = conversation
                    continue  # Next turn
                
                # No function calls = final response
                if final_text:
                    return self._parse_thinking_batch_response(data, len(claims_to_verify))
                    
            except Exception as e:
                logger.error(f"Agentic turn {turn + 1} failed: {e}")
                if turn == max_turns - 1:
                    break
                import time
                time.sleep(1)
        
        # Fallback: return error for all claims in this sub-batch
        logger.error("Agentic sub-batch failed after all turns")
        return [{
            "is_claim": True,
            "verdict": "COULD_NOT_VERIFY",
            "explanation": "Agentic analysis failed",
            "sources": []
        } for _ in claims_to_verify]
    
    def _build_agentic_batch_prompt(self, claims, inline_context=None, strategy_hints=None):
        """Build agentic prompt with economic strategy selection and triage hints."""
        # Build claims list with strategy hints from triage
        if strategy_hints:
            claims_lines = []
            for i, c in enumerate(claims):
                hint = strategy_hints.get(i + 1, "")
                hint_str = f" [SUGGESTED: {hint}]" if hint else ""
                claims_lines.append(f"{i+1}. {c}{hint_str}")
            claims_str = "\n".join(claims_lines)
        else:
            claims_str = "\n".join([f"{i+1}. {c}" for i, c in enumerate(claims)])
        
        context_block = ""
        if inline_context:
            context_block = f"""

SOURCE DOCUMENT CONTEXT:
---
{inline_context}
---
"""
        
        return f"""You are an expert fact-checker. Your goals:
1. VERIFY claims accurately
2. MINIMIZE unnecessary API calls (economic efficiency)

AVAILABLE VERIFICATION STRATEGIES (choose per-claim):

| Strategy | Cost | Use When |
|----------|------|----------|
| CONTEXT_CHECK | FREE | Claim is about the source document above |
| KNOWLEDGE_CHECK | FREE | Simple well-known fact, 95%+ confident |
| SEARCH_VERIFY | 1 search | Need sources, statistics, recent events |
| SOCIAL_VERIFY | 1 Grok call | Viral claims, quotes, breaking news |

CLAIMS TO ANALYZE:
{claims_str}
{context_block}

INSTRUCTIONS:
1. For each claim, pick the most cost-effective strategy that ensures accuracy
2. ALWAYS provide 1-5 working source URLs for EVERY claim (no exceptions)
3. If using KNOWLEDGE_CHECK but unsure about sources, switch to SEARCH_VERIFY
4. Use SOCIAL_VERIFY sparingly (only for claims needing real-time social context)

OUTPUT FORMAT:
Return JSON with results in the EXACT same order as input claims:
{{
  "results": [
    {{
      "claim_index": 1,
      "strategy_used": "SEARCH_VERIFY",
      "verdict": "TRUE|FALSE|MISLEADING|COULD_NOT_VERIFY|FUTURE_PROJECTION",
      "explanation": "Brief explanation (max 3 sentences)",
      "sources": ["https://authoritative-source.com/article"],
      "social_context": "Optional: only include if SOCIAL_VERIFY was used"
    }}
  ]
}}

CRITICAL: Every claim MUST have at least 1 source URL. No exceptions."""
    
    def _parse_thinking_batch_response(self, response_data, expected_count):
        """Parse the one-shot thinking response into structured results."""
        try:
            if 'candidates' not in response_data or not response_data['candidates']:
                raise ValueError("No candidates in response")
            
            candidate = response_data['candidates'][0]
            content = candidate.get('content', {})
            parts = content.get('parts', [])
            
            # Extract text (skip thought parts)
            text = ""
            for part in parts:
                if 'text' in part and not part.get('thought'):
                    text += part['text']
            
            # Clean and parse JSON

            # Extract and parse JSON using unified helper
            json_text = self._extract_json(text)
            result_data = json.loads(json_text)
            results = result_data.get('results', [])
            
            # Normalize results
            normalized = []
            for i in range(expected_count):
                # Find result for this claim index
                claim_result = None
                for r in results:
                    if r.get('claim_index') == i + 1:
                        claim_result = r
                        break
                
                if claim_result:
                    result = {
                        "is_claim": True,
                        "verdict": claim_result.get('verdict', 'UNVERIFIABLE'),
                        "explanation": claim_result.get('explanation', ''),
                        "sources": claim_result.get('sources', [])[:5]
                    }
                    # Add social context if present (from search_social tool)
                    if claim_result.get('social_context'):
                        result["social_context"] = claim_result['social_context']
                    normalized.append(result)
                else:
                    # Missing result for this index
                    normalized.append({
                        "is_claim": True,
                        "verdict": "COULD_NOT_VERIFY",
                        "explanation": "Result missing from model response",
                        "sources": []
                    })
            
            logger.info(f"Parsed {len(normalized)} results from One-Shot response")
            return normalized
            
        except Exception as e:
            logger.error(f"Failed to parse One-Shot response: {e}")
            return [{
                "is_claim": True,
                "verdict": "COULD_NOT_VERIFY",
                "explanation": "Failed to parse model response",
                "sources": []
            } for _ in range(expected_count)]



    # =========================================================================
    # FLASH TRIAGE - Fast classification before expensive verification
    # =========================================================================
    def triage_for_stream(self, claims: list, stream_context: str = None) -> list:
        """
        Fast triage using Flash-Lite to categorize claims by priority and strategy.
        
        Makes a SINGLE cheap API call to classify all claims before expensive
        agentic verification. This reduces overall latency by routing simple
        claims to fast paths and skipping trivial/opinion claims entirely.
        
        Args:
            claims: List of claim strings to triage
            stream_context: Optional context about the source (e.g., stream title)
            
        Returns:
            List of dicts with: claim, priority (ClaimPriority), strategy, index
        """
        if not claims:
            return []
        
        # Build compact triage prompt
        claims_str = "\n".join([f"{i+1}. {c}" for i, c in enumerate(claims)])
        
        context_hint = ""
        if stream_context:
            context_hint = f"\nSOURCE CONTEXT: {stream_context[:200]}"
        
        prompt = f"""Categorize each claim for fact-check priority.

CLAIMS:
{claims_str}
{context_hint}

For each claim, classify:
- priority: SKIP (opinion/trivial), IMMEDIATE (breaking/viral), NORMAL (standard), DEFERRED (low-priority)
- strategy: KNOWLEDGE_CHECK (well-known fact), SEARCH_VERIFY (needs sources), SOCIAL_VERIFY (viral/social)

Return ONLY a JSON array, one object per claim:
[{{"index": 1, "priority": "NORMAL", "strategy": "SEARCH_VERIFY"}}, ...]"""

        try:
            response = requests.post(
                self._get_api_url(self.MODEL_TRIAGE),
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024}
                },
                headers={'Content-Type': 'application/json'},
                timeout=self.TIMEOUT_TRIAGE
            )
            response.raise_for_status()
            data = response.json()
            
            # Parse response
            if 'candidates' not in data or not data['candidates']:
                raise ValueError("No candidates in triage response")
            
            text = data['candidates'][0]['content']['parts'][0]['text']
            
            # Extract JSON array

            # Extract JSON array using unified helper
            json_text = self._extract_json(text, expect_array=True)
            triage_results = json.loads(json_text)
            
            # Map to output format with ClaimPriority enum
            priority_map = {
                "skip": ClaimPriority.SKIP,
                "immediate": ClaimPriority.IMMEDIATE,
                "normal": ClaimPriority.NORMAL,
                "deferred": ClaimPriority.DEFERRED
            }
            
            results = []
            for i, claim in enumerate(claims):
                # Find triage result for this claim (1-indexed in response)
                triage = next((t for t in triage_results if t.get('index') == i + 1), None)
                
                if triage:
                    priority_str = triage.get('priority', 'normal').lower()
                    results.append({
                        "claim": claim,
                        "index": i,
                        "priority": priority_map.get(priority_str, ClaimPriority.NORMAL),
                        "strategy": triage.get('strategy', 'SEARCH_VERIFY')
                    })
                else:
                    # Missing result, default to NORMAL
                    results.append({
                        "claim": claim,
                        "index": i,
                        "priority": ClaimPriority.NORMAL,
                        "strategy": "SEARCH_VERIFY"
                    })
            
            # Log triage summary
            skip_count = sum(1 for r in results if r["priority"] == ClaimPriority.SKIP)
            immediate_count = sum(1 for r in results if r["priority"] == ClaimPriority.IMMEDIATE)
            logger.info(f"📊 Triage: {len(claims)} claims → {skip_count} SKIP, {immediate_count} IMMEDIATE, {len(claims) - skip_count - immediate_count} NORMAL/DEFERRED")
            
            return results
            
        except Exception as e:
            logger.warning(f"Triage failed, defaulting all to NORMAL: {e}")
            # Fallback: all claims as NORMAL priority
            return [
                {
                    "claim": c,
                    "index": i,
                    "priority": ClaimPriority.NORMAL,
                    "strategy": "SEARCH_VERIFY"
                }
                for i, c in enumerate(claims)
            ]

    def _extract_json(self, text: str, expect_array: bool = False) -> str:
        """
        Robustly extract JSON from text, handling markdown blocks and loose text.
        
        Args:
            text: Raw text containing JSON
            expect_array: Whether to prioritize matching a JSON array ([]) vs object ({})
            
        Returns:
            str: Cleaned JSON string ready for json.loads()
        """
        if not text:
            return "[]" if expect_array else "{}"
            
        clean_text = text.strip()
        
        # 1. Try markdown code block first
        pattern = r'```(?:json)?\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*```'
        json_match = re.search(pattern, clean_text, re.IGNORECASE)
        if json_match:
            return json_match.group(1).strip()
            
        # 2. Fallback: Find first/last bracket based on expected type
        start_char = '[' if expect_array else '{'
        end_char = ']' if expect_array else '}'
        
        start = clean_text.find(start_char)
        end = clean_text.rfind(end_char)
        
        if start != -1 and end != -1 and end > start:
            return clean_text[start:end+1]
            
        # 3. Final attempt: startswith/endswith strip (legacy fallback)
        if clean_text.startswith('```json'):
            clean_text = clean_text[7:]
        elif clean_text.startswith('```'):
            clean_text = clean_text[3:]
        if clean_text.endswith('```'):
            clean_text = clean_text[:-3]
            
        return clean_text.strip()

    def _parse_response(self, response_data):
        """Parse Gemini REST API response into structured format."""
        try:
            # Validate response structure
            if 'candidates' not in response_data or len(response_data['candidates']) == 0:
                raise ValueError("No candidates in response")
            
            candidate = response_data['candidates'][0]
            
            if 'content' not in candidate:
                raise ValueError("No content in candidate")
            
            if 'parts' not in candidate['content'] or len(candidate['content']['parts']) == 0:
                raise ValueError("No parts in content")
            
            # Extract text from response
            content_text = candidate['content']['parts'][0]['text']
            
            # Extract and parse JSON using unified helper
            json_text = self._extract_json(content_text)
            result = json.loads(json_text)
            
            # Validate required fields
            required_fields = ['is_claim', 'verdict', 'explanation']
            for field in required_fields:
                if field not in result:
                    raise ValueError(f"Missing required field: {field}")
            
            # Ensure sources is a list
            if 'sources' not in result:
                result['sources'] = []
            elif not isinstance(result['sources'], list):
                result['sources'] = [result['sources']]
            
            # Ensure fallacy is present
            if 'fallacy' not in result:
                result['fallacy'] = None

            # Ensure source_reliability is present
            if 'source_reliability' not in result:
                result['source_reliability'] = 'MEDIUM'
            
            # Ensure Meta-Truth fields have defaults
            if 'is_quote_claim' not in result:
                result['is_quote_claim'] = False
            if 'quote_attribution' not in result:
                result['quote_attribution'] = None
            if 'quote_verified' not in result:
                result['quote_verified'] = None
            if 'quote_source' not in result:
                result['quote_source'] = None
            if 'meta_truth_verdict' not in result:
                result['meta_truth_verdict'] = result.get('verdict')
            
            # Validate sources - filter out dead links
            if result.get('sources'):
                result['sources'] = self._validate_sources(result['sources'])
            
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini response as JSON: {e}")
            return {
                'is_claim': True,
                'verdict': 'MISLEADING',
                'explanation': 'Unable to fully verify this claim. The AI model returned an invalid response.',
                'fallacy': None,
                'sources': [],
                'source_reliability': 'LOW'
            }
        except Exception as e:
            logger.error(f"Error parsing Gemini response: {e}")
            raise
    
    def _chunk_text(self, text, chunk_size=4000, overlap=500):
        """Split text into overlapping chunks to preserve context."""
        if len(text) <= chunk_size:
            return [text]
            
        chunks = []
        start = 0
        text_len = len(text)
        
        while start < text_len:
            end = min(start + chunk_size, text_len)
            
            # If we are not at the end, try to break at a sentence or word boundary
            if end < text_len:
                # Look back from 'end' to find a period or space
                # Look in the last 100 chars of the chunk
                lookback = 100
                search_start = max(start, end - lookback)
                search_text = text[search_start:end]
                
                # Priority 1: Sentence break
                last_period = search_text.rfind('.')
                if last_period != -1:
                    end = search_start + last_period + 1
                else:
                    # Priority 2: Space break
                    last_space = search_text.rfind(' ')
                    if last_space != -1:
                        end = search_start + last_space

            chunks.append(text[start:end])
            
            if end >= text_len:
                break
                
            # Move start forward, backing up by overlap amount
            # But ensure we effectively move forward (start + chunk_size - overlap > start)
            start = end - overlap
            
        return chunks

    def _generate_context_summary(self, text):
        """
        Generate a short summary of the text for cross-chunk context.
        Used by identify_claims to give each chunk awareness of the full text.
        """
        # Take first ~6000 chars (enough to capture topic, speakers, key entities)
        sample = text[:6000]
        prompt = f"""Summarize this text in 2-3 sentences. Focus on: who is speaking/writing,
what the main topic is, and any key entities (people, organizations, numbers) mentioned.

TEXT:
\"\"\"
{sample}
\"\"\"

Respond with ONLY the summary, no JSON."""
        
        try:
            response = requests.post(
                self._get_api_url(self.MODEL_FAST),
                json={"contents": [{"parts": [{"text": prompt}]}]},
                headers={'Content-Type': 'application/json'},
                timeout=self.TIMEOUT_FAST
            )
            response.raise_for_status()
            data = response.json()
            
            if 'candidates' in data and data['candidates']:
                summary = data['candidates'][0]['content']['parts'][0]['text'].strip()
                logger.info(f"Generated context summary: {summary[:80]}...")
                return summary
        except Exception as e:
            logger.warning(f"Context summary generation failed: {e}")
        
        return None

    @staticmethod
    def _normalize_claim_text(claim):
        """Normalize claim text for deduplication. Lowercases and strips punctuation."""
        normalized = claim.strip().lower()
        # Remove trailing punctuation that doesn't change meaning
        normalized = normalized.rstrip('.!?')
        # Collapse whitespace
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized

    def identify_claims(self, text):
        """Smart Agent: Identify distinct claims in text with full context using chunking."""
        
        # 1. Chunk the text
        chunks = self._chunk_text(text)
        logger.info(f"Smart Agent potentially processing {len(chunks)} chunks for text length {len(text)}")
        
        # 2. Generate a global context summary for multi-chunk texts
        # This prevents cross-chunk context loss (e.g. pronoun resolution)
        context_summary = None
        if len(chunks) > 1:
            context_summary = self._generate_context_summary(text)
        
        # claim_text -> normalized_key for dedup
        seen_normalized = {}  # normalized_key -> original claim text
        
        for i, chunk in enumerate(chunks):
            # Build context preamble for multi-chunk texts
            preamble = ""
            if context_summary:
                preamble = f"""GLOBAL CONTEXT (this is part {i+1} of {len(chunks)} from a longer text):
{context_summary}

Use this context to resolve pronouns and references. For example, if the context
mentions "Biden" and this chunk says "He", replace "He" with "Biden".

"""
            
            prompt = f"""You are a meticulous fact-checker assistant. Your job is to extract EVERY verifiable factual claim from this text.

{preamble}TEXT TO ANALYZE (Part {i+1}/{len(chunks)}):
\"\"\"
{chunk}
\"\"\"

YOUR TASK:
Go through the text SENTENCE BY SENTENCE. For each sentence, ask: "Does this contain a factual claim that can be verified as true or false?"

EXTRACTION RULES:
1. RESOLVE PRONOUNS: Replace "they", "it", "this", "that", "he", "she" with the actual noun
   - Original: "They are mammals" → Extract: "Dolphins are mammals"
   
2. STANDALONE CLAIMS: Each claim must make sense on its own without the original text
   - Original: "This is a lot" → Extract: "40 grams of sugar per serving is a lot"
   
3. MULTIPLE CLAIMS PER SENTENCE: If a sentence has 2+ claims, extract each separately
   - "Dolphins lay eggs AND are the best pets" → 2 separate claims
   
4. DO NOT SKIP THE LAST SENTENCE - check it for claims too!

5. INCLUDE claims about:
   - Scientific facts ("dolphins are mammals")
   - Statistics ("eggs are the best investment of past 20 years")  
   - Technical claims ("pop() removes the last item")
   - Comparisons ("X is better than Y")

6. EXCLUDE only:
   - Pure opinions with no factual basis ("I like eggs")
   - Questions ("what is pop()?")
   - Commands/instructions ("click the button")

RESPOND WITH JSON ONLY - extract up to 15 claims per chunk:
{{"claims": ["claim 1", "claim 2", "claim 3", ...]}}

If zero claims found, return: {{"claims": []}}"""

            try:
                payload = {"contents": [{"parts": [{"text": prompt}]}]}
                response = requests.post(self._get_api_url(self.MODEL_FAST), json=payload, 
                                         headers={'Content-Type': 'application/json'}, timeout=self.TIMEOUT_FAST)
                response.raise_for_status()
                data = response.json()
                
                if 'candidates' not in data or not data['candidates']:
                    continue
                
                content = data['candidates'][0]['content']['parts'][0]['text'].strip()
                # Clean markdown code blocks
                if content.startswith('```json'):
                    content = content[7:]
                elif content.startswith('```'):
                    content = content[3:]
                if content.endswith('```'):
                    content = content[:-3]
                
                result = json.loads(content.strip())
                claims = result.get('claims', [])
                
                # Add with normalized dedup
                for claim in claims:
                    if claim and isinstance(claim, str) and len(claim.strip()) > 5:
                        clean = claim.strip()
                        normalized = self._normalize_claim_text(clean)
                        if normalized not in seen_normalized:
                            seen_normalized[normalized] = clean
                        
                logger.info(f"Chunk {i+1}: Found {len(claims)} claims")
                
            except Exception as e:
                logger.warning(f"Smart Agent chunk {i+1} failed: {e}", exc_info=True)
                # Continue to next chunk even if one fails
        
        final_claims = list(seen_normalized.values())
        logger.info(f"Smart Agent total distinct claims found: {len(final_claims)}")
        return final_claims

    def analyze_ai_content(self, text):
        """
        Analyze text for AI-generation likelihood (similar to GPT Zero).
        
        Args:
            text: The text to analyze
            
        Returns:
            Dict containing AI probability and indicators
        """
        prompt = f"""You are an expert AI text detector. Your job is to determine if text was written by an AI language model (like ChatGPT, Claude, Gemini) or by a human.

STRONG AI INDICATORS (score +20 each if present):
- "Furthermore," "Moreover," "In addition," "As a result," at sentence starts
- Phrases like "significantly transformed," "remarkable efficiency," "unprecedented capacity"
- Perfect parallel sentence structures
- Generic corporate/academic tone with no personal voice
- Every paragraph is roughly the same length
- No contractions (using "cannot" instead of "can't")
- Hedging phrases like "it is important to note," "one might argue"
- Bullet-point-ready prose (lists disguised as paragraphs)
- Overuse of "various," "numerous," "substantial," "enhance"

STRONG HUMAN INDICATORS (score -15 each if present):
- Typos, grammatical errors, or informal punctuation
- Personal opinions ("I think," "in my experience")
- Specific examples from real life
- Slang, contractions, or casual language
- Emotional reactions or humor
- Run-on sentences or fragments
- Inconsistent formatting or structure

SCORING GUIDE:
- 80-100%: Clearly AI (multiple strong AI indicators, corporate/smooth prose)
- 60-79%: Likely AI (some AI patterns, too polished)
- 40-59%: Uncertain (mixed signals)
- 20-39%: Likely Human (some polish but genuine voice)
- 0-19%: Clearly Human (obvious personal voice, imperfections)

TEXT TO ANALYZE:
\"\"\"
{text}
\"\"\"

Be decisive. The text above uses classic AI writing patterns if it:
- Opens with broad generalizations about technology/society
- Uses transition words like "Furthermore" or "Moreover"
- Has perfectly balanced paragraphs
- Lacks any personal voice or specific examples

RESPOND WITH JSON ONLY:
{{
    "ai_probability": <0-100 integer - be decisive, avoid 50>,
    "confidence": "HIGH" or "MEDIUM" or "LOW",
    "ai_indicators": ["specific patterns found in THIS text"],
    "human_indicators": ["human traits found, if any"],
    "explanation": "2-sentence verdict"
}}"""

        try:
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            response = requests.post(
                self._get_api_url(self.MODEL_FAST), 
                json=payload, 
                headers={'Content-Type': 'application/json'}, 
                timeout=self.TIMEOUT_FAST
            )
            response.raise_for_status()
            data = response.json()
            
            if 'candidates' not in data or not data['candidates']:
                raise ValueError("No candidates in response")
            
            content = data['candidates'][0]['content']['parts'][0]['text'].strip()
            
            # Clean markdown code blocks
            if content.startswith('```json'):
                content = content[7:]
            elif content.startswith('```'):
                content = content[3:]
            if content.endswith('```'):
                content = content[:-3]
            
            result = json.loads(content.strip())
            
            # Validate required fields with defaults
            if 'ai_probability' not in result:
                result['ai_probability'] = 50
            if 'confidence' not in result:
                result['confidence'] = 'LOW'
            if 'ai_indicators' not in result:
                result['ai_indicators'] = []
            if 'human_indicators' not in result:
                result['human_indicators'] = []
            if 'explanation' not in result:
                result['explanation'] = 'Unable to fully analyze text.'
            
            # Ensure indicators are lists of strings (Gemini sometimes returns dicts)
            def sanitize_indicators(items):
                if not isinstance(items, list):
                    return []
                return [str(item) if not isinstance(item, str) else item for item in items]
            
            result['ai_indicators'] = sanitize_indicators(result['ai_indicators'])
            result['human_indicators'] = sanitize_indicators(result['human_indicators'])
            
            logger.info(f"AI Detection result: {result['ai_probability']}% AI probability")
            return result
            
        except requests.exceptions.Timeout:
            logger.error("AI detection timeout")
            return {
                'ai_probability': 50,
                'confidence': 'LOW',
                'ai_indicators': [],
                'human_indicators': [],
                'explanation': 'Analysis timed out. Please try again.'
            }
        except Exception as e:
            logger.error(f"AI detection failed: {e}", exc_info=True)
            return {
                'ai_probability': 50,
                'confidence': 'LOW',
                'ai_indicators': [],
                'human_indicators': [],
                'explanation': f'Analysis failed: {str(e)}'
            }
