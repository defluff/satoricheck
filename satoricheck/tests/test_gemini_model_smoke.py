"""
Opt-in live smoke test for the configured Gemini model.

Why: unit tests mock the Gemini client, so they cannot detect when a model rejects a
thinking level (e.g. gemini-3.8-flash returns 400 for "minimal"). Run this before
changing GEMINI_MODEL in production:

    GEMINI_SMOKE_API_KEY=<real key> .venv/bin/pytest tests/test_gemini_model_smoke.py

A dedicated env var is used because conftest.py overwrites GEMINI_API_KEY with a fake.
"""
import os

import pytest
from google import genai
from google.genai import types

from backend.config import Config

SMOKE_API_KEY = os.getenv('GEMINI_SMOKE_API_KEY')

pytestmark = pytest.mark.skipif(
    not SMOKE_API_KEY,
    reason="Set GEMINI_SMOKE_API_KEY to run live Gemini model smoke tests",
)


@pytest.mark.parametrize("thinking_level", ["low", "high"])
def test_configured_model_accepts_thinking_level_used_in_code(thinking_level):
    """The configured GEMINI_MODEL must accept every thinking level the codebase uses."""
    client = genai.Client(api_key=SMOKE_API_KEY)

    response = client.models.generate_content(
        model=Config.GEMINI_MODEL,
        contents="Reply with OK",
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level=thinking_level)
        ),
    )

    assert response.text

