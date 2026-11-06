import json
import logging
import time
from google.genai import types
from backend.services.gemini.batch import GeminiServiceBatch

logger = logging.getLogger(__name__)

class GeminiServiceClaims(GeminiServiceBatch):
    """Claim verification methods for GeminiService (Standard & Agentic loops)."""

    def analyze_claim(self, text, smart_agent=False, cache_name=None):
        """Public entry point for claim analysis."""
        if smart_agent:
            return self._analyze_claim_agentic(text, cache_name)
        return self._analyze_claim_standard(text)

    def _analyze_claim_agentic(self, text, cache_name=None):
        """Agentic analysis loop with Thinking Mode and Tool Use."""
        try:
            from backend.services.grok_service import get_grok_service, should_fire_grok
            grok = get_grok_service()
            
            social_hint = should_fire_grok(text)
            if social_hint:
                logger.info(f"Social trigger detected for: {text[:50]}...")
            
            system_instruction = self._load_skill(
                "fact_checking",
                fallback="You are an elite, impartial fact-checker specializing in detecting misinformation and state propaganda."
            )
            
            if social_hint:
                tool_block = """

AVAILABLE TOOLS:
- search_social(query): Search X/Twitter for real-time social context.
  USE when: quote claims ("X said", "according to X"), @handles, #hashtags,
  breaking/viral news, temporal keywords (today, just, now, breaking),
  or when you cannot verify attribution from your knowledge alone.
  DO NOT USE for: well-established historical facts, science, geography.

⚠️ SOCIAL CONTEXT RECOMMENDED: This claim matches social triggers (quote attribution,
breaking news, or social reference). You SHOULD call search_social to verify attribution
before rendering your verdict."""
                system_instruction_full = f"{system_instruction}\n\n{tool_block}"
            else:
                system_instruction_full = system_instruction
            
            from datetime import datetime, UTC
            current_date_str = datetime.now(UTC).strftime('%B %d, %Y')
            
            prompt = f"""CONTEXT:
Today's Date: {current_date_str}

CLAIM TO ANALYZE:
"{text}"

Verify the claim and respond with JSON matching the required schema."""
            
            log_prefix = f"AGENTIC (Thinking{' + Cache' if cache_name else ''})"
            logger.info(f"Starting {log_prefix} analysis for: {text[:50]}...")

            def _single_claim_parser(final_json_text, response):
                if final_json_text and "verdict" in final_json_text:
                    clean_json = self._extract_json(final_json_text)
                    try:
                        json.loads(clean_json)
                        return self._parse_response(response)
                    except json.JSONDecodeError:
                        logger.warning(f"Agent emitted invalid JSON: {clean_json[:50]}...")
                logger.warning("Agent stopped without JSON or Tool call. Forcing another turn...")
                return None

            parsed = self._run_verification_loop(
                prompt=prompt,
                system_instruction=system_instruction_full,
                needs_social=social_hint,
                grok=grok,
                response_parser=_single_claim_parser,
                cache_name=cache_name,
                max_turns=4,
                retries_per_turn=2,
                raise_on_turn_failure=True,
                enable_closing_turn=True,
                context_label_prefix="agentic",
            )
            if parsed is not None:
                return parsed

            raise Exception("Agent exceeded max turns without final answer")
 
        except Exception as e:
            logger.error(f"Agentic loop error: {e}")
            return self._analyze_claim_standard(text)
 
    def _analyze_claim_standard(self, text):
        """Standard claim analysis using Gemini Pro with Google Search grounding."""
        # 1. Check Cache (Exact Match)
        try:
            from backend.database import db_session
            from backend.models import FactCheck
            
            cached = db_session.query(FactCheck).filter(
                FactCheck.claim_text == text
            ).order_by(FactCheck.timestamp.desc()).first()
            
            if cached:
                logger.info(f"Cache hit for text: {text[:50]}...")
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
 
        # 2. Call API with Retries
        max_retries = 3
        retry_delay = 1
        last_exception = None
 
        for attempt in range(max_retries):
            try:
                system_instruction = self._load_skill(
                    "fact_checking",
                    fallback="You are an elite, impartial fact-checker specializing in detecting misinformation and state propaganda."
                )
                
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    thinking_config=types.ThinkingConfig(
                        thinking_level="high"
                    )
                )
                
                from datetime import datetime, UTC
                current_date_str = datetime.now(UTC).strftime('%B %d, %Y')
                
                prompt = f"""CONTEXT:
Today's Date: {current_date_str}

CLAIM TO ANALYZE:
"{text}"

Verify the claim and respond with JSON matching the required schema."""
                
                logger.info(f"Sending fact-check request (attempt {attempt + 1}) for text: {text[:100]}...")
                
                if self.client:
                    response = self.client.models.generate_content(
                        model=self.MODEL,
                        contents=prompt,
                        config=config
                    )
                    result = self._parse_response(response)
                    logger.info(f"Fact-check result: {result['verdict']}")
                    return result
                
            except Exception as e:
                logger.error(f"Gemini API error (Attempt {attempt + 1}): {str(e)}", exc_info=True)
                last_exception = str(e)
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
 
        logger.error(f"Gemini API failed after {max_retries} attempts. Last error: {last_exception}")
        
        if last_exception and "429" in last_exception:
             return {
                'is_claim': True,
                'verdict': 'COULD_NOT_VERIFY',
                'explanation': 'System is experiencing high traffic. Please try again in a few minutes.',
                'fallacy': None,
                'sources': []
            }
            
        raise Exception("Fact-check service temporarily unavailable. Please try again.")
