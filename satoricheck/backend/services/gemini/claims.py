import json
import logging
import time
from google.genai import types
from backend.services.gemini.batch import GeminiServiceBatch

logger = logging.getLogger(__name__)

class GeminiServiceClaims(GeminiServiceBatch):
    """Claim verification methods for GeminiService (Standard & Agentic loops)."""

    def _analyze_claim_safe(self, text, smart_agent=False, cache_name=None):
        """Thread-safe wrapper for analyze_claim.
        Ensures database session is removed after execution.
        """
        try:
            return self.analyze_claim(text, smart_agent, cache_name)
        finally:
            from backend.database import db_session
            db_session.remove()

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
            
            grok_tool = self._build_grok_tool(grok)
            
            social_hint = should_fire_grok(text)
            if social_hint:
                logger.info(f"Social trigger detected for: {text[:50]}...")
            
            system_instruction = self._load_skill(
                "fact_checking",
                fallback="You are an elite, impartial fact-checker specializing in detecting misinformation and state propaganda."
            )
            
            tool_block = """

AVAILABLE TOOLS:
- search_social(query): Search X/Twitter for real-time social context.
  USE when: quote claims ("X said", "according to X"), @handles, #hashtags,
  breaking/viral news, temporal keywords (today, just, now, breaking),
  or when you cannot verify attribution from your knowledge alone.
  DO NOT USE for: well-established historical facts, science, geography."""
            if social_hint:
                tool_block += """

⚠️ SOCIAL CONTEXT RECOMMENDED: This claim matches social triggers (quote attribution,
breaking news, or social reference). You SHOULD call search_social to verify attribution
before rendering your verdict."""

            system_instruction_full = f"{system_instruction}\n\n{tool_block}"
            
            prompt = f"""CLAIM TO ANALYZE:
"{text}"

Verify the claim and respond with JSON matching the required schema."""
            
            conversation_history = [
                types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
            ]
            
            max_turns = 5 
            current_turn = 0
            
            log_prefix = f"AGENTIC (Thinking{' + Cache' if cache_name else ''})"
            logger.info(f"Starting {log_prefix} analysis for: {text[:50]}...")
            
            while current_turn < max_turns:
                current_turn += 1
                
                # Configure generation with Thinking Mode, Google Search, and Grok tool
                google_search_tool = types.Tool(google_search=types.GoogleSearch())
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction_full,
                    tools=[google_search_tool, grok_tool],
                    tool_config=types.ToolConfig(
                        include_server_side_tool_invocations=True
                    ),
                    thinking_config=types.ThinkingConfig(
                        include_thoughts=True
                    )
                )
                
                if cache_name:
                    config.cached_content = cache_name
                
                turn_response = None
                turn_exception = None
                
                for attempt in range(2):
                    try:
                        response, cache_cleared = self._generate_with_cache_fallback(
                            model=self.MODEL_PRO,
                            contents=conversation_history,
                            config=config,
                            context_label=f"agentic turn {current_turn}"
                        )
                        if cache_cleared:
                            cache_name = None
                        turn_response = response
                        break
                    except Exception as e:
                        logger.warning(f"Agentic turn {current_turn} failed (attempt {attempt+1}): {e}")
                        turn_exception = e
                        time.sleep(1)
                
                if not turn_response:
                    raise turn_exception or Exception("Agentic turn failed after retries")
 
                response = turn_response
                
                if not response.candidates:
                    raise ValueError("No candidates in response")
                    
                candidate = response.candidates[0]
                content = candidate.content
                if not content or not content.parts:
                    raise ValueError("No content or parts in candidate")
                
                # Check for Function Calls
                if response.function_calls:
                    # Append the model's reply (containing function call parts) to maintain history
                    conversation_history.append(content)
                    
                    logger.info(f"Agent decided to call {len(response.function_calls)} tools.")
                    for fc in response.function_calls:
                        fn_name = fc.name
                        fn_args = fc.args
                        
                        tool_result = {}
                        if fn_name == 'search_social':
                            query = fn_args.get('query')
                            logger.info(f"🛠️ Executing Tool: search_social(query='{query}')")
                            tool_result = grok.search_social(query)
                        else:
                            tool_result = {"error": f"Unknown tool: {fn_name}"}
                            
                        # Add tool response part in correct role format
                        function_response_part = types.Part.from_function_response(
                            name=fn_name,
                            response=tool_result
                        )
                        conversation_history.append(
                            types.Content(role="tool", parts=[function_response_part])
                        )
                    continue
 
                # Parse response text
                final_json_text = self._extract_text_from_parts(content.parts)
 
                # Final Result Extraction
                if final_json_text and "verdict" in final_json_text:
                    clean_json = self._extract_json(final_json_text)
                    
                    try:
                        json.loads(clean_json)
                        # Mock up candidate for parsing method compatibility
                        return self._parse_response(response)
                    except json.JSONDecodeError:
                        logger.warning(f"Agent emitted invalid JSON: {clean_json[:50]}...")
                
                logger.warning("Agent stopped without JSON or Tool call. Forcing another turn...")
                if current_turn >= max_turns:
                    break
            
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
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
                
                prompt = f"""CLAIM TO ANALYZE:
"{text}"

Verify the claim and respond with JSON matching the required schema."""
                
                logger.info(f"Sending fact-check request (attempt {attempt + 1}) for text: {text[:100]}...")
                
                if self.client:
                    response = self.client.models.generate_content(
                        model=self.MODEL_PRO,
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
