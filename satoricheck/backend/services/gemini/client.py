import concurrent.futures
import functools
import ipaddress
import logging
import os
import re
import socket
import time
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
    MODEL = Config.GEMINI_MODEL
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
        self,
        model: str,
        contents,
        config: types.GenerateContentConfig,
        context_label: str = "",
        fallback_system_instruction: str | None = None,
        fallback_tools: list | None = None,
        fallback_tool_config: types.ToolConfig | None = None,
    ):
        """Call generate_content, automatically clearing cached_content on failure and retrying.

        In the Gemini API, a GenerateContentConfig referencing cached_content cannot
        also specify system_instruction, tools, or tool_config (those are baked into
        the CachedContent resource at creation time). If the cache expired or failed,
        this helper clears cached_content and restores the fallback system_instruction,
        tools, and tool_config before retrying.

        Args:
            model:                       Model identifier (e.g. self.MODEL).
            contents:                    Prompt or conversation history.
            config:                      GenerateContentConfig (may include cached_content).
            context_label:               Descriptive label for log messages.
            fallback_system_instruction: Restored onto config if cache is cleared.
            fallback_tools:              Restored onto config if cache is cleared.
            fallback_tool_config:        Restored onto config if cache is cleared.

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
                if fallback_system_instruction is not None:
                    config.system_instruction = fallback_system_instruction
                if fallback_tools is not None:
                    config.tools = fallback_tools
                if fallback_tool_config is not None:
                    config.tool_config = fallback_tool_config
                response = self.client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return response, True
            raise

    @staticmethod
    def _execute_tool_calls(function_calls, grok) -> list[types.Content]:
        """Execute client-side tool calls emitted by Gemini and return tool role Content entries.

        Centralises tool dispatch across single-claim (`claims.py`) and batch (`batch.py`)
        agentic loops, including graceful fallback instructions if Grok social search is
        unavailable or times out.
        """
        tool_contents = []
        for fc in function_calls:
            fn_name = fc.name
            fn_args = fc.args or {}

            if fn_name == 'search_social':
                query = fn_args.get('query', '')
                logger.info(f"🔍 Executing Tool: search_social(query='{query[:50]}')")
                result = grok.search_social(query)
                if result.get('error'):
                    logger.info(f"Grok failed ({result['error']}), injecting fallback hint.")
                    result['found'] = False
                    result['instruction'] = (
                        "The social search tool is unavailable. "
                        "Do NOT call search_social again. "
                        "Proceed to produce the final JSON verdict using your own knowledge."
                    )
            else:
                result = {"error": f"Unknown tool: {fn_name}"}

            function_response_part = types.Part.from_function_response(
                name=fn_name,
                response=result
            )
            tool_contents.append(
                types.Content(role="tool", parts=[function_response_part])
            )
        return tool_contents

    @staticmethod
    def _attach_social_watermark(parsed, conversation):
        """Propagate EU AI Act watermark provenance from Grok tool calls onto parsed results.

        Why: Transport-layer headers (`x-watermark: true`) are deterministic metadata from xAI.
        Attaching `social_watermarked` directly from tool outputs in Python avoids asking the
        LLM to copy a boolean into its JSON output (saving tokens and preventing hallucination).
        """
        if parsed is None or not conversation:
            return parsed

        watermark_flags = []
        for msg in conversation:
            if getattr(msg, "role", None) != "tool":
                continue
            for part in getattr(msg, "parts", None) or []:
                fn_resp = getattr(part, "function_response", None)
                if fn_resp and getattr(fn_resp, "name", None) == "search_social":
                    resp_data = getattr(fn_resp, "response", None)
                    if isinstance(resp_data, dict) and "watermarked" in resp_data:
                        watermark_flags.append(bool(resp_data["watermarked"]))

        if not watermark_flags:
            return parsed

        social_watermarked = any(watermark_flags)
        if isinstance(parsed, dict):
            parsed["social_watermarked"] = social_watermarked
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    item["social_watermarked"] = social_watermarked
        return parsed

    def _run_verification_loop(
        self,
        prompt: str,
        system_instruction: str,
        needs_social: bool,
        grok,
        response_parser,
        cache_name: str | None = None,
        max_turns: int = 3,
        retries_per_turn: int = 1,
        raise_on_turn_failure: bool = False,
        enable_closing_turn: bool = False,
        context_label_prefix: str = "agentic",
        thinking_level: str = "high",
    ):
        """Master agentic verification loop shared across single-claim, batch, pitchdeck, and media flows.

        Why: Consolidating the multi-turn high-thinking loop into a single method ensures
        consistent Google Search grounding, conditional xAI Grok tool attachment, context-cache
        fallback recovery, and tool-response synthesis across every verification module.
        """
        google_search_tool = types.Tool(google_search=types.GoogleSearch())
        if needs_social:
            tools_list = [google_search_tool, self._build_grok_tool(grok)]
        else:
            tools_list = [google_search_tool]
        tool_cfg = types.ToolConfig(
            include_server_side_tool_invocations=True
        )

        conversation = [
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        ]

        # In the Gemini API, a GenerateContentConfig referencing cached_content
        # cannot also set system_instruction, tools, or tool_config.
        if cache_name:
            config = types.GenerateContentConfig(
                cached_content=cache_name,
                thinking_config=types.ThinkingConfig(
                    thinking_level=thinking_level,
                    include_thoughts=True,
                ),
            )
        else:
            config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                tools=tools_list,
                tool_config=tool_cfg,
                thinking_config=types.ThinkingConfig(
                    thinking_level=thinking_level,
                    include_thoughts=True,
                ),
            )

        for turn in range(1, max_turns + 1):
            turn_response = None
            turn_exception = None

            for attempt in range(retries_per_turn):
                try:
                    logger.info(f"Agentic turn {turn}/{max_turns}...")
                    response, cache_cleared = self._generate_with_cache_fallback(
                        model=self.MODEL,
                        contents=conversation,
                        config=config,
                        context_label=f"{context_label_prefix} turn {turn}",
                        fallback_system_instruction=system_instruction,
                        fallback_tools=tools_list,
                        fallback_tool_config=tool_cfg,
                    )
                    if cache_cleared:
                        cache_name = None
                    turn_response = response
                    break
                except Exception as e:
                    turn_exception = e
                    if retries_per_turn > 1:
                        logger.warning(f"Agentic turn {turn} failed (attempt {attempt + 1}): {e}")
                        time.sleep(1)

            if not turn_response:
                if raise_on_turn_failure:
                    raise turn_exception or Exception("Agentic turn failed after retries")
                logger.error(f"Agentic turn {turn} failed: {turn_exception}")
                if turn == max_turns:
                    break
                time.sleep(1)
                continue

            try:
                if not turn_response.candidates:
                    raise ValueError("No candidates in response")
                candidate = turn_response.candidates[0]
                content = candidate.content
                if not content or not content.parts:
                    raise ValueError("No content or parts in candidate")

                if turn_response.function_calls:
                    conversation.append(content)
                    logger.info(f"Executing {len(turn_response.function_calls)} tool calls...")
                    conversation.extend(
                        self._execute_tool_calls(turn_response.function_calls, grok)
                    )
                    continue

                final_text = self._extract_text_from_parts(content.parts)
                parsed = response_parser(final_text, turn_response)
                if parsed is not None:
                    return self._attach_social_watermark(parsed, conversation)
                conversation.append(content)
            except Exception as e:
                if raise_on_turn_failure:
                    raise
                logger.error(f"Agentic turn {turn} response processing failed: {e}")
                if turn == max_turns:
                    break
                time.sleep(1)

        if enable_closing_turn and conversation and conversation[-1].role == "tool":
            try:
                logger.info("Agentic closing turn (producing final answer from tool results)...")
                closing_tool_cfg = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.NONE
                    )
                )
                if cache_name:
                    closing_config = types.GenerateContentConfig(
                        cached_content=cache_name,
                        response_mime_type="application/json",
                        thinking_config=types.ThinkingConfig(
                            thinking_level=thinking_level,
                            include_thoughts=True,
                        ),
                    )
                else:
                    closing_config = types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        thinking_config=types.ThinkingConfig(
                            thinking_level=thinking_level,
                            include_thoughts=True,
                        ),
                        tool_config=closing_tool_cfg,
                    )

                response, _ = self._generate_with_cache_fallback(
                    model=self.MODEL,
                    contents=conversation,
                    config=closing_config,
                    context_label=f"{context_label_prefix} closing turn",
                    fallback_system_instruction=system_instruction,
                    fallback_tool_config=closing_tool_cfg,
                )
                if response.candidates:
                    closing_text = self._extract_text_from_parts(response.candidates[0].content.parts)
                    parsed = response_parser(closing_text, response)
                    if parsed is not None:
                        return self._attach_social_watermark(parsed, conversation)
            except Exception as e:
                logger.error(f"Agentic closing turn failed: {e}")

        return None

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
        """Filter sources to only include live public URLs concurrently. Enforces max 5 sources."""
        if not sources or not isinstance(sources, list):
            return []

        # Deduplicate while preserving order, inspect at most 10 candidates
        candidates = []
        seen = set()
        for url in sources:
            if isinstance(url, str):
                clean = url.strip()
                if clean and clean not in seen:
                    seen.add(clean)
                    candidates.append(clean)
                    if len(candidates) >= 10:
                        break

        if not candidates:
            return []

        if len(candidates) == 1:
            return candidates if self._validate_url(candidates[0]) else []

        # Validate URLs in parallel to avoid blocking Gunicorn worker threads sequentially
        max_workers = min(5, len(candidates))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            checks = list(executor.map(self._validate_url, candidates))

        valid_sources = []
        for url, is_valid in zip(candidates, checks):
            if is_valid:
                valid_sources.append(url)
                if len(valid_sources) >= 5:
                    break
        return valid_sources

    @staticmethod
    def _extract_grounding_urls(candidate) -> list[str]:
        """Extract verified web source URLs from Gemini's server-side Google Search grounding metadata."""
        if not candidate:
            return []

        grounding_meta = (
            candidate.get('groundingMetadata') or candidate.get('grounding_metadata')
            if isinstance(candidate, dict)
            else getattr(candidate, 'grounding_metadata', None)
        )
        if not grounding_meta:
            return []

        chunks = (
            grounding_meta.get('groundingChunks') or grounding_meta.get('grounding_chunks')
            if isinstance(grounding_meta, dict)
            else getattr(grounding_meta, 'grounding_chunks', None)
        )
        if not chunks or not isinstance(chunks, (list, tuple)):
            return []

        urls = []
        seen = set()
        for chunk in chunks:
            web = chunk.get('web') if isinstance(chunk, dict) else getattr(chunk, 'web', None)
            if not web:
                continue
            uri = web.get('uri') if isinstance(web, dict) else getattr(web, 'uri', None)
            if isinstance(uri, str) and uri.startswith(('http://', 'https://')) and uri not in seen:
                seen.add(uri)
                urls.append(uri)
        return urls
