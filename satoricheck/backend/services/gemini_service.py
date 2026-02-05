"""
Google Gemini API integration service.
"""
import requests
import json
import logging
from backend.config import Config

logger = logging.getLogger(__name__)


class GeminiService:
    """Service for interacting with Google Gemini API."""
    
    # Models
    # Models
    # all Gemini 3 Flash for now, smart model approach will be used in a later stage.
    MODEL_SMART = "gemini-3-flash-preview"
    MODEL_FAST = "gemini-3-flash-preview"
    
    # Timeouts (seconds)
    TIMEOUT_SMART = 30
    TIMEOUT_FAST = 30
    
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
    
    def analyze_claim(self, text):
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
    
    def _build_fact_check_prompt(self, text):
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
    "verdict": "TRUE" or "FALSE" or "MISLEADING" or "COULD_NOT_VERIFY" or "NOT_A_CLAIM",
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

    def analyze_claims_batch(self, claims):
        """
        Analyze multiple claims in a single API call (Batch Mode).
        
        Args:
            claims: List of strings (claims) to analyze
            
        Returns:
            List of dicts (analysis results), one for each claim in order
        """
        if not claims:
            return []
            
        # Call API with Retries
        max_retries = 3
        retry_delay = 1
        last_exception = None

        prompt = self._build_batch_fact_check_prompt(claims)

        for attempt in range(max_retries):
            try:
                logger.info(f"Sending BATCH fact-check request (attempt {attempt + 1}) for {len(claims)} claims...")
                
                payload = {
                    "contents": [{
                        "parts": [{"text": prompt}]
                    }],
                    "tools": [{"google_search": {}}]
                }
                
                response = requests.post(
                    self._get_api_url(self.MODEL_SMART),
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=self.TIMEOUT_SMART * 2  # Double timeout for batch
                )
                response.raise_for_status()
                response_data = response.json()
                
                # Parse Batch Response
                results = self._parse_batch_response(response_data, len(claims))
                
                logger.info(f"Batch fact-check completed. Got {len(results)} results.")
                return results
                
            except Exception as e:
                # Same retry logic as single check
                error_msg = f"Batch API error (Attempt {attempt + 1}): {str(e)}"
                logger.error(error_msg, exc_info=True)
                last_exception = str(e)
                
                if attempt < max_retries - 1:
                    import time
                    time.sleep(retry_delay)
                    retry_delay *= 2

        # Retries exhausted
        raise Exception(f"Batch fact-check failed: {last_exception}")

    def _build_batch_fact_check_prompt(self, claims):
        """Build the batch fact-checking prompt."""
        
        claims_list = "\n".join([f"CLAIM #{i+1}: {claim}" for i, claim in enumerate(claims)])
        
        return f"""You are an elite, impartial fact-checker.
Analyze the following list of {len(claims)} claims with extreme skepticism.

CLAIMS TO CHECK:
{claims_list}

INSTRUCTIONS:
For EACH claim, provide a full analysis following the same strict rules as individual checks:
- Detect QUOTE CLAIMS (attribution verification vs meta-truth).
- Provide a VERDICT (TRUE, FALSE, MISLEADING, COULD_NOT_VERIFY).
- Provide an EXPLANATION (Max 3 sentences).
- Provide SOURCES (1-3 reliable URLs).

REQUIRED JSON RESPONSE FORMAT:
Respond ONLY with a JSON OBJECT containing a "results" array. The array must preserve the exact order of claims.

{{
  "results": [
    {{
      "claim_index": 1,
      "is_claim": true,
      "verdict": "FALSE",
      "explanation": "Brief explanation...",
      "sources": ["url1", "url2"],
      "source_reliability": "HIGH",
      "is_quote_claim": false,
      "quote_attribution": null,
      "meta_truth_verdict": null
    }},
    ... results for all {len(claims)} claims ...
  ]
}}

CRITICAL:
- You MUST return a result for EVERY claim in the list.
- Use valid JSON.
"""

    def _parse_batch_response(self, response_data, expected_count):
        """Parse batch response."""
        try:
            # Basic extraction (same as single)
            if 'candidates' not in response_data or not response_data['candidates']:
                raise ValueError("No candidates")
                
            content = response_data['candidates'][0]['content']['parts'][0]['text'].strip()
            
            # Clean markdown
            if content.startswith('```json'): content = content[7:]
            if content.startswith('```'): content = content[3:]
            if content.endswith('```'): content = content[:-3]
            
            data = json.loads(content.strip())
            results = data.get('results', [])
            
            # Normalize and validate each result
            normalized_results = []
            for i in range(min(len(results), expected_count)):
                res = results[i]
                # Apply defaults
                if 'verdict' not in res: res['verdict'] = 'COULD_NOT_VERIFY'
                if 'explanation' not in res: res['explanation'] = 'Analysis failed.'
                if 'sources' not in res: res['sources'] = []
                
                # Validate sources
                if res.get('sources'):
                     res['sources'] = self._validate_sources(res['sources'])
                
                normalized_results.append(res)
                
            # If AI missed some claims, pad with errors
            while len(normalized_results) < expected_count:
                normalized_results.append({
                    "verdict": "COULD_NOT_VERIFY",
                    "explanation": "Batch processing incomplete.",
                    "sources": [],
                    "is_claim": True
                })
                
            return normalized_results
            
        except Exception as e:
            logger.error(f"Batch parse error: {e}")
            raise
    
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
            
            # Clean up markdown code blocks
            text = content_text.strip()
            if text.startswith('```json'):
                text = text[7:]
            if text.startswith('```'):
                text = text[3:]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()
            
            # Parse JSON
            result = json.loads(text)
            
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

    def identify_claims(self, text):
        """Smart Agent: Identify distinct claims in text with full context using chunking."""
        
        # 1. Chunk the text
        chunks = self._chunk_text(text)
        logger.info(f"Smart Agent potentially processing {len(chunks)} chunks for text length {len(text)}")
        
        all_claims = set() # Use set for deduplication
        
        for i, chunk in enumerate(chunks):
            # Prompt updated to allow more claims per chunk
            prompt = f"""You are a meticulous fact-checker assistant. Your job is to extract EVERY verifiable factual claim from this text.

TEXT TO ANALYZE (Part {i+1}/{len(chunks)}):
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
                
                # Add to set (deduplication)
                for claim in claims:
                    if claim and isinstance(claim, str) and len(claim.strip()) > 5:
                        all_claims.add(claim.strip())
                        
                logger.info(f"Chunk {i+1}: Found {len(claims)} claims")
                
            except Exception as e:
                logger.warning(f"Smart Agent chunk {i+1} failed: {e}", exc_info=True)
                # Continue to next chunk even if one fails
        
        final_claims = list(all_claims)
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

