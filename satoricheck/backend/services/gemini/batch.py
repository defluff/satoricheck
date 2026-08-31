import json
import logging
import time
from datetime import datetime, UTC
from google.genai import types
from backend.services.gemini.client import ClaimPriority
from backend.services.gemini.utils import GeminiServiceUtils

logger = logging.getLogger(__name__)

class GeminiServiceBatch(GeminiServiceUtils):
    """Batch claim verification and triage methods for GeminiService."""

    def create_cache(self, content: str | dict | list, ttl_minutes: int = 15) -> str:
        """Create a Context Cache for Gemini."""
        if not self.client:
            return None
        
        contents = []
        if hasattr(content, 'uri') and hasattr(content, 'mime_type'):
            contents = [types.Part.from_uri(file_uri=content.uri, mime_type=content.mime_type)]
        elif isinstance(content, str):
            contents = [content]
        elif isinstance(content, list):
            contents = content
        elif isinstance(content, dict):
            contents = [content]
            
        try:
            ttl_str = f"{ttl_minutes * 60}s"
            cache = self.client.caches.create(
                model=self.MODEL_PRO,
                config=types.CreateCachedContentConfig(
                    contents=contents,
                    ttl=ttl_str
                )
            )
            logger.info(f"Created Gemini Context Cache: {cache.name} at {datetime.now(UTC)} (TTL: {ttl_minutes}m)")
            return cache.name
        except Exception as e:
            logger.error(f"Failed to create context cache: {e}")
            return None
            
    def delete_cache(self, cache_name: str) -> bool:
        """Explicitly delete a Context Cache record."""
        if not cache_name or not self.client:
            return False
        try:
            self.client.caches.delete(name=cache_name)
            logger.info(f"Successfully deleted Gemini Cache: {cache_name}")
            return True
        except Exception as e:
            logger.error(f"Error deleting cache {cache_name}: {e}")
            return False

    def generate_with_cache(self, cache_name: str, prompt: str, config: types.GenerateContentConfig = None) -> dict:
        """Generate content referencing an existing Context Cache."""
        if not self.client:
            raise Exception("Gemini client not initialized")
        try:
            if not config:
                config = types.GenerateContentConfig()
            config.cached_content = cache_name
            response = self.client.models.generate_content(
                model=self.MODEL_PRO,
                contents=prompt,
                config=config
            )
            return {'text': response.text, 'raw': response}
        except Exception as e:
            logger.error(f"Failed to generate with cache: {e}")
            raise

    def analyze_claims_batch(self, claims, context=None, cache_name=None):
        """Agentic Batch Analysis with Flash Triage + Thinking Mode + Tool Calling."""
        if not claims:
            return []
        
        logger.info(f"Starting Batch Analysis for {len(claims)} claims.")
        
        # STEP 1: Fast Triage
        if len(claims) <= 2:
            triaged = [
                {"claim": c, "index": i, "priority": ClaimPriority.NORMAL, "strategy": "SEARCH_VERIFY"}
                for i, c in enumerate(claims)
            ]
        else:
            context_hint = context if isinstance(context, str) else None
            triaged = self.triage_for_stream(claims, stream_context=context_hint)
        
        skip_indices = [t["index"] for t in triaged if t["priority"] == ClaimPriority.SKIP]
        verify_items = [t for t in triaged if t["priority"] != ClaimPriority.SKIP]
        
        results = [None] * len(claims)
        
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
        
        # STEP 2: Setup for verification
        from backend.services.grok_service import get_grok_service
        grok = get_grok_service()
        
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
        
        claims_to_verify = [v["claim"] for v in verify_items]
        
        # STEP 3: Sub-batching
        if len(claims_to_verify) <= self.MAX_CLAIMS_PER_PROMPT:
            sub_batches = [(verify_items, claims_to_verify)]
        else:
            sub_batches = []
            for i in range(0, len(verify_items), self.MAX_CLAIMS_PER_PROMPT):
                chunk_items = verify_items[i:i + self.MAX_CLAIMS_PER_PROMPT]
                chunk_claims = [v["claim"] for v in chunk_items]
                sub_batches.append((chunk_items, chunk_claims))
            logger.info(f"Split {len(claims_to_verify)} claims into {len(sub_batches)} sub-batches")
        
        # STEP 4: Process each sub-batch
        for batch_items, batch_claims in sub_batches:
            strategy_hints = {i + 1: v["strategy"] for i, v in enumerate(batch_items)}
            prompt = self._build_agentic_batch_prompt(batch_claims, inline_context, strategy_hints)
            batch_results = self._run_agentic_batch(
                prompt, batch_claims, grok, cache_name
            )
            
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
        """Execute the agentic loop for a single batch of claims."""
        grok_tool = self._build_grok_tool(grok)
        google_search_tool = types.Tool(google_search=types.GoogleSearch())
        
        conversation = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]
        
        system_instruction = self._load_skill(
            "batch_verification",
            fallback="You are an expert fact-checker. Verify claims accurately and minimize API calls."
        )

        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[google_search_tool, grok_tool],
            tool_config=types.ToolConfig(
                include_server_side_tool_invocations=True
            ),
            thinking_config=types.ThinkingConfig(
                include_thoughts=True
            ),
            temperature=1.0
        )
        
        if cache_name:
            config.cached_content = cache_name
            
        max_turns = 3
        
        for turn in range(max_turns):
            try:
                logger.info(f"Agentic turn {turn + 1}/{max_turns}...")
                
                if self.client:
                    response, cache_cleared = self._generate_with_cache_fallback(
                        model=self.MODEL_PRO,
                        contents=conversation,
                        config=config,
                        context_label=f"batch turn {turn + 1}"
                    )
                    if cache_cleared:
                        cache_name = None
                else:
                    raise Exception("Gemini client not initialized")
                
                if not response.candidates:
                    raise ValueError("No candidates in response")
                    
                candidate = response.candidates[0]
                content = candidate.content
                if not content or not content.parts:
                    raise ValueError("No content or parts in candidate")
                
                # Add model response to conversation to maintain state
                conversation.append(content)
                
                # Check for Function Calls
                if response.function_calls:
                    logger.info(f"Executing {len(response.function_calls)} tool calls...")
                    
                    for fc in response.function_calls:
                        fn_name = fc.name
                        fn_args = fc.args
                        
                        result = {}
                        if fn_name == 'search_social':
                            query = fn_args.get('query', '')
                            logger.info(f"🔍 Social Search: {query[:50]}...")
                            result = grok.search_social(query)
                            if result.get('error'):
                                logger.info(f"Grok failed ({result['error']}), injecting fallback hint.")
                                result['found'] = False
                                result['instruction'] = (
                                    "The social search tool is unavailable. "
                                    "Do NOT call search_social again. "
                                    "Proceed to produce the final JSON verdict using your own knowledge."
                                )
                        elif fn_name == 'google_search':
                            result = {"status": "search_executed", "query": fn_args.get('query')}
                        else:
                            result = {"error": f"Unknown tool: {fn_name}"}
                            
                        function_response_part = types.Part.from_function_response(
                            name=fn_name,
                            response=result
                        )
                        conversation.append(
                            types.Content(role="tool", parts=[function_response_part])
                        )
                    continue  # Next turn
 
                # Extract text (skip thoughts)
                final_text = self._extract_text_from_parts(content.parts)
                
                if final_text:
                    return self._parse_thinking_batch_response(response, len(claims_to_verify))
                    
            except Exception as e:
                logger.error(f"Agentic turn {turn + 1} failed: {e}")
                if turn == max_turns - 1:
                    break
                time.sleep(1)
        
        # Closing turn if final turn ended in function response
        if conversation and conversation[-1].role == "tool":
            try:
                logger.info("Agentic closing turn (producing final answer from tool results)...")
                closing_config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    thinking_config=types.ThinkingConfig(include_thoughts=True),
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingMode.NONE
                        )
                    )
                )
                if cache_name:
                    closing_config.cached_content = cache_name
                    
                response, _ = self._generate_with_cache_fallback(
                    model=self.MODEL_PRO,
                    contents=conversation,
                    config=closing_config,
                    context_label="batch closing turn"
                )
                if response.candidates:
                    return self._parse_thinking_batch_response(response, len(claims_to_verify))
            except Exception as e:
                logger.error(f"Agentic closing turn failed: {e}")
        
        # Fallback
        logger.error("Agentic sub-batch failed after all turns")
        return [{
            "is_claim": True,
            "verdict": "COULD_NOT_VERIFY",
            "explanation": "Agentic analysis failed",
            "sources": []
        } for _ in claims_to_verify]
 
    def _build_agentic_batch_prompt(self, claims, inline_context=None, strategy_hints=None):
        """Build the user prompt containing target claims list and any inline context."""
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
        
        from datetime import datetime, UTC
        current_date_str = datetime.now(UTC).strftime('%B %d, %Y')
        
        return f"""CONTEXT:
Today's Date: {current_date_str}

CLAIMS TO ANALYZE:
{claims_str}
{context_block}
 
For each claim, select the appropriate strategy from your guidelines, verify it, and respond with a JSON results array in the same order."""
 
    def _parse_thinking_batch_response(self, response_data, expected_count):
        """Parse the one-shot thinking response into structured results."""
        try:
            content_text = ""
            if isinstance(response_data, dict):
                if 'candidates' not in response_data or not response_data['candidates']:
                    raise ValueError("No candidates in response")
                candidate = response_data['candidates'][0]
                parts = candidate.get('content', {}).get('parts', [])
                for part in parts:
                    if 'text' in part and not part.get('thought'):
                        content_text += part['text']
            else:
                # google-genai Response object
                if not response_data.candidates:
                    raise ValueError("No candidates in response")
                candidate = response_data.candidates[0]
                content_text = self._extract_text_from_parts(candidate.content.parts)
 
            json_text = self._extract_json(content_text)
            result_data = json.loads(json_text)
            results = result_data.get('results', [])
            
            normalized = []
            used_indices = set()
            for i in range(expected_count):
                claim_result = None
                for r in results:
                    if r.get('claim_index') == i + 1:
                        claim_result = r
                        break
                
                if not claim_result and i < len(results):
                    claim_result = results[i]
                    if claim_result.get('claim_index') in used_indices:
                        claim_result = None
                
                if claim_result:
                    used_indices.add(claim_result.get('claim_index'))
                    result = {
                        "is_claim": True,
                        "verdict": claim_result.get('verdict', 'UNVERIFIABLE'),
                        "explanation": claim_result.get('explanation', ''),
                        "sources": claim_result.get('sources', [])[:5],
                        "fallacy": claim_result.get('fallacy'),
                        "is_quote_claim": claim_result.get('is_quote_claim', False),
                        "quote_attribution": claim_result.get('quote_attribution'),
                        "quote_verified": claim_result.get('quote_verified'),
                        "quote_source": claim_result.get('quote_source'),
                        "meta_truth_verdict": claim_result.get('meta_truth_verdict'),
                    }
                    if claim_result.get('social_context'):
                        result["social_context"] = claim_result['social_context']
                    normalized.append(result)
                else:
                    logger.warning(f"No result for claim {i+1}/{expected_count}.")
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
 
    def triage_for_stream(self, claims: list, stream_context: str = None) -> list:
        """Fast triage using Flash-Lite to categorize claims by priority and strategy."""
        if not claims:
            return []
        
        claims_str = "\n".join([f"{i+1}. {c}" for i, c in enumerate(claims)])
        
        context_hint = ""
        if stream_context:
            context_hint = f"\nSOURCE CONTEXT: {stream_context[:200]}"
        
        system_instruction = (
            "You are a fact-checking triage assistant. Categorize each input claim for fact-check priority "
            "and strategy based on the instructions. Respond ONLY with a JSON array, one object per claim."
        )
        
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=1.0,
        )
        
        prompt = f"""CLAIMS TO CATEGORIZE:
{claims_str}
{context_hint}

For each claim, classify:
- priority: SKIP (opinion/trivial), IMMEDIATE (breaking/viral), NORMAL (standard), DEFERRED (low-priority)
- strategy: KNOWLEDGE_CHECK (well-known fact), SEARCH_VERIFY (needs sources), SOCIAL_VERIFY (viral/social)

Return ONLY a JSON array matching:
[{{"index": 1, "priority": "NORMAL", "strategy": "SEARCH_VERIFY"}}, ...]"""
 
        try:
            if self.client:
                response = self.client.models.generate_content(
                    model=self.MODEL_FAST,
                    contents=prompt,
                    config=config
                )
                text = response.text
                
                json_text = self._extract_json(text, expect_array=True)
                triage_results = json.loads(json_text)
                
                priority_map = {
                    "skip": ClaimPriority.SKIP,
                    "immediate": ClaimPriority.IMMEDIATE,
                    "normal": ClaimPriority.NORMAL,
                    "deferred": ClaimPriority.DEFERRED
                }
                
                results = []
                for i, claim in enumerate(claims):
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
                        results.append({
                            "claim": claim,
                            "index": i,
                            "priority": ClaimPriority.NORMAL,
                            "strategy": "SEARCH_VERIFY"
                        })
                
                skip_count = sum(1 for r in results if r["priority"] == ClaimPriority.SKIP)
                immediate_count = sum(1 for r in results if r["priority"] == ClaimPriority.IMMEDIATE)
                logger.info(f"📊 Triage: {len(claims)} claims → {skip_count} SKIP, {immediate_count} IMMEDIATE, {len(claims) - skip_count - immediate_count} NORMAL/DEFERRED")
                return results
                
        except Exception as e:
            logger.warning(f"Triage failed, defaulting all to NORMAL: {e}")
            
        return [
            {
                "claim": c,
                "index": i,
                "priority": ClaimPriority.NORMAL,
                "strategy": "SEARCH_VERIFY"
            }
            for i, c in enumerate(claims)
        ]
