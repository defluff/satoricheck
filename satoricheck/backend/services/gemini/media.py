import base64
import json
import logging
import time
import requests
from google.genai import types
from backend.services.gemini.claims import GeminiServiceClaims

logger = logging.getLogger(__name__)

class GeminiServiceMedia(GeminiServiceClaims):
    """Multimodal media verification, video processing, and embedding methods for GeminiService."""

    def _prepare_media_part(self, media_input: str | bytes, mime_type: str, input_type: str = 'url') -> types.Part:
        """Helper to prepare a multimodal part for the Gemini SDK."""
        if input_type == 'url':
            if not self._validate_url(str(media_input)):
                raise ValueError("Invalid or restricted URL")
            resp = requests.get(
                str(media_input),
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
        try:
            if input_type == 'file' and self.client:
                logger.info(f"[Gemini] Uploading file for analysis: {media_input}")
                uploaded_file = self.client.files.upload(
                    file=str(media_input),
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
                    raise ValueError(f"Gemini file processing failed with state: {state_name}")
                
                part = uploaded_file
            else:
                part = self._prepare_media_part(media_input, mime_type, input_type)

            system_instruction = self._load_skill(
                "media_forensics",
                fallback="Act as a Senior Forensic Media Analyst specializing in deepfake and synthetic content detection."
            )
            
            config = types.GenerateContentConfig(
                system_instruction=system_instruction
            )
            
            prompt = "Conduct a rigorous multi-layered analysis of this media to determine its authenticity and respond with JSON matching the required schema."

            cache_name = self.create_cache(part)
            
            if cache_name:
                result_data = self.generate_with_cache(cache_name, prompt, config=config)
                parsed = json.loads(self._extract_json(result_data['text']))
                parsed['cache_name'] = cache_name
                return parsed
            
            if self.client:
                # Use modern Client SDK
                response = self.client.models.generate_content(
                    model=self.MODEL_PRO,
                    contents=[part, prompt],
                    config=config
                )
                text = response.text
                
                # Cleanup if we uploaded a file
                if uploaded_file:
                    try:
                        self.client.files.delete(name=uploaded_file.name)
                        logger.info(f"Deleted Gemini file: {uploaded_file.name}")
                    except Exception as cleanup_err:
                        logger.warning(f"Failed to delete Gemini file: {cleanup_err}")
            else:
                raise Exception("Gemini client not initialized")

            return json.loads(self._extract_json(text))

        except Exception as e:
            logger.error(f"Media analysis failed: {e}")
            if uploaded_file and self.client:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass
            raise e
