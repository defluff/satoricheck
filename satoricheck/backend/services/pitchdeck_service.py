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
    
    # Vision-capable model for PDF analysis
    MODEL_VISION = "gemini-3-pro-preview"
    
    # Maximum PDF size: 25MB
    MAX_FILE_SIZE = 25 * 1024 * 1024
    
    # Field length limits for sanitization
    MAX_FIELD_LENGTH = 5000
    MAX_COMPANY_NAME_LENGTH = 200
    
    # API timeout for vision analysis (longer than text)
    TIMEOUT = 60
    
    def __init__(self):
        """Initialize Pitchdeck service."""
        self.api_key = Config.GEMINI_API_KEY
        if not self.api_key:
            logger.error("GEMINI_API_KEY is not set for PitchdeckService!")
        else:
            logger.info("[Pitchdeck] ✓ Service initialized")
    
    def _get_api_url(self, model: str) -> str:
        """Get API URL for a specific model (matches factcheck pattern)."""
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}"

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
        
        max_retries = 3
        retry_delay = 1
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                # logger.info(f"[Pitchdeck] Sending analysis request (Attempt {attempt+1})")
                raw_result = self._call_gemini_vision(pdf_bytes, prompt)
                
                # If successful, break loop
                break
                
            except requests.exceptions.Timeout:
                logger.warning(f"[Pitchdeck] Timeout on attempt {attempt+1}")
                last_exception = TimeoutError("Analysis timed out. Please try again.")
                
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response else None
                logger.warning(f"[Pitchdeck] HTTP {status_code} on attempt {attempt+1}: {e}")
                
                if status_code == 429:
                    last_exception = Exception("Rate limit reached. Please try again later.")
                elif status_code and 500 <= status_code < 600:
                    last_exception = Exception("Service temporarily unavailable. Please try again.")
                else:
                    # 4xx errors (client side) shouldn't be retried usually, unless transient
                    last_exception = Exception(f"API error: {status_code}")
                    if 400 <= status_code < 500:
                         raise last_exception # Don't retry client errors
            
            except Exception as e:
                logger.warning(f"[Pitchdeck] Error on attempt {attempt+1}: {e}")
                last_exception = e
                
            # If we are here, we failed. Wait and retry if not last attempt
            if attempt < max_retries - 1:
                import time
                time.sleep(retry_delay)
                retry_delay *= 2
        else:
            # Loop finished without break = all attempts failed
            raise last_exception or Exception("Analysis failed after retries")
        
        # =================================================================
        # PARSE AND SANITIZE RESPONSE
        # =================================================================
        
        result = self._parse_vision_response(raw_result)
        result = self._sanitize_output(result)
        
        return result

    def _is_valid_pdf(self, pdf_bytes: bytes) -> bool:
        """Check if bytes represent a valid PDF file."""
        # PDF files start with %PDF-
        if len(pdf_bytes) < 5:
            return False
        return pdf_bytes[:5] == b'%PDF-'

    def _build_analysis_prompt(self) -> str:
        """Build the prompt for pitch deck analysis."""
        return """You are an expert VC analyst reviewing a pitch deck. Analyze this presentation thoroughly.

TASK: Extract key information AND verifiable claims from this pitch deck.

ANALYZE BOTH TEXT AND VISUALS:
- Read all text content carefully
- Interpret charts, graphs, and diagrams
- Note team photos and product screenshots
- Extract data from tables and infographics

REQUIRED OUTPUT (JSON format):
{
    "company_name": "The company/startup name",
    "summary": "2-3 sentence summary of what the company does and their core product/service",
    "usp": "Their unique selling proposition - what makes them different from competitors",
    "industry": "The broad industry category (e.g., CleanTech, FinTech, HealthTech, SaaS, AI/ML, E-commerce)",
    "sector": "The specific vertical or niche (e.g., Water Purification, Payment Processing, Diagnostics)",
    "market_size": "Total addressable market size if mentioned (e.g., '$50B by 2030'). Include source if stated.",
    "competition": ["Competitor 1", "Competitor 2"],
    "team_highlights": "Brief note on founders/team if shown",
    "funding_ask": "Amount they're raising if mentioned",
    "verifiable_claims": [
        {
            "claim": "The exact claim as stated (e.g., 'Global water purification market will reach $60B by 2030')",
            "category": "market_size | revenue | growth_rate | roi | customer_count | cost_savings | competitor | technology | other",
            "source_cited": "Source mentioned in deck if any (e.g., 'Statista', 'Company data', null)",
            "is_quantitative": true,
            "context": "Brief context about where this claim appears (e.g., 'Market slide', 'Financial projections')"
        }
    ]
}

CLAIM EXTRACTION RULES:
- Extract ALL quantitative claims (numbers, percentages, dollar amounts, growth rates)
- Prioritize claims about: market size, revenue, growth rates, ROI, customer acquisition cost, unit economics
- Include claims about competitor comparisons, technology advantages, or market positioning
- Note if the deck cites a source (Statista, Gartner, SEC filings, etc.)
- Maximum 10 claims, prioritize the most significant ones

GENERAL RULES:
- Be concise and factual
- Only include information actually present in the deck
- If a field is not mentioned, use null
- Extract exact numbers/stats when shown
- For industry/sector, infer from context if not explicitly stated

Respond ONLY with valid JSON, no additional text."""

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
        
        # Build multimodal request with PDF
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
        
        url = self._get_api_url(self.MODEL_VISION)
        
        logger.info(f"[Pitchdeck] Sending PDF to Gemini Vision ({len(pdf_bytes)} bytes)")
        
        response = requests.post(
            url,
            json=payload,
            headers={'Content-Type': 'application/json'},
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
            
            text = candidate['content']['parts'][0].get('text', '')
            
            # Clean markdown code blocks
            text = text.strip()
            if text.startswith('```json'):
                text = text[7:]
            elif text.startswith('```'):
                text = text[3:]
            if text.endswith('```'):
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

    def verify_market_claims(
        self,
        verifiable_claims: Optional[list] = None,
        market_size: Optional[str] = None,
        competition: Optional[list] = None,
        industry: Optional[str] = None
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
                logger.info(f"[Pitchdeck] Claim {i}: type={type(claim_obj)}, value={str(claim_obj)[:100]}")
                if not isinstance(claim_obj, dict):
                    logger.warning(f"[Pitchdeck] Skipping claim {i}: not a dict")
                    continue
                    
                claim_text = claim_obj.get("claim", "")
                category = claim_obj.get("category", "other")
                source_cited = claim_obj.get("source_cited")
                
                logger.info(f"[Pitchdeck] Claim {i}: text='{claim_text[:50]}...', len={len(claim_text)}")
                
                if not claim_text or len(claim_text) < 5:
                    logger.warning(f"[Pitchdeck] Skipping claim {i}: too short")
                    continue
                
                try:
                    logger.info(f"[Pitchdeck] Verifying claim ({category}): {claim_text[:60]}...")
                    
                    result = gemini_svc.analyze_claim(claim_text)
                    
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
                
                result = gemini_svc.analyze_claim(claim)
                
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
