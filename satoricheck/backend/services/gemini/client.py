import functools
import ipaddress
import logging
import os
import re
import socket
import requests
from enum import Enum
from urllib.parse import urlparse
from google import genai
from google.genai import types
from backend.config import Config

logger = logging.getLogger(__name__)

class ClaimPriority(str, Enum):
    """Priority levels for stream claim processing."""
    IMMEDIATE = "immediate"
    NORMAL = "normal"
    DEFERRED = "deferred"
    SKIP = "skip"


_BLOCKED_METADATA_IPS = frozenset({
    '169.254.169.254',  # AWS / GCP instance metadata
    'fd00:ec2::254',    # AWS IMDSv2 IPv6
})


@functools.lru_cache(maxsize=16)
def _read_skill_file(skill_name: str) -> str | None:
    """Read a skill markdown file from disk (cached).

    Returns file contents on success, None on failure.
    Cached at module level so repeated calls skip disk I/O.
    """
    skill_path = os.path.join(Config.SKILLS_DIR, f"{skill_name}.md")
    try:
        with open(skill_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"Skill file not found: {skill_path}")
        return None
    except Exception as e:
        logger.error(f"Error loading skill {skill_name}: {e}")
        return None


class GeminiServiceClient:
    """Base client for Gemini Service utilizing the modern google-genai SDK."""
    MODEL_PRO = Config.GEMINI_MODEL_PRO
    MODEL_FAST = Config.GEMINI_MODEL_FLASH
    MODEL_EMBEDDING = "gemini-embedding-2-preview"
    
    TIMEOUT_PRO = 30
    TIMEOUT_FAST = 30
    TIMEOUT_TRIAGE = 10
    TIMEOUT_SLOW = 60
    
    MAX_CLAIMS_PER_PROMPT = 8
    _MAX_REDIRECT_DEPTH = 5

    def __init__(self):
        self.api_key = Config.GEMINI_API_KEY
        if not self.api_key:
            logger.error("GEMINI_API_KEY is not set for GeminiService!")
            self.client = None
        else:
            logger.info("✓ Gemini service initialized")
            try:
                # Initialize the modern google-genai client
                self.client = genai.Client(api_key=self.api_key)
                logger.info("✓ Gemini SDK Client (google-genai) configured")
            except Exception as e:
                logger.error(f"Failed to configure Gemini SDK Client: {e}")
                self.client = None

    def _get_api_url(self, model: str, action: str = "generateContent") -> str:
        """Get legacy API URL for REST fallback calls."""
        return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{action}"

    def _get_headers(self) -> dict:
        """Get standard legacy headers for REST fallback calls."""
        return {
            'Content-Type': 'application/json',
            'x-goog-api-key': self.api_key
        }

    def _load_skill(self, skill_name: str, fallback: str = "") -> str:
        """Load an agent skill markdown file as a system instruction string.

        Results are cached in memory (LRU, max 16 skills) to avoid redundant
        disk reads on every API call.  Call ``_read_skill_file.cache_clear()``
        if you need to hot-reload skills at runtime.

        Args:
            skill_name: Basename of the skill file (without .md extension).
            fallback:   String returned when the skill file is missing or unreadable.

        Returns:
            Skill file contents, or *fallback* on any failure.
        """
        # Defensive: reject path separators to prevent directory traversal.
        if re.search(r'[/\\]', skill_name) or '..' in skill_name:
            logger.error(f"Invalid skill name (path traversal attempt): {skill_name}")
            return fallback

        content = _read_skill_file(skill_name)
        return content if content is not None else fallback

    @staticmethod
    def _build_grok_tool(grok) -> types.Tool:
        """Build a Gemini-compatible Tool declaration from the Grok service definition.

        Centralises the dict→types.Tool conversion so callers don't duplicate
        the 18-line construction block.
        """
        tool_dict = grok.get_tool_definition()
        return types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name=tool_dict.get("name"),
                    description=tool_dict.get("description"),
                    parameters=types.Schema(
                        type="OBJECT",
                        properties={
                            k: types.Schema(
                                type=v.get("type", "STRING").upper(),
                                description=v.get("description", "")
                            )
                            for k, v in tool_dict.get("parameters", {}).get("properties", {}).items()
                        },
                        required=tool_dict.get("parameters", {}).get("required", [])
                    )
                )
            ]
        )

    def _generate_with_cache_fallback(
        self, model: str, contents, config: types.GenerateContentConfig, context_label: str = ""
    ):
        """Call generate_content, automatically clearing cached_content on failure and retrying.

        This centralises the try→catch→clear-cache→retry pattern used across
        agentic loops in claims.py and batch.py.

        Args:
            model:         Model identifier (e.g. self.MODEL_PRO).
            contents:      Prompt or conversation history.
            config:        GenerateContentConfig (may include cached_content).
            context_label: Descriptive label for log messages.

        Returns:
            Tuple of (response, cache_was_cleared: bool).

        Raises:
            Exception: If the call fails even without cache.
        """
        if not self.client:
            raise Exception("Gemini client not initialized")

        try:
            response = self.client.models.generate_content(
                model=model, contents=contents, config=config
            )
            return response, False
        except Exception as e:
            if config.cached_content:
                logger.warning(
                    f"Cache {config.cached_content} failed/conflicted"
                    f"{' in ' + context_label if context_label else ''}, "
                    f"clearing cache and retrying: {e}"
                )
                config.cached_content = None
                response = self.client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return response, True
            raise

    @staticmethod
    def _is_private_ip(ip_str: str) -> bool:
        """Return True if the IP is private, loopback, link-local, or cloud metadata."""
        if ip_str in _BLOCKED_METADATA_IPS:
            return True
        try:
            addr = ipaddress.ip_address(ip_str)
            return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
        except ValueError:
            return True

    def _validate_url(self, url: str, _redirect_depth: int = 0) -> bool:
        """Check if a URL is reachable and points to a public IP to prevent SSRF."""
        if _redirect_depth >= self._MAX_REDIRECT_DEPTH:
            logger.warning(f"SSRF blocked: Exceeded {self._MAX_REDIRECT_DEPTH} redirects for {url}")
            return False

        if not url or not isinstance(url, str):
            return False
        if not url.startswith('http://') and not url.startswith('https://'):
            return False

        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            if not hostname:
                return False

            addr_infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
            for family, _type, _proto, _canonname, sockaddr in addr_infos:
                ip = sockaddr[0]
                if self._is_private_ip(ip):
                    logger.warning(f"SSRF blocked: {url} resolved to private IP {ip}")
                    return False
        except (socket.gaierror, OSError):
            return False

        try:
            response = requests.head(url, timeout=3, allow_redirects=False, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; Authenix/1.0)'
            })
            if 300 <= response.status_code < 400:
                from urllib.parse import urljoin
                redirect_url = response.headers.get('Location')
                if redirect_url and isinstance(redirect_url, str):
                    full_redirect_url = urljoin(url, redirect_url)
                    logger.info(f"SSRF Check: Following redirect to {full_redirect_url}")
                    return self._validate_url(full_redirect_url, _redirect_depth + 1)
            return response.status_code < 400
        except Exception:
            return False

    def _validate_sources(self, sources):
        """Filter sources to only include live public URLs. Enforces max 5 sources."""
        if not sources or not isinstance(sources, list):
            return []
        valid_sources = []
        for url in sources[:10]:
            if self._validate_url(url):
                valid_sources.append(url)
                if len(valid_sources) >= 5:
                    break
        return valid_sources
