import io
import json
import logging
import os
import re
import time
from urllib.parse import urlparse, parse_qs
from google.genai import types
from backend.config import Config
from backend.services import safe_http
from backend.services.gemini.claims import GeminiServiceClaims

logger = logging.getLogger(__name__)


class GeminiServiceMedia(GeminiServiceClaims):
    """Multimodal media verification and video processing methods for GeminiService."""

    # Matches the 50 MB direct-upload cap (MAX_CONTENT_LENGTH) so URL and file inputs
    # share one memory budget per request.
    MAX_REMOTE_MEDIA_BYTES = 50 * 1024 * 1024
    # 14 MB raw binary cap keeps base64-encoded inline JSON payloads under Gemini's 20 MB limit.
    MAX_INLINE_MEDIA_BYTES = 14 * 1024 * 1024
    MAX_SPOKEN_CLAIMS = 20
    REMOTE_MEDIA_TOTAL_TIMEOUT_S = 60
    VIDEO_HIGH_FPS_MAX_BYTES = 5 * 1024 * 1024
    VIDEO_MAX_FPS = 4.0
    VIDEO_STANDARD_FPS = 2.0

    @classmethod
    def video_sampling_fps(cls, file_size_bytes: int | None = None) -> float:
        """Use 4.0 FPS for small video clips (<= 5 MB) and 2.0 FPS for larger clips or YouTube URIs."""
        if file_size_bytes is not None and 0 < file_size_bytes <= cls.VIDEO_HIGH_FPS_MAX_BYTES:
            return cls.VIDEO_MAX_FPS
        return cls.VIDEO_STANDARD_FPS

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

    @staticmethod
    def extract_youtube_start_seconds(url: str) -> int:
        """Extract start timestamp in seconds from YouTube ?t= or ?start= query parameters."""
        if not url or not isinstance(url, str):
            return 0
        parsed = urlparse(url.strip())
        qs = parse_qs(parsed.query)
        raw = (qs.get('t') or qs.get('start') or [None])[0]
        if not raw and parsed.fragment:
            frag_qs = parse_qs(parsed.fragment)
            raw = (frag_qs.get('t') or frag_qs.get('start') or [None])[0]
        if not raw:
            return 0

        match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?', raw.strip(), re.IGNORECASE)
        if not match or not any(match.groups()):
            return 0

        hours, minutes, seconds = match.groups()
        total = (int(hours or 0) * 3600) + (int(minutes or 0) * 60) + int(seconds or 0)
        return max(0, total)

    def validate_youtube_video(self, canonical_url: str) -> bool:
        """Keyless YouTube oEmbed pre-flight check to reject private/deleted videos before billing."""
        oembed_url = f'https://www.youtube.com/oembed?url={canonical_url}&format=json'
        resp = safe_http.safe_request('GET', oembed_url, timeout=5)
        try:
            if resp.status_code in (400, 401, 403, 404):
                raise ValueError('YouTube video is unavailable, private, or does not exist.')
        finally:
            resp.close()
        return True

    @classmethod
    def _attach_video_sampling(
        cls,
        part,
        mime_type: str,
        fps: float | None = None,
        start_seconds: int = 0,
    ):
        """Attach VideoMetadata(fps, start_offset, end_offset) to a video Part or uploaded File reference.

        Why: File-size tiered sampling (4.0 FPS for <= 5 MB clips, 2.0 FPS for larger clips or
        YouTube URIs) preserves micro-kinematic detail on small clips while bounding Gemini cost,
        and start/end offsets window ingestion to 30s (so uploads longer than 30s are analyzed on
        their first window instead of rejected).
        """
        if 'video' not in (mime_type or '').lower():
            return part

        max_video_seconds = int(Config.MAX_VIDEO_DURATION_SECONDS)
        start_sec = max(0, int(start_seconds or 0))
        end_sec = start_sec + max_video_seconds
        video_meta = types.VideoMetadata(
            fps=fps or cls.VIDEO_MAX_FPS,
            start_offset=f'{start_sec}s',
            end_offset=f'{end_sec}s',
        )

        if isinstance(part, types.Part):
            part.video_metadata = video_meta
            return part

        file_uri = getattr(part, 'uri', None)
        file_mime = getattr(part, 'mime_type', None) or mime_type
        if isinstance(file_uri, str) and file_uri.startswith(('http://', 'https://')):
            return types.Part(
                file_data=types.FileData(file_uri=file_uri, mime_type=file_mime),
                video_metadata=video_meta,
            )

        return part

    def _prepare_media_part(self, media_input: str | bytes, mime_type: str, input_type: str = 'url') -> types.Part:
        """Helper to prepare a multimodal part for the Gemini SDK."""
        if input_type == 'url':
            url_str = str(media_input)

            # YouTube URLs are passed natively to Gemini via Part.from_uri without downloading
            yt_id = self.extract_youtube_id(url_str)
            if yt_id:
                if not self._validate_url(url_str):
                    raise ValueError("Invalid or restricted URL")
                canonical_yt = f"https://www.youtube.com/watch?v={yt_id}"
                return types.Part.from_uri(file_uri=canonical_yt, mime_type="video/mp4")

            # download_bounded validates every redirect hop via safe_request('GET', ...),
            # avoiding a redundant separate HEAD round-trip before the GET.
            try:
                data = safe_http.download_bounded(
                    url_str,
                    max_bytes=self.MAX_REMOTE_MEDIA_BYTES,
                    timeout=30,
                    total_timeout=self.REMOTE_MEDIA_TOTAL_TIMEOUT_S,
                )
            except safe_http.UnsafeURLError as exc:
                raise ValueError("Invalid or restricted URL") from exc
            return types.Part.from_bytes(data=data, mime_type=mime_type)

        if input_type == 'file':
            with open(str(media_input), 'rb') as f:
                data = f.read()
            return types.Part.from_bytes(data=data, mime_type=mime_type)

        # Assume bytes
        return types.Part.from_bytes(data=media_input, mime_type=mime_type)

    @staticmethod
    def _build_media_response_schema() -> types.Schema:
        """Build the typed Gemini response schema for forensic media analysis."""
        criterion_schema = types.Schema(
            type=types.Type.OBJECT,
            properties={
                "tag": types.Schema(
                    type=types.Type.STRING,
                    enum=["High Signal", "Med Signal", "Clean", "N/A"],
                ),
                "score": types.Schema(type=types.Type.INTEGER),
                "detail": types.Schema(type=types.Type.STRING),
            },
            required=["tag", "score", "detail"],
        )
        criterion_keys = (
            "physics",
            "bio",
            "temporal",
            "audio",
            "context",
            "compression",
            "metadata",
        )
        claim_schema = types.Schema(
            type=types.Type.OBJECT,
            properties={
                "timestamp": types.Schema(type=types.Type.STRING),
                "timestamp_seconds": types.Schema(type=types.Type.INTEGER),
                "claim": types.Schema(type=types.Type.STRING),
            },
            required=["timestamp", "timestamp_seconds", "claim"],
        )
        return types.Schema(
            type=types.Type.OBJECT,
            properties={
                "verdict": types.Schema(
                    type=types.Type.STRING,
                    enum=["AI Generated", "Likely Manipulated", "Appears Authentic"],
                ),
                "confidence": types.Schema(type=types.Type.INTEGER),
                "explanation": types.Schema(type=types.Type.STRING),
                "criteria": types.Schema(
                    type=types.Type.OBJECT,
                    properties={k: criterion_schema for k in criterion_keys},
                    required=list(criterion_keys),
                ),
                "claims": types.Schema(
                    type=types.Type.ARRAY,
                    items=claim_schema,
                ),
            },
            required=["verdict", "confidence", "explanation", "criteria", "claims"],
        )

    @staticmethod
    def _is_transient_gemini_error(exc: Exception) -> bool:
        """Return True for transient 429/503 upstream capacity errors worth one retry."""
        code = getattr(exc, 'code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
        if code in (429, 503):
            return True
        msg = str(exc)
        return any(tok in msg for tok in ('429', '503', 'RESOURCE_EXHAUSTED', 'UNAVAILABLE'))

    def _upload_and_poll_file(self, file_source, mime_type: str):
        """Upload a local path or BytesIO stream to the Gemini Files API and wait until ACTIVE."""
        uploaded_file = self.client.files.upload(
            file=file_source,
            config=types.UploadFileConfig(mime_type=mime_type),
        )
        try:
            start_time = time.time()
            while True:
                state = getattr(uploaded_file, 'state', None)
                state_name = getattr(state, 'name', str(state)).upper()

                if state_name != "PROCESSING":
                    break

                if time.time() - start_time > 120:
                    raise TimeoutError("Gemini file processing timed out")
                time.sleep(2)
                uploaded_file = self.client.files.get(name=uploaded_file.name)

            state = getattr(uploaded_file, 'state', None)
            state_name = getattr(state, 'name', str(state)).upper()
            if state_name != "ACTIVE":
                err_msg = getattr(getattr(uploaded_file, 'error', None), 'message', state_name)
                raise ValueError(f"Gemini file processing failed: {err_msg}")
            return uploaded_file
        except Exception:
            if uploaded_file:
                try:
                    self.client.files.delete(name=uploaded_file.name)
                except Exception:
                    pass
            raise

    def analyze_media_authenticity(
        self,
        media_input: str | bytes,
        input_type: str = 'url',
        mime_type: str = 'video/mp4',
        start_seconds: int = 0,
    ) -> dict:
        """Analyze media for authenticity (AI generation, manipulation, deepfakes).

        Routes visual media (images/video) to MODEL_MEDIA (Argon) and audio-only tracks to MODEL (Flash).
        Payloads <= 14 MB go straight inline (1 API call); only > 14 MB payloads use Gemini Files API.
        """
        uploaded_file = None
        part = None
        payload_size: int | None = None
        try:
            if input_type == 'file' and self.client:
                file_path = str(media_input)
                file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
                payload_size = file_size

                if file_size <= self.MAX_INLINE_MEDIA_BYTES:
                    logger.info(f"[Gemini] Sending file inline ({file_size} bytes): {file_path}")
                    with open(file_path, 'rb') as f:
                        file_data = f.read()
                    part = types.Part.from_bytes(data=file_data, mime_type=mime_type)
                else:
                    logger.info(f"[Gemini] Uploading large file via Files API ({file_size} bytes): {file_path}")
                    uploaded_file = self._upload_and_poll_file(file_path, mime_type)
                    part = uploaded_file
            elif (
                input_type == 'bytes'
                and self.client
                and isinstance(media_input, (bytes, bytearray))
                and len(media_input) > self.MAX_INLINE_MEDIA_BYTES
            ):
                payload_size = len(media_input)
                logger.info(f"[Gemini] Uploading large byte payload ({payload_size} bytes) via Files API")
                uploaded_file = self._upload_and_poll_file(io.BytesIO(media_input), mime_type)
                part = uploaded_file
            else:
                if input_type == 'bytes' and isinstance(media_input, (bytes, bytearray)):
                    payload_size = len(media_input)
                part = self._prepare_media_part(media_input, mime_type, input_type)
                inline_data = getattr(part, 'inline_data', None)
                raw_inline = getattr(inline_data, 'data', None)
                if isinstance(raw_inline, (bytes, bytearray)):
                    payload_size = len(raw_inline)
                if (
                    self.client
                    and isinstance(raw_inline, (bytes, bytearray))
                    and len(raw_inline) > self.MAX_INLINE_MEDIA_BYTES
                ):
                    logger.info(f"[Gemini] Uploading large remote payload ({len(raw_inline)} bytes) via Files API")
                    uploaded_file = self._upload_and_poll_file(io.BytesIO(raw_inline), mime_type)
                    part = uploaded_file

            is_video = 'video' in mime_type
            is_audio = 'audio' in mime_type and not is_video
            is_youtube = input_type == 'url' and bool(self.extract_youtube_id(str(media_input)))

            if is_youtube and not start_seconds:
                start_seconds = self.extract_youtube_start_seconds(str(media_input))

            if is_video:
                part = self._attach_video_sampling(
                    part,
                    mime_type,
                    fps=self.video_sampling_fps(payload_size),
                    start_seconds=start_seconds,
                )

            system_instruction = self._load_skill(
                "media_forensics",
                fallback="Act as a Senior Forensic Media Analyst specializing in deepfake and synthetic content detection."
            )

            config_kwargs = {
                "system_instruction": system_instruction,
                "response_mime_type": "application/json",
                "response_schema": self._build_media_response_schema(),
                "thinking_config": types.ThinkingConfig(
                    thinking_level="high"
                ),
            }
            if not is_audio:
                config_kwargs["media_resolution"] = types.MediaResolution.MEDIA_RESOLUTION_HIGH
            config = types.GenerateContentConfig(**config_kwargs)

            if is_video:
                prompt = (
                    f"Conduct a rigorous multi-layered forensic analysis of this video with audio track ({mime_type}). "
                    "Evaluate structural and mechanical topology, optical depth planes and illumination, contact/weight physics, "
                    "biometric micro-consistency, temporal detail/text convergence across motion, audio/acoustic stationarity, "
                    "and sensor/compression characteristics. "
                    f"If spoken dialogue or narration is present, extract up to {self.MAX_SPOKEN_CLAIMS} distinct falsifiable "
                    "claims with relative timestamps into the 'claims' array. "
                    "Respond with JSON matching the required schema."
                )
            elif is_audio:
                prompt = (
                    f"Conduct a rigorous forensic analysis of this audio track ({mime_type}). "
                    "Evaluate vocal timbre, acoustic stationarity, room coupling, scene/behavioral context, compression, and metadata. "
                    "Set 'physics', 'bio', and 'temporal' criteria to "
                    "{\"tag\": \"N/A\", \"score\": 0, \"detail\": \"Not applicable to audio-only media.\"}. "
                    f"If spoken dialogue or narration is present, extract up to {self.MAX_SPOKEN_CLAIMS} distinct falsifiable "
                    "claims with relative timestamps into the 'claims' array. "
                    "Respond with JSON matching the required schema."
                )
            else:
                prompt = (
                    f"Conduct a rigorous multi-layered forensic analysis of this static image ({mime_type}). "
                    "Evaluate structural and mechanical topology, optical depth planes and illumination, contact/weight physics, "
                    "biometric micro-consistency, scene/contextual coherence, and sensor/compression characteristics. "
                    "Set 'temporal' and 'audio' criteria to "
                    "{\"tag\": \"N/A\", \"score\": 0, \"detail\": \"Not applicable to static images.\"} "
                    "and set 'claims' to []. "
                    "Respond with JSON matching the required schema."
                )

            if not self.client:
                raise Exception("Gemini client not initialized")

            target_model = self.MODEL if is_audio else self.MODEL_MEDIA
            response = None
            for attempt in range(2):
                try:
                    response = self.client.models.generate_content(
                        model=target_model,
                        contents=[part, prompt],
                        config=config
                    )
                    break
                except Exception as exc:
                    if attempt == 0 and self._is_transient_gemini_error(exc):
                        logger.warning(f"Transient Gemini media error (retrying once): {exc}")
                        time.sleep(1)
                        continue
                    raise

            text = response.text
            if not text or not text.strip():
                raise ValueError("Media analysis returned an empty response")
            parsed = json.loads(self._extract_json(text))
            required_keys = ('verdict', 'confidence', 'explanation', 'criteria')
            if not isinstance(parsed, dict) or any(parsed.get(k) is None for k in required_keys):
                raise ValueError("Media analysis returned an incomplete response")
            raw_claims = parsed.get('claims')
            parsed['claims'] = raw_claims[:self.MAX_SPOKEN_CLAIMS] if isinstance(raw_claims, list) else []
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
