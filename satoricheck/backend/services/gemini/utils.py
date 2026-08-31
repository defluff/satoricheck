import json
import logging
import re
from datetime import datetime, UTC
from google.genai import types
from backend.services.gemini.client import GeminiServiceClient

logger = logging.getLogger(__name__)

class GeminiServiceUtils(GeminiServiceClient):
    """Text processing, response parsing, and validation utility methods for GeminiService."""

    def _extract_json(self, text: str, expect_array: bool = False) -> str:
        """Robustly extract JSON from text, handling markdown blocks and loose text."""
        if not text:
            return "[]" if expect_array else "{}"
            
        clean_text = text.strip()
        
        # 1. Try markdown code block first
        pattern = r'```(?:json)?\s*(\[[\s\S]*?\]|\{[\s\S]*?\})\s*```'
        json_match = re.search(pattern, clean_text, re.IGNORECASE)
        if json_match:
            return json_match.group(1).strip()
            
        # 2. Fallback: Find first/last bracket based on expected type
        start_char = '[' if expect_array else '{'
        end_char = ']' if expect_array else '}'
        
        start = clean_text.find(start_char)
        end = clean_text.rfind(end_char)
        
        if start != -1 and end != -1 and end > start:
            return clean_text[start:end+1]
            
        # 3. Final attempt: startswith/endswith strip (legacy fallback)
        if clean_text.startswith('```json'):
            clean_text = clean_text[7:]
        elif clean_text.startswith('```'):
            clean_text = clean_text[3:]
        if clean_text.endswith('```'):
            clean_text = clean_text[:-3]
            
        return clean_text.strip()

    def _parse_response(self, response_data):
        """Parse Gemini response into structured format.
        Supports both raw dictionary payload and google-genai response objects.
        """
        try:
            content_text = ""
            if isinstance(response_data, dict):
                # Validate response structure
                if 'candidates' not in response_data or len(response_data['candidates']) == 0:
                    raise ValueError("No candidates in response")
                candidate = response_data['candidates'][0]
                if 'content' not in candidate:
                    raise ValueError("No content in candidate")
                if 'parts' not in candidate['content'] or len(candidate['content']['parts']) == 0:
                    raise ValueError("No parts in content")
                content_text = candidate['content']['parts'][0]['text']
            else:
                # google-genai Response object
                if not response_data.candidates:
                    raise ValueError("No candidates in response")
                candidate = response_data.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    raise ValueError("No content/parts in candidate")
                
                content_text = self._extract_text_from_parts(candidate.content.parts)

            # Extract and parse JSON using unified helper
            json_text = self._extract_json(content_text)
            result = json.loads(json_text)
            
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
            start = end - overlap
            
        return chunks

    def _generate_context_summary(self, text):
        """Generate a short summary of the text for cross-chunk context."""
        sample = text[:6000]
        prompt = f"""Summarize this text in 2-3 sentences. Focus on: who is speaking/writing,
what the main topic is, and any key entities (people, organizations, numbers) mentioned.

TEXT:
\"\"\"
{sample}
\"\"\"

Respond with ONLY the summary, no JSON."""
        
        try:
            if self.client:
                response = self.client.models.generate_content(
                    model=self.MODEL_FAST,
                    contents=prompt
                )
                summary = response.text.strip()
                logger.info(f"Generated context summary: {summary[:80]}...")
                return summary
        except Exception as e:
            logger.warning(f"Context summary generation failed: {e}")
        return None

    @staticmethod
    def _extract_text_from_parts(parts) -> str:
        """Extract concatenated text from response parts, skipping thought parts."""
        return "".join(
            part.text for part in parts
            if not getattr(part, 'thought', False) and part.text
        )

    @staticmethod
    def _normalize_claim_text(claim):
        """Normalize claim text for deduplication. Lowercases and strips punctuation."""
        normalized = claim.strip().lower()
        normalized = normalized.rstrip('.!?')
        normalized = re.sub(r'\s+', ' ', normalized)
        return normalized

    def identify_claims(self, text):
        """Smart Agent: Identify distinct claims in text with full context using chunking."""
        chunks = self._chunk_text(text)
        logger.info(f"Smart Agent potentially processing {len(chunks)} chunks for text length {len(text)}")
        
        context_summary = None
        if len(chunks) > 1:
            context_summary = self._generate_context_summary(text)
        
        seen_normalized = {}
        
        for i, chunk in enumerate(chunks):
            preamble = ""
            if context_summary:
                preamble = f"""GLOBAL CONTEXT (this is part {i+1} of {len(chunks)} from a longer text):
{context_summary}

Use this context to resolve pronouns and references. For example, if the context
mentions "Biden" and this chunk says "He", replace "He" with "Biden".

"""
            
            system_instruction = self._load_skill(
                "claim_extraction",
                fallback="You are a meticulous fact-checker assistant. Your job is to extract EVERY verifiable factual claim from this text."
            )
                
            config = types.GenerateContentConfig(system_instruction=system_instruction)
            
            prompt = f"""{preamble}TEXT TO ANALYZE (Part {i+1}/{len(chunks)}):
\"\"\"
{chunk}
\"\"\"

YOUR TASK:
Go through the text SENTENCE BY SENTENCE, keeping the context of the whole text. Extract up to 15 claims from this chunk according to your guidelines and respond with JSON only matching:
{{"claims": ["claim 1", "claim 2", ...]}}"""

            try:
                if self.client:
                    response = self.client.models.generate_content(
                        model=self.MODEL_FAST,
                        contents=prompt,
                        config=config
                    )
                    content_res = response.text.strip()
                    content_res = self._extract_json(content_res)
                    
                    result = json.loads(content_res.strip())
                    claims = result.get('claims', [])
                    
                    for claim in claims:
                        if claim and isinstance(claim, str) and len(claim.strip()) > 5:
                            clean = claim.strip()
                            normalized = self._normalize_claim_text(clean)
                            if normalized not in seen_normalized:
                                seen_normalized[normalized] = clean
                            
                    logger.info(f"Chunk {i+1}: Found {len(claims)} claims")
            except Exception as e:
                logger.warning(f"Smart Agent chunk {i+1} failed: {e}", exc_info=True)
        
        final_claims = list(seen_normalized.values())
        logger.info(f"Smart Agent total distinct claims found: {final_claims}")
        return final_claims

    def analyze_ai_content(self, text):
        """Analyze text for AI-generation likelihood using the AI Detection skill file."""
        system_instruction = self._load_skill(
            "ai_detection",
            fallback="You are an expert AI text detector."
        )
            
        config = types.GenerateContentConfig(system_instruction=system_instruction)
        current_date_str = datetime.now(UTC).strftime('%B %d, %Y')
        word_count = len(text.split())
        is_short_text_flag = word_count < 50

        prompt = f"""TASK:
Analyze the text below and determine if it was written by an AI language model (like ChatGPT, Claude, Gemini) or by a human.

CONTEXT:
Today's Date: {current_date_str}
Word Count: {word_count} ({'SHORT TEXT SAMPLE <50 words' if is_short_text_flag else 'Standard Length'})

TEXT TO ANALYZE:
\"\"\"
{text}
\"\"\"

INSTRUCTIONS:
1. Apply the Domain & Register Calibration guidelines (Academic, Social, News, Literature, General).
2. IMPORTANT: Evaluate date references relative to Today's Date ({current_date_str}). References to past calendar years or months are standard facts, NOT temporal hallucinations.
3. Be decisive. Avoid middle-ground probabilities like 50% unless truly ambiguous.
4. Identify specific markers (linguistic, structural, lexical, register-specific) found in THIS text.

RESPOND WITH JSON ONLY:
{{
    "ai_probability": <0-100 integer>,
    "confidence": "HIGH" or "MEDIUM" or "LOW",
    "detected_register": "academic" or "social" or "news" or "literature" or "general",
    "is_short_text": {str(is_short_text_flag).lower()},
    "ai_indicators": ["specific markers found in the text"],
    "human_indicators": ["human traits found, if any"],
    "explanation": "2-3 sentence verdict summarizing findings and register calibration"
}}"""

        try:
            if self.client:
                response = self.client.models.generate_content(
                    model=self.MODEL_FAST,
                    contents=prompt,
                    config=config
                )
                content_res = response.text.strip()
                content_res = self._extract_json(content_res)
                result = json.loads(content_res.strip())
                
                if 'ai_probability' not in result:
                    result['ai_probability'] = 50
                if 'confidence' not in result:
                    result['confidence'] = 'LOW' if is_short_text_flag else 'MEDIUM'
                if 'detected_register' not in result:
                    result['detected_register'] = 'general'
                result['is_short_text'] = bool(result.get('is_short_text', is_short_text_flag))
                if 'ai_indicators' not in result:
                    result['ai_indicators'] = []
                if 'human_indicators' not in result:
                    result['human_indicators'] = []
                if 'explanation' not in result:
                    result['explanation'] = 'Unable to fully analyze text.'
                
                def sanitize_indicators(items):
                    if not isinstance(items, list):
                        return []
                    return [str(item) if not isinstance(item, str) else item for item in items]
                
                result['ai_indicators'] = sanitize_indicators(result['ai_indicators'])
                result['human_indicators'] = sanitize_indicators(result['human_indicators'])
                
                logger.info(f"AI Detection result: {result['ai_probability']}% AI probability (Register: {result.get('detected_register')}, Short: {result['is_short_text']})")
                return result
        except Exception as e:
            logger.error(f"AI detection failed: {e}", exc_info=True)
            
        return {
            'ai_probability': 50,
            'confidence': 'LOW',
            'detected_register': 'general',
            'is_short_text': is_short_text_flag,
            'ai_indicators': [],
            'human_indicators': [],
            'explanation': 'Analysis failed.'
        }
