"""
Opt-in live smoke test for the configured Gemini model.

Why: unit tests mock the Gemini client, so they cannot detect when a model rejects a
thinking level (e.g. gemini-3.8-flash returns 400 for "minimal"). Run this before
changing GEMINI_MODEL in production:

    GEMINI_SMOKE_API_KEY=<real key> .venv/bin/pytest tests/test_gemini_model_smoke.py

A dedicated env var is used because conftest.py overwrites GEMINI_API_KEY with a fake.
"""
import base64
import json
import os

import pytest
from google import genai
from google.genai import types

from backend.config import Config
from backend.services.gemini.media import GeminiServiceMedia

SMOKE_API_KEY = os.getenv('GEMINI_SMOKE_API_KEY')

# Minimal valid 1x1 transparent PNG for multimodal Argon schema smoke verification.
_TINY_PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not SMOKE_API_KEY,
        reason="Set GEMINI_SMOKE_API_KEY to run live Gemini model smoke tests",
    ),
]


@pytest.mark.parametrize("model_attr", ["GEMINI_MODEL", "GEMINI_MEDIA_MODEL"])
@pytest.mark.parametrize("thinking_level", ["low", "high"])
def test_configured_model_accepts_thinking_level_used_in_code(model_attr, thinking_level):
    """Both GEMINI_MODEL and GEMINI_MEDIA_MODEL must accept every thinking level the codebase uses."""
    client = genai.Client(api_key=SMOKE_API_KEY)
    model_id = getattr(Config, model_attr)

    response = client.models.generate_content(
        model=model_id,
        contents="Reply with OK",
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level)
        ),
    )

    assert response.text


def test_flash_explicit_context_cache_with_google_search_and_low_thinking():
    """GEMINI_MODEL must support explicit CachedContent with GoogleSearch + ToolConfig and low thinking."""
    client = genai.Client(api_key=SMOKE_API_KEY)
    # Build a >= 4,096-token static deck summary payload to clear Gemini 3.8 Flash's cache floor.
    filler_lines = [
        f"Slide {i}: Quarterly SaaS benchmark note {i} covering enterprise pipeline conversion, "
        f"gross margin stability, net dollar retention, and regional compliance milestones."
        for i in range(1, 260)
    ]
    cached_payload = (
        "<pitchdeck_data_boundary>\n"
        "Company: SmokeTestCo\n"
        + "\n".join(filler_lines)
        + "\n</pitchdeck_data_boundary>"
    )

    cache = client.caches.create(
        model=Config.GEMINI_MODEL,
        config=types.CreateCachedContentConfig(
            contents=[cached_payload],
            system_instruction="You are a concise verification assistant. Reply with JSON.",
            tools=[types.Tool(google_search=types.GoogleSearch())],
            tool_config=types.ToolConfig(include_server_side_tool_invocations=True),
            ttl="120s",
        ),
    )
    try:
        assert cache.name
        response = client.models.generate_content(
            model=Config.GEMINI_MODEL,
            contents='What company name is listed inside the cached deck? Reply with {"company": "..."}',
            config=types.GenerateContentConfig(
                cached_content=cache.name,
                thinking_config=types.ThinkingConfig(thinking_level="low"),
            ),
        )
        assert response.text
        assert "SmokeTestCo" in response.text
    finally:
        client.caches.delete(name=cache.name)


def test_argon_media_model_accepts_high_resolution_and_forensic_schema():
    """GEMINI_MEDIA_MODEL must accept MEDIA_RESOLUTION_HIGH + thinking_level='high' + forensic response_schema."""
    client = genai.Client(api_key=SMOKE_API_KEY)
    img_part = types.Part.from_bytes(data=_TINY_PNG_BYTES, mime_type="image/png")

    response = client.models.generate_content(
        model=Config.GEMINI_MEDIA_MODEL,
        contents=[
            img_part,
            "Evaluate this 1x1 test image and return JSON matching the required forensic schema with claims=[].",
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GeminiServiceMedia._build_media_response_schema(),
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
            thinking_config=types.ThinkingConfig(thinking_level="high"),
        ),
    )

    assert response.text
    parsed = json.loads(response.text)
    assert parsed["verdict"] in ("AI Generated", "Likely Manipulated", "Appears Authentic")
    assert isinstance(parsed["criteria"], dict)
    assert isinstance(parsed["claims"], list)

