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
    ],
    "vc_metrics": {
        "monthly_revenue_arr": { "value": "€85K MRR", "assessment": "Good",       "detail": "..." },
        "burn_multiple":      { "value": "1.2x",     "assessment": "Good",       "detail": "..." },
        "nrr_percent":        { "value": "115%",     "assessment": "Elite",      "detail": "..." },
        "cac_payback_months": { "value": "8",        "assessment": "Good",       "detail": "..." },
        "ltv_cac_ratio":      { "value": "4:1",      "assessment": "Good",       "detail": "..." },
        "runway_months":      { "value": "18",       "assessment": "Good",       "detail": "..." }
    }
}

VC METRICS RULES:
- This tool analyses STARTUP pitch decks (startups seeking investment from angels, VCs, or grant bodies).
- Extract ONLY from figures explicitly stated in the deck. Do NOT infer or fabricate.
- "assessment" must be exactly one of: "Elite", "Good", "Caution", "Red Flag", "Not Disclosed", "Pre-Revenue".
- "value" is the raw figure verbatim from the deck (e.g. "€85K MRR", "£1.2M ARR", "1.2x", "18"). ALWAYS use the currency symbol and amount exactly as stated — do NOT convert to USD. If the metric cannot be extracted and the startup is NOT pre-revenue, set to null.
- "detail" is ONE sentence (max 200 characters) from an investor's perspective on what this signals.

PRE-REVENUE RULE — apply when the startup has no revenue yet:
  monthly_revenue_arr → { "value": "Pre-Revenue", "assessment": "Pre-Revenue", "detail": "No revenue yet — investors are evaluating team, market size, and early traction signals." }
  burn_multiple, nrr_percent, cac_payback_months, ltv_cac_ratio → null (require revenue to calculate).
  runway_months → still extract if cash balance and monthly burn are both stated.

BENCHMARK TIERS (2026):

  Monthly Revenue / ARR (report MRR or ARR exactly as stated in the deck, including currency symbol):
    Elite: >1M | Good: 100K–1M | Caution: <100K | Pre-Revenue: no revenue yet.
    Thresholds apply in whatever currency the deck uses — do NOT convert. Use "Not Disclosed" only if revenue exists but no figure is stated.

  Burn Multiple (Net Burn ÷ Net New ARR):
    Elite: <1x | Good: 1.0–1.5x | Caution: 1.5–2x | Red Flag: >2x
    → null if pre-revenue OR if burn rate and ARR growth are not both stated.

  NRR / Net Revenue Retention (%):
    Elite: >120% | Good: 100–120% | Caution: 80–100% | Red Flag: <80%
    → null if pre-revenue OR if no churn, retention, or expansion revenue data stated.

  CAC Payback (months to recover CAC from gross margin):
    Elite: <6 mo | Good: 6–12 mo | Caution: 12–18 mo | Red Flag: >18 mo
    → null if pre-revenue OR if CAC or payback period not stated.

  LTV:CAC Ratio:
    Elite: ≥5:1 | Good: 3–5:1 | Caution: 1.5–3:1 | Red Flag: <1.5:1
    → null if pre-revenue OR if LTV or CAC not stated.

  Cash Runway (months at current burn):
    Elite: >24 mo | Good: 18–24 mo | Caution: 12–18 mo | Red Flag: <12 mo
    → null if cash balance or monthly burn not stated.

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
            }],
            "generationConfig": {
                "thinkingConfig": {
                    "includeThoughts": True
                }
            }
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
