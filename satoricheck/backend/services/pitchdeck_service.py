"""
Pitchdeck Analysis Service.

Analyzes pitch deck PDFs using Gemini Vision API to extract
company summary, USP, market, and competition information.

Privacy-first: PDFs are processed inline (not stored) and discarded after analysis.
"""
import base64
import html
import json
import logging
import requests
from typing import Optional

from backend.config import Config

logger = logging.getLogger(__name__)


class PitchdeckService:
    """Service for analyzing pitch deck PDFs with Gemini Vision."""
    
    # Models — strictly two-tier architecture (Fast/Flash and Pro)
    MODEL_PRO = Config.GEMINI_MODEL_PRO
    
    # Maximum PDF size: 25MB
    MAX_FILE_SIZE = 25 * 1024 * 1024
    
    # Field length limits for sanitization
    MAX_FIELD_LENGTH = 5000
    MAX_COMPANY_NAME_LENGTH = 200
    
    # API timeout for vision analysis.
    # 90s is generous for a direct extraction call (no thinking mode).
    # If thinking mode is ever re-enabled here, raise this to 180s.
    TIMEOUT = 90
    
    def __init__(self):
        """Initialize Pitchdeck service."""
        self.api_key = Config.GEMINI_API_KEY
        if not self.api_key:
            logger.error("GEMINI_API_KEY is not set for PitchdeckService!")
        else:
            logger.info("[Pitchdeck] ✓ Service initialized")
    
    def _get_api_url(self, model: str) -> str:
        """Get API URL for a specific model (matches GeminiService pattern)."""
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def _get_headers(self) -> dict:
        """Get standard headers for Gemini REST API."""
        return {
            'Content-Type': 'application/json',
            'x-goog-api-key': self.api_key
        }

    def analyze_pitch_deck(self, pdf_bytes: bytes) -> dict:
        """
        Analyze a pitch deck PDF using Gemini Vision.
        
        Args:
            pdf_bytes: Raw PDF file bytes
            
        Returns:
            Dict containing analysis results:
            - company_name: str
            - summary: str
            - usp: str
            - market_size: str (optional)
            - competition: list[str] (optional)
            
        Raises:
            ValueError: For invalid input (null, empty, non-PDF, oversized)
            TimeoutError: If API times out
            Exception: For other API errors
        """
        # =================================================================
        # INPUT VALIDATION
        # =================================================================
        
        if pdf_bytes is None:
            raise ValueError("PDF data is required")
        
        if len(pdf_bytes) == 0:
            raise ValueError("PDF data is required - file is empty")
        
        if len(pdf_bytes) > self.MAX_FILE_SIZE:
            raise ValueError(f"File too large. Maximum size is {self.MAX_FILE_SIZE // (1024*1024)}MB")
        
        # Basic PDF validation - check for PDF header
        if not self._is_valid_pdf(pdf_bytes):
            raise ValueError("Invalid PDF - not a valid PDF file")
        
        # =================================================================
        # CALL GEMINI VISION API
        # =================================================================
        
        # Call Gemini Vision API with Retries
        prompt = self._build_analysis_prompt()
        
        # 2 retries max: each attempt can take up to TIMEOUT seconds.
        # 3 retries × 180s = 9 min of blocking — capped at 2 × 180s = 6 min worst case.
        max_retries = 2
        retry_delay = 2
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                logger.info(f"[Pitchdeck] Sending analysis request (Attempt {attempt + 1}/{max_retries})")
                raw_result = self._call_gemini_vision(pdf_bytes, prompt)
                break  # Success
                
            except requests.exceptions.Timeout:
                logger.warning(f"[Pitchdeck] Timeout on attempt {attempt + 1} (>{self.TIMEOUT}s)")
                last_exception = TimeoutError("Analysis timed out. Please try again.")
                
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response else None
                logger.warning(f"[Pitchdeck] HTTP {status_code} on attempt {attempt + 1}: {e}")
                
                if status_code == 429:
                    last_exception = Exception("Rate limit reached. Please try again later.")
                elif status_code and 500 <= status_code < 600:
                    last_exception = Exception("Service temporarily unavailable. Please try again.")
                else:
                    last_exception = Exception(f"API error: {status_code}")
                    if status_code and 400 <= status_code < 500:
                        raise last_exception  # Don't retry client errors
            
            except Exception as e:
                logger.warning(f"[Pitchdeck] Error on attempt {attempt + 1}: {e}")
                last_exception = e
                
            # Wait before retry (not after last attempt)
            if attempt < max_retries - 1:
                import time
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10)  # Cap backoff at 10s
        else:
            # for/else: loop finished without break — all attempts failed
            raise last_exception or Exception("Analysis failed after retries")
        
        # =================================================================
        # PARSE AND SANITIZE RESPONSE
        # =================================================================
        
        result = self._parse_vision_response(raw_result)
        
        # --- NEW: Context Caching ---
        # Create a cache of the entire deck context for subsequent queries
        try:
            # Import gemini service
            from backend.services import get_gemini_service
            gemini_svc = get_gemini_service()
            
            # Synthesize context content
            deck_content = f"""
PITCH DECK CONTEXT CACHE
Company: {result.get('company_name')}
Summary: {result.get('summary')}
Industry: {result.get('industry')} / {result.get('sector')}
Competition: {', '.join(result.get('competition', []))}
USP: {result.get('usp')}

EXTRACTED CLAIMS:
{json.dumps(result.get('verifiable_claims', []), indent=2)}

RAW VISION OUTPUT (Full Text):
{json.dumps(raw_result, indent=2) if raw_result else 'No raw data'}
            """
            
            cache_name = gemini_svc.create_cache(deck_content, ttl_minutes=10)
            result['cache_name'] = cache_name
            logger.info(f"[Pitchdeck] Created context cache: {cache_name}")
            
        except Exception as e:
            logger.warning(f"[Pitchdeck] Failed to create context cache: {e}")
            result['cache_name'] = None
            
        # ----------------------------

        result = self._sanitize_output(result)
        
        return result

    def _is_valid_pdf(self, pdf_bytes: bytes) -> bool:
        """Check if bytes represent a valid PDF file."""
        # PDF files start with %PDF-
        if len(pdf_bytes) < 5:
            return False
        return pdf_bytes[:5] == b'%PDF-'

    def _build_analysis_prompt(self) -> str:
        """
        Build the extraction prompt by injecting the vc_analyst skill file.

        The skill file (vc_analyst.md) is the single source of truth for all VC domain
        knowledge: benchmark tiers, metric definitions, claim categories, and output schema.
        This mirrors the ai_detection skill pattern used in GeminiService.analyze_ai_content.

        Falls back to a minimal inline prompt if the skill file is missing, so the
        service degrades gracefully rather than raising an error.
        """
        # Deferred import to avoid circular imports at module load time.
        from backend.services import get_gemini_service
        skill_manual = get_gemini_service()._load_skill("vc_analyst")

        if not skill_manual:
            # Graceful fallback — service keeps working without the skill file.
            logger.warning("[Pitchdeck] vc_analyst skill missing, using fallback prompt")
            return (
                "You are an expert VC analyst. Analyze this pitch deck PDF and return a JSON "
                "object with: company_name, summary, usp, industry, sector, market_size, "
                "competition, team_highlights, funding_ask, verifiable_claims, vc_metrics. "
                "Respond ONLY with valid JSON."
            )

        return (
            f"Using the following Expert VC Analyst Manual:\n\n{skill_manual}\n\n"
            "Analyze the attached pitch deck PDF and respond ONLY with valid JSON."
        )

    def _call_gemini_vision(self, pdf_bytes: bytes, prompt: str) -> dict:
        """
        Call Gemini Vision API with PDF as inline data.
        
        Args:
            pdf_bytes: Raw PDF bytes
            prompt: Analysis prompt
            
        Returns:
            Raw API response dict
        """
        # Encode PDF as base64 for inline data
        pdf_base64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')
        
        # Build multimodal request with PDF.
        # No thinkingConfig: pitchdeck analysis is structured extraction (read → emit JSON),
        # not a reasoning task. Thinking mode adds 60-90s latency with no accuracy benefit here.
        payload = {
            "contents": [{
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": "application/pdf",
                            "data": pdf_base64
                        }
                    },
                    {
                        "text": prompt
                    }
                ]
            }]
        }
        
        url = self._get_api_url(self.MODEL_PRO)
        
        logger.info(f"[Pitchdeck] Sending PDF to Gemini Vision ({len(pdf_bytes)} bytes)")
        
        response = requests.post(
            url,
            json=payload,
            headers=self._get_headers(),
            timeout=self.TIMEOUT
        )
        response.raise_for_status()
        
        return response.json()

    def _parse_vision_response(self, response_data: dict) -> dict:
        """Parse Gemini Vision response into structured format."""
        try:
            if 'candidates' not in response_data or len(response_data['candidates']) == 0:
                raise ValueError("No response from analysis")
            
            candidate = response_data['candidates'][0]
            
            if 'content' not in candidate or 'parts' not in candidate['content']:
                raise ValueError("Malformed response structure")
            
            text = ""
            for part in candidate['content']['parts']:
                # Skip thought parts if present (Thinking Mode)
                if part.get('thought'):
                    logger.info(f"[Pitchdeck] 🧠 Vision Thought: {part.get('text', '')[:100]}...")
                    continue
                    
                text += part.get('text', '')
            
            # Clean markdown code blocks
            text = text.strip()
            if text.startswith('```json'): # Common start with markdown
                text = text[7:]
            elif text.startswith('```'):
                text = text[3:]
            
            if text.endswith('```'): # Handle end of block
                text = text[:-3]
            
            text = text.strip()
            
            result = json.loads(text)
            
            # Debug: Log what fields were extracted
            logger.info(f"[Pitchdeck] Parsed fields: {list(result.keys())}")
            if 'verifiable_claims' in result:
                logger.info(f"[Pitchdeck] Found {len(result['verifiable_claims'])} verifiable claims")
            else:
                logger.warning("[Pitchdeck] No verifiable_claims in response - Gemini may not have extracted them")
            
            # Ensure required fields have defaults
            if 'company_name' not in result:
                result['company_name'] = 'Unknown Company'
            if 'summary' not in result:
                result['summary'] = 'No summary extracted.'
            if 'usp' not in result:
                result['usp'] = 'Not specified.'
            
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"[Pitchdeck] Failed to parse JSON response: {e}")
            raise ValueError("Failed to parse analysis response")

    def _sanitize_output(self, result: dict) -> dict:
        """
        Sanitize output to prevent XSS and enforce field limits.
        
        - Escapes HTML in all string fields
        - Truncates excessively long fields
        """
        sanitized = {}
        
        for key, value in result.items():
            if key == 'vc_metrics' and isinstance(value, dict):
                sanitized[key] = self._sanitize_vc_metrics(value)
                continue

            if isinstance(value, str):
                # Escape HTML to prevent XSS
                value = html.escape(value)
                
                # Apply field-specific length limits
                if key == 'company_name':
                    value = value[:self.MAX_COMPANY_NAME_LENGTH]
                else:
                    value = value[:self.MAX_FIELD_LENGTH]
                    
            elif isinstance(value, list):
                # Handle list of dicts (like verifiable_claims) vs list of strings (like competition)
                sanitized_list = []
                for item in value[:20]:  # Max 20 items in lists
                    if isinstance(item, dict):
                        # Sanitize string values inside dict, preserve structure
                        sanitized_item = {}
                        for k, v in item.items():
                            if isinstance(v, str):
                                sanitized_item[k] = html.escape(v)[:self.MAX_FIELD_LENGTH]
                            else:
                                sanitized_item[k] = v
                        sanitized_list.append(sanitized_item)
                    elif isinstance(item, str):
                        sanitized_list.append(html.escape(item)[:self.MAX_FIELD_LENGTH])
                    else:
                        sanitized_list.append(item)
                value = sanitized_list
            
            sanitized[key] = value
        
        return sanitized

    def _sanitize_vc_metrics(self, metrics: dict) -> dict:
        """Sanitize the nested vc_metrics structure."""
        sanitized = {}
        valid_assessments = {'Elite', 'Good', 'Caution', 'Red Flag', 'Not Disclosed', 'Pre-Revenue'}

        for metric_name, metric_data in metrics.items():
            if metric_data is None:
                sanitized[metric_name] = None
                continue
            if not isinstance(metric_data, dict):
                continue

            sanitized[metric_name] = {
                'value': html.escape(str(metric_data.get('value', '')))[:100],
                'assessment': (
                    metric_data.get('assessment', 'Not Disclosed')
                    if metric_data.get('assessment') in valid_assessments
                    else 'Not Disclosed'
                ),
                # Hard cap at 200 chars — prompt requests 1 sentence but models can ignore it
                'detail': html.escape(
                    str(metric_data.get('detail', ''))
                )[:200],
            }

        return sanitized

    def verify_market_claims(
        self,
        verifiable_claims: Optional[list] = None,
        market_size: Optional[str] = None,
        competition: Optional[list] = None,
        industry: Optional[str] = None,
        cache_name: Optional[str] = None
    ) -> list:
        """
        Fact-check claims using existing GeminiService.
        
        Args:
            verifiable_claims: List of structured claims from extraction
            market_size: Legacy - claimed market size (fallback)
            competition: Legacy - list of competitor names (fallback)
            industry: Industry category for context
            
        Returns:
            List of findings with verdicts and sources
        """
        # Import here to avoid circular imports
        from backend.services import get_gemini_service
        gemini_svc = get_gemini_service()
        
        findings = []
        industry_ctx = industry or "technology"
        
        # Use verifiable_claims if provided (new structured format)
        if verifiable_claims and isinstance(verifiable_claims, list):
            logger.info(f"[Pitchdeck] Processing {len(verifiable_claims)} verifiable claims")
            # Limit to 5 claims to avoid rate limits and long waits
            for i, claim_obj in enumerate(verifiable_claims[:5]):
                # logger.info(f"[Pitchdeck] Claim {i}: type={type(claim_obj)}, value={str(claim_obj)[:100]}")
                if not isinstance(claim_obj, dict):
                    logger.warning(f"[Pitchdeck] Skipping claim {i}: not a dict")
                    continue
                    
                claim_text = claim_obj.get("claim", "")
                category = claim_obj.get("category", "other")
                source_cited = claim_obj.get("source_cited")
                # NEW: Extract context to help the agent
                claim_context = claim_obj.get("context", "")
                
                logger.info(f"[Pitchdeck] Claim {i}: text='{claim_text[:50]}...', context='{claim_context[:50]}...'")
                
                if not claim_text or len(claim_text) < 5:
                    logger.warning(f"[Pitchdeck] Skipping claim {i}: too short")
                    continue
                
                try:
                    logger.info(f"[Pitchdeck] Verifying claim ({category}): {claim_text[:60]}...")
                    
                    # Construct rich input for the agent so it has full context
                    # This solves the issue of "single claim lacking context"
                    analysis_payload = f"""Claim: "{claim_text}"
Context from Deck: {claim_context}
Source Cited in Deck: {source_cited or 'None'}
Industry: {industry_ctx}"""

                    # ENABLE SMART AGENT OR CACHE
                    # Unified call: always use analyze_claim with smart_agent=True
                    # Pass cache_name if available - backend handles mixing Thinking + Cache
                    result = gemini_svc.analyze_claim(
                        analysis_payload, 
                        smart_agent=True, 
                        cache_name=cache_name
                    )
                    
                    findings.append({
                        "claim_type": category,
                        "original_claim": claim_text,
                        "source_cited": source_cited,
                        "verdict": result.get("verdict", "UNVERIFIED"),
                        "explanation": result.get("explanation", ""),
                        "sources": result.get("sources", [])[:3]
                    })
                except Exception as e:
                    logger.warning(f"[Pitchdeck] Failed to verify claim: {e}")
                    findings.append({
                        "claim_type": category,
                        "original_claim": claim_text,
                        "source_cited": source_cited,
                        "verdict": "UNVERIFIED",
                        "explanation": "Verification unavailable",
                        "sources": []
                    })
            
            logger.info(f"[Pitchdeck] Verification complete: {len(findings)} findings")
            return findings
        
        # Fallback: Use legacy market_size / competition format
        if market_size and market_size.lower() not in ['null', 'not mentioned', 'n/a', '']:
            try:
                claim = f"The {industry_ctx} market size is {market_size}"
                logger.info(f"[Pitchdeck] Verifying market size: {claim[:80]}...")
                
                if cache_name:
                    prompt = f"Claim: {claim}\nContext: Validate this market size claim against the deck context."
                    result = gemini_svc.analyze_claim(prompt, smart_agent=True, cache_name=cache_name)
                else:
                    result = gemini_svc.analyze_claim(claim, smart_agent=True)
                
                findings.append({
                    "claim_type": "market_size",
                    "original_claim": market_size,
                    "verdict": result.get("verdict", "UNVERIFIED"),
                    "explanation": result.get("explanation", ""),
                    "sources": result.get("sources", [])[:3]
                })
            except Exception as e:
                logger.warning(f"[Pitchdeck] Failed to verify market size: {e}")
                findings.append({
                    "claim_type": "market_size",
                    "original_claim": market_size,
                    "verdict": "UNVERIFIED",
                    "explanation": "Verification unavailable",
                    "sources": []
                })
        
        # Fallback: Verify competitors
        if competition and isinstance(competition, list):
            for competitor in competition[:3]:
                if not competitor or not isinstance(competitor, str):
                    continue
                    
                try:
                    claim = f"{competitor} is a company operating in the {industry_ctx} industry"
                    logger.info(f"[Pitchdeck] Verifying competitor: {competitor[:50]}...")
                    
                    result = gemini_svc.analyze_claim(claim)
                    
                    findings.append({
                        "claim_type": "competitor",
                        "original_claim": competitor,
                        "verdict": result.get("verdict", "UNVERIFIED"),
                        "explanation": result.get("explanation", ""),
                        "sources": result.get("sources", [])[:2]
                    })
                except Exception as e:
                    logger.warning(f"[Pitchdeck] Failed to verify competitor {competitor}: {e}")
                    findings.append({
                        "claim_type": "competitor",
                        "original_claim": competitor,
                        "verdict": "UNVERIFIED",
                        "explanation": "Verification unavailable",
                        "sources": []
                    })
        
        logger.info(f"[Pitchdeck] Verification complete: {len(findings)} findings")
        return findings


# Singleton instance
pitchdeck_service = PitchdeckService()
