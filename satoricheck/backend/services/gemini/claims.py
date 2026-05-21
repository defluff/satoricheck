import json
import logging
import re
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
            
            grok_tool_dict = grok.get_tool_definition()
            grok_tool = types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=grok_tool_dict.get("name"),
                        description=grok_tool_dict.get("description"),
                        parameters=types.Schema(
                            type="OBJECT",
                            properties={
                                k: types.Schema(
                                    type=v.get("type", "STRING").upper(),
                                    description=v.get("description", "")
                                )
                                for k, v in grok_tool_dict.get("parameters", {}).get("properties", {}).items()
                            },
                            required=grok_tool_dict.get("parameters", {}).get("required", [])
                        )
                    )
                ]
            )
            
            social_hint = should_fire_grok(text)
            if social_hint:
                logger.info(f"Social trigger detected for: {text[:50]}...")
            
            prompt = self._build_fact_check_prompt(text, agentic=True, social_hint=social_hint)
            
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
                        if self.client:
                            response = self.client.models.generate_content(
                                model=self.MODEL_PRO,
                                contents=conversation_history,
                                config=config
                            )
                            turn_response = response
                            break
                    except Exception as e:
                        logger.warning(f"Agentic turn {current_turn} failed (attempt {attempt+1}): {e}")
                        turn_exception = e
                        if config.cached_content:
                            logger.warning(f"Cache {config.cached_content} failed/conflicted, clearing cache and retrying turn immediately...")
                            config.cached_content = None
                            cache_name = None  # Clear so subsequent turns skip caching
                            try:
                                response = self.client.models.generate_content(
                                    model=self.MODEL_PRO,
                                    contents=conversation_history,
                                    config=config
                                )
                                turn_response = response
                                break
                            except Exception as retry_err:
                                logger.warning(f"Retry without cache failed: {retry_err}")
                                turn_exception = retry_err
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
                final_json_text = ""
                for part in content.parts:
                    if not getattr(part, 'thought', False) and part.text:
                        final_json_text += part.text

                # Final Result Extraction
                if final_json_text and "verdict" in final_json_text:
                    clean_json = final_json_text.strip()
                    
                    # 1. Try to extract from Markdown code blocks
                    json_block_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', clean_json, re.IGNORECASE)
                    if json_block_match:
                        clean_json = json_block_match.group(1)
                    else:
                        # 2. Fallback: Find outermost curly braces
                        start_idx = clean_json.find('{')
                        end_idx = clean_json.rfind('}')
                        if start_idx != -1 and end_idx != -1:
                            clean_json = clean_json[start_idx:end_idx+1]
                    
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
                prompt = self._build_fact_check_prompt(text)
                logger.info(f"Sending fact-check request (attempt {attempt + 1}) for text: {text[:100]}...")
                
                if self.client:
                    # Enable Google Search Grounding tool
                    config = types.GenerateContentConfig(
                        tools=[types.Tool(google_search=types.GoogleSearch())]
                    )
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

    def _build_fact_check_prompt(self, text, agentic=False, social_hint=False):
        """Build the fact-checking prompt with Meta-Truth awareness."""
        tool_block = ""
        if agentic:
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

        return f"""You are an elite, impartial fact-checker specializing in detecting misinformation and state propaganda.
Analyze the following text with extreme skepticism.
{tool_block}

CRITICAL DETECTION - QUOTE CLAIMS:
If the text contains phrases like "X said", "X claimed", "X stated", "according to X", then this is a QUOTE CLAIM.
For QUOTE CLAIMS, you MUST set is_quote_claim=true and fill in ALL quote fields.

EXAMPLE - Quote Claim:
"Donald Trump posted that he will visit Mars tomorrow"
{{
  "is_claim": true,
  "verdict": "FALSE",
  "explanation": "No official announcement of this exists on Donald Trump's verified accounts.",
  "fallacy": "None",
  "sources": [],
  "is_quote_claim": true,
  "quote_attribution": "Donald Trump",
  "quote_verified": false,
  "quote_source": "Social Media Post",
  "meta_truth_verdict": "FALSE"
}}

EXAMPLE - Regular Claim:
"The moon is made of cheese"
{{
  "is_claim": true,
  "verdict": "FALSE",
  "explanation": "The moon is composed of rock and dust, not dairy products.",
  "fallacy": "Factual Error",
  "sources": ["https://nasa.gov/moon-composition"],
  "is_quote_claim": false,
  "quote_attribution": null,
  "quote_verified": null,
  "quote_source": null,
  "meta_truth_verdict": "FALSE"
}}

Respond ONLY with a JSON object (matching the format of the examples above) containing:
- is_claim: boolean (is this text a verifiable factual claim?)
- verdict: 'TRUE', 'FALSE', 'MISLEADING', 'PARTIALLY TRUE', or 'COULD_NOT_VERIFY'
- explanation: A concise 2-sentence explanation of the verdict based on evidence.
- fallacy: Name of logical fallacy if present (e.g. 'Ad Hominem', 'Strawman', 'False Dilemma'), or null.
- sources: A list of 1-5 reputable, live website URLs used to verify the claim.
- source_reliability: 'HIGH', 'MEDIUM', or 'LOW' based on your sources.
- is_quote_claim: boolean (true if verifying whether X actually said Y)
- quote_attribution: string (the speaker/author, e.g. "Donald Trump") or null
- quote_verified: boolean (did they actually say/post this?) or null
- quote_source: string (where they said it, e.g. "interview on NBC") or null
- meta_truth_verdict: 'TRUE' (they said it and it is true), 'FALSE' (they did not say it OR they said it but it is false), 'MISLEADING', or 'PARTIALLY TRUE'.

CLAIM TO ANALYZE:
"{text}"
"""
