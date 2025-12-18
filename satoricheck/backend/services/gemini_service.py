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
    
    def __init__(self):
        """Initialize Gemini service."""
        if not Config.GEMINI_API_KEY:
            logger.error("GEMINI_API_KEY is not set!")
        else:
            logger.info(f"GEMINI_API_KEY loaded: {Config.GEMINI_API_KEY[:10]}...")
        self.api_key = Config.GEMINI_API_KEY
        self.api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={self.api_key}"
    
    def analyze_claim(self, text):
        """
        Analyze text for factual claims using Gemini API.
        
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
                return {
                    "is_claim": cached.is_claim,
                    "verdict": cached.verdict,
                    "explanation": cached.explanation,
                    "fallacy": cached.fallacy,
                    "sources": json.loads(cached.sources) if cached.sources else []
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
                    self.api_url,
                    json=payload,
                    headers={'Content-Type': 'application/json'},
                    timeout=30
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
        """Build the fact-checking prompt."""
        return f"""You are a professional fact-checker. Analyze the following text and determine:

1. Whether it contains a factual claim that can be verified
2. If it is a claim, determine its truthfulness
3. Identify any logical fallacies
4. Provide sources to support your analysis

Text to analyze:
"{text}"

Respond in the following JSON format:
{{
    "is_claim": true/false,
    "verdict": "TRUE" or "FALSE" or "MISLEADING" or "COULD_NOT_VERIFY" or "NOT_A_CLAIM",
    "explanation": "detailed explanation of your analysis (optional for NOT_A_CLAIM)",
    "fallacy": "name of logical fallacy if detected, or null",
    "sources": ["url1", "url2", ...]
}}

Important guidelines:
- Only mark as "TRUE" if the claim is factually accurate
- Mark as "FALSE" if the claim is factually incorrect
- Mark as "MISLEADING" if the claim is partially true but missing context or contains distortions
- Mark as "COULD_NOT_VERIFY" if the claim cannot be verified with available information
- Mark as "NOT_A_CLAIM" if the text is opinion, question, or non-verifiable statement (no explanation needed)
- Always provide credible sources (news outlets, academic papers, official statistics) for claims
- Identify logical fallacies like: strawman, ad hominem, false equivalence, slippery slope, etc.

Respond ONLY with valid JSON, no additional text."""
    
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
            
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Gemini response as JSON: {e}")
            return {
                'is_claim': True,
                'verdict': 'MISLEADING',
                'explanation': 'Unable to fully verify this claim. The AI model returned an invalid response.',
                'fallacy': None,
                'sources': []
            }
        except Exception as e:
            logger.error(f"Error parsing Gemini response: {e}")
            raise
    
    def identify_claims(self, text):
        """Smart Agent: Identify distinct claims in text with full context."""
        prompt = f"""You are a meticulous fact-checker assistant. Your job is to extract EVERY verifiable factual claim from this text.

TEXT TO ANALYZE:
\"\"\"
{text}
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

RESPOND WITH JSON ONLY - extract up to 10 claims:
{{"claims": ["claim 1", "claim 2", "claim 3", ...]}}

If zero claims found, return: {{"claims": []}}"""

        try:
            payload = {"contents": [{"parts": [{"text": prompt}]}]}
            response = requests.post(self.api_url, json=payload, 
                                     headers={'Content-Type': 'application/json'}, timeout=25)
            response.raise_for_status()
            data = response.json()
            
            if 'candidates' not in data or not data['candidates']:
                return []
            
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
            logger.info(f"Smart Agent identified {len(claims)} claims: {claims}")
            return claims
        except Exception as e:
            logger.warning(f"Smart Agent failed: {e}", exc_info=True)
            return []


