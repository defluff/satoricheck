"""
xAI Grok API service for social context in Smart Mode.
Searches X/Twitter for quote sources and recent events.
"""
import re
import requests
import logging
from urllib.parse import urlparse

from backend.config import Config

logger = logging.getLogger(__name__)


# Allowed domains for social media URLs
ALLOWED_DOMAINS = ['twitter.com', 'x.com', 'www.twitter.com', 'www.x.com']


def strip_invisible_chars(text: str) -> str:
    """Remove invisible/non-printable Unicode characters (ASCII smuggling protection)."""
    if not text:
        return text
    invisible_pattern = re.compile(
        r'[\u200b-\u200f\u2028-\u202f\u2060-\u206f\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]'
    )
    return invisible_pattern.sub('', text)


def validate_social_url(url: str) -> str | None:
    """Only allow known social media domains (SSRF protection)."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        if parsed.netloc.lower() in ALLOWED_DOMAINS:
            return url
    except Exception:
        pass
    return None


def should_fire_grok(claim: str, gemini_result: dict = None) -> bool:
    """
    Detect triggers that warrant a Grok social context search.
    
    Triggers:
    - @handles (e.g., @elonmusk)
    - #hashtags (e.g., #breaking)
    - Temporal keywords (just, today, now, breaking)
    - Quote patterns (X said, according to X)
    - Gemini couldn't verify (fallback)
    """
    if not claim:
        return False
    
    claim_lower = claim.lower()
    
    # Explicit social triggers
    if '@' in claim or '#' in claim:
        return True
    
    # Temporal triggers (recent events)
    temporal_keywords = ['just', 'today', 'now', 'breaking', 'just now', 'just did']
    if any(keyword in claim_lower for keyword in temporal_keywords):
        return True
    
    # Quote claim patterns
    quote_patterns = [
        r'\b\w+\s+said\b',           # "Trump said"
        r'\baccording to\s+\w+',     # "according to Elon"
        r'\b\w+\s+tweeted\b',        # "Biden tweeted"
        r'\b\w+\s+posted\b',         # "Musk posted"
    ]
    for pattern in quote_patterns:
        if re.search(pattern, claim_lower):
            return True
    
    # Fallback: Gemini couldn't verify
    if gemini_result:
        verdict = gemini_result.get('verdict', '').upper()
        if verdict in ['COULD_NOT_VERIFY', 'UNKNOWN']:
            return True
    
    return False


class GrokService:
    """Service for interacting with xAI Grok API for social context."""
    
    ENDPOINT = "https://api.x.ai/v1/chat/completions"
    MODEL = "grok-4-latest"
    
    def __init__(self):
        """Initialize Grok service."""
        if not Config.GROK_API_KEY:
            logger.warning("GROK_API_KEY is not set - Grok service will not work")
        self.api_key = Config.GROK_API_KEY
        self.timeout = Config.GROK_TIMEOUT
    
    def search_social(self, claim: str) -> dict:
        """
        Search X/Twitter for social context about a claim.
        
        Args:
            claim: The claim to search for context
            
        Returns:
            dict with keys: found, source, text, url, verified, engagement
        """
        if not self.api_key:
            logger.error("Cannot search: GROK_API_KEY not configured")
            return {'found': False, 'error': 'API not configured'}
        
        # Build the prompt for Grok
        system_prompt = """You are a social media fact-checking assistant. 
Search X/Twitter for the most relevant post, tweet, or statement related to the user's claim.

Respond in JSON format with these fields:
- found: boolean (true if you found relevant social media content)
- source: string (e.g., "@realDonaldTrump", "@WhiteHouse")
- source_verified: boolean (is this a verified/official account?)
- text: string (the actual tweet/post text, max 280 chars)
- url: string (link to the tweet if available, or null)
- posted_at: string (when it was posted, e.g., "2026-01-15")
- engagement: object with likes, retweets if available
- context: string (brief explanation of relevance)

If no relevant social content found, return: {"found": false, "context": "reason"}"""

        user_prompt = f"Find social media context for this claim: \"{claim}\""
        
        try:
            response = requests.post(
                self.ENDPOINT,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.api_key}'
                },
                json={
                    'model': self.MODEL,
                    'messages': [
                        {'role': 'system', 'content': system_prompt},
                        {'role': 'user', 'content': user_prompt}
                    ],
                    'temperature': 0,
                    'stream': False
                },
                timeout=self.timeout
            )
            
            response.raise_for_status()
            data = response.json()
            
            # Extract the content from Grok's response
            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            
            # Parse the JSON response from Grok
            result = self._parse_grok_response(content)
            return self.sanitize_response(result)
            
        except requests.Timeout:
            logger.warning(f"Grok API timeout after {self.timeout}s")
            return {'found': False, 'error': 'timeout'}
        except requests.RequestException as e:
            logger.error(f"Grok API request failed: {e}")
            return {'found': False, 'error': str(e)}
        except Exception as e:
            logger.error(f"Grok service error: {e}", exc_info=True)
            return {'found': False, 'error': 'internal_error'}
    
    def _parse_grok_response(self, content: str) -> dict:
        """Parse JSON response from Grok, handling markdown code blocks."""
        import json
        
        if not content:
            return {'found': False}
        
        # Strip markdown code blocks if present
        content = content.strip()
        if content.startswith('```'):
            # Remove ```json and closing ```
            lines = content.split('\n')
            content = '\n'.join(lines[1:-1] if lines[-1] == '```' else lines[1:])
        
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse Grok response as JSON: {content[:100]}")
            return {'found': False, 'raw_response': content}
    
    def sanitize_response(self, response: dict) -> dict:
        """
        Sanitize Grok response for security.
        - Strip invisible characters (ASCII smuggling)
        - Validate URLs (SSRF protection)
        """
        if not response:
            return {'found': False}
        
        # Sanitize text fields
        text_fields = ['text', 'source', 'context']
        for field in text_fields:
            if field in response and response[field]:
                response[field] = strip_invisible_chars(str(response[field]))
        
        # Validate URL
        if 'url' in response:
            response['url'] = validate_social_url(response.get('url'))
        
        return response


# Singleton instance
_grok_service = None


def get_grok_service() -> GrokService:
    """Get or create singleton GrokService instance."""
    global _grok_service
    if _grok_service is None:
        _grok_service = GrokService()
    return _grok_service
