import base64
import json
import logging
import os
import re
import time
from urllib.parse import urlparse, parse_qs
import requests
from google.genai import types
from backend.services.gemini.claims import GeminiServiceClaims

logger = logging.getLogger(__name__)

class GeminiServiceMedia(GeminiServiceClaims):
    """Multimodal media verification, video processing, and embedding methods for GeminiService."""

    @staticmethod
    def extract_youtube_id(url: str) -> str | None:
        """Extract the 11-character YouTube video ID from various URL formats."""
        if not url or not isinstance(url, str):
            return None
        parsed = urlparse(url.strip())
        hostname = (parsed.hostname or '').lower().removeprefix('www.')
        if hostname in ('youtube.com', 'm.youtube.com'):
            if parsed.path == '/watch':
                v = parse_qs(parsed.query).get('v')
                if v and re.match(r'^[a-zA-Z0-9_-]{11}$', v[0]):
                    return v[0]
            elif parsed.path.startswith(('/embed/', '/v/', '/shorts/')):
                parts = [p for p in parsed.path.split('/') if p]
                if len(parts) >= 2 and re.match(r'^[a-zA-Z0-9_-]{11}$', parts[1]):
                    return parts[1]
        elif hostname == 'youtu.be':
            video_id = parsed.path.lstrip('/').split('/')[0].split('?')[0]
            if re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
                return video_id
        return None

    def _prepare_media_part(self, media_input: str | bytes, mime_type: str, input_type: str = 'url') -> types.Part:
        """Helper to prepare a multimodal part for the Gemini SDK."""
        if input_type == 'url':
            url_str = str(media_input)
            if not self._validate_url(url_str):
                raise ValueError("Invalid or restricted URL")
            
            # YouTube URLs are passed natively to Gemini via Part.from_uri without downloading
            yt_id = self.extract_youtube_id(url_str)
            if yt_id:
                canonical_yt = f"https://www.youtube.com/watch?v={yt_id}"
                return types.Part.from_uri(file_uri=canonical_yt, mime_type="video/mp4")

            resp = requests.get(
                url_str,
                timeout=30,
                headers={'User-Agent': 'Mozilla/5.0 (compatible; Authenix/1.0)'},
                stream=True
            )
            resp.raise_for_status()
            data = resp.content
            return types.Part.from_bytes(data=data, mime_type=mime_type)
        
        if input_type == 'file':
            with open(str(media_input), 'rb') as f:
                data = f.read()
            return types.Part.from_bytes(data=data, mime_type=mime_type)
        
        # Assume bytes
        return types.Part.from_bytes(data=media_input, mime_type=mime_type)

    def get_media_embedding(self, media_input: str | bytes, mime_type: str, input_type: str = 'url') -> list:
        """Generate a multimodal embedding for media fingerprinting."""
        try:
            # YouTube URLs stream externally; vector embedding is not supported for external video URIs
            if input_type == 'url' and self.extract_youtube_id(str(media_input)):
                return []

            part = self._prepare_media_part(media_input, mime_type, input_type)
            
            if self.client:
                response = self.client.models.embed_content(
                    model=self.MODEL_EMBEDDING,
                    contents=part
                )
                if response.embeddings:
                    return response.embeddings[0].values
            return []
            
        except Exception as e:
            logger.error(f"Media embedding failed: {e}")
            return []

    def analyze_media_authenticity(self, media_input: str | bytes, input_type: str = 'url', mime_type: str = 'video/mp4') -> dict:
        """Analyze media for authenticity (AI generation, manipulation, deepfakes).
        Uses Gemini Files API for video/large files, otherwise REST.
        """
        uploaded_file = None
        part = None
        try:
            if input_type == 'file' and self.client:
                file_path = str(media_input)
                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

                # Try Files API first
                try:
                    logger.info(f"[Gemini] Uploading file for analysis ({file_size} bytes): {file_path}")
                    uploaded_file = self.client.files.upload(
                        file=file_path,
                        config=types.UploadFileConfig(mime_type=mime_type)
                    )
                    
                    # Wait for processing
                    start_time = time.time()
                    while True:
                        state = getattr(uploaded_file, 'state', None)
                        state_name = getattr(state, 'name', str(state)).upper()
                        
                        if state_name != "PROCESSING":
                            break
                            
                        if time.time() - start_time > 120:  # 2 minute timeout
                            raise TimeoutError("Gemini file processing timed out")
                        time.sleep(2)
                        uploaded_file = self.client.files.get(name=uploaded_file.name)
                    
                    state = getattr(uploaded_file, 'state', None)
                    state_name = getattr(state, 'name', str(state)).upper()
                    if state_name != "ACTIVE":
                        err_msg = getattr(getattr(uploaded_file, 'error', None), 'message', state_name)
                        raise ValueError(f"Gemini file processing failed: {err_msg}")
                    
                    part = uploaded_file
                except Exception as upload_err:
                    logger.warning(f"Gemini files.upload failed ({upload_err}). Checking inline fallback...")
                    if uploaded_file:
                        try:
                            self.client.files.delete(name=uploaded_file.name)
                        except Exception:
                            pass
                        uploaded_file = None

                    # If file is under 20MB, fallback to inline bytes
                    if file_size <= 20 * 1024 * 1024:
                        logger.info(f"Falling back to inline bytes for {file_size} byte file")
                        with open(file_path, 'rb') as f:
                            file_data = f.read()
                        part = types.Part.from_bytes(data=file_data, mime_type=mime_type)
                    else:
                        raise upload_err
            else:
                part = self._prepare_media_part(media_input, mime_type, input_type)

            system_instruction = self._load_skill(
                "media_forensics",
                fallback="Act as a Senior Forensic Media Analyst specializing in deepfake and synthetic content detection."
            )
            
            config = types.GenerateContentConfig(
                system_instruction=system_instruction
            )
            
            is_video = 'video' in mime_type
            is_audio = 'audio' in mime_type
            media_context_hint = "video with audio track" if is_video else ("audio track" if is_audio else "static image")
            
            claims_instruction = (
                " If spoken dialogue or narration is present, extract distinct falsifiable claims "
                "with relative timestamps into the 'claims' array."
                if (is_video or is_audio) else " Set 'claims' to []."
            )

            prompt = (
                f"Conduct a rigorous multi-layered forensic analysis of this {media_context_hint} ({mime_type}). "
                "Evaluate physical lighting/shadows, biometric features, video temporal continuity, audio/voice authenticity "
                f"(splicing, cloning, breath cadence), and compression markers.{claims_instruction} "
                "Respond with JSON matching the required schema."
            )

            cache_name = self.create_cache(part, system_instruction=system_instruction)
            
            if cache_name:
                try:
                    result_data = self.generate_with_cache(cache_name, prompt, config=config)
                    parsed = json.loads(self._extract_json(result_data['text']))
                    parsed['cache_name'] = cache_name
                    parsed.setdefault('claims', [])
                    return parsed
                except Exception as cache_err:
                    logger.warning(f"generate_with_cache failed ({cache_err}), falling back to direct generate_content")
                    self.delete_cache(cache_name)
                    cache_name = None
            
            if self.client:
                # Use modern Client SDK
                response = self.client.models.generate_content(
                    model=self.MODEL_PRO,
                    contents=[part, prompt],
                    config=config
                )
                text = response.text
            else:
                raise Exception("Gemini client not initialized")

            parsed = json.loads(self._extract_json(text))
            parsed.setdefault('claims', [])
            return parsed

        except Exception as e:
            logger.error(f"Media analysis failed: {e}")
            raise e
        finally:
            # Always clean up uploaded Gemini files to avoid storage leaks
            if uploaded_file and self.client:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                    logger.info(f"Deleted Gemini file: {uploaded_file.name}")
                except Exception as cleanup_err:
                    logger.warning(f"Failed to delete Gemini file: {cleanup_err}")

    def transcribe_and_extract_claims_from_audio(
        self,
        audio_bytes: bytes,
        mime_type: str = 'audio/webm',
        context: str | None = None
    ) -> dict:
        """Single-pass multimodal audio transcription and falsifiable claim extraction.

        Uses Gemini Flash to transcribe the audio chunk and extract falsifiable
        claims with timestamps and quotes in a single pass.
        """
        try:
            part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)

            system_instruction = (
                "You are an expert live audio transcriber and real-time fact-checking triage assistant. "
                "Your objective is twofold:\n"
                "1. Accurately transcribe the spoken audio dialogue.\n"
                "2. Extract distinct, falsifiable factual claims from the dialogue that meet the Falsifiability Standard "
                "(can be confirmed or refuted by empirical data, statistics, public records, legislation, or official statements). "
                "Exclude conversational banter, verbal fillers ('you know', 'like', 'literally'), pure opinions, "
                "rhetorical hyperbole lacking empirical anchors, and unfinished speech fragments.\n"
                "Respond ONLY with a JSON object."
            )

            context_hint = f"\nSTREAM CONTEXT: {context[:300]}" if context else ""
            prompt = (
                f"Transcribe this audio slice and extract all verifiable claims.{context_hint}\n"
                "Return a JSON object with this exact structure:\n"
                "{\n"
                '  "transcript": "Verbatim transcript of spoken dialogue in this audio slice...",\n'
                '  "claims": [\n'
                '    {\n'
                '      "claim": "Standalone verifiable factual claim with resolved pronouns",\n'
                '      "timestamp": "00:12",\n'
                '      "quote": "Direct quote from transcript"\n'
                '    }\n'
                '  ]\n'
                "}\n"
                "If no speech or verifiable claims are detected, return empty transcript and empty claims list."
            )

            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                thinking_config=types.ThinkingConfig(
                    thinking_level="minimal"
                )
            )

            if self.client:
                response = self.client.models.generate_content(
                    model=self.MODEL_FAST,
                    contents=[part, prompt],
                    config=config
                )
                text = response.text or "{}"
                parsed = json.loads(self._extract_json(text))
                transcript = parsed.get("transcript", "").strip()
                claims = parsed.get("claims", [])
                word_count = len(transcript.split()) if transcript else 0
                return {
                    "transcript": transcript,
                    "claims": claims if isinstance(claims, list) else [],
                    "word_count": word_count
                }
            else:
                raise Exception("Gemini client not initialized")
        except Exception as e:
            logger.error(f"Audio transcription/extraction failed: {e}")
            raise

