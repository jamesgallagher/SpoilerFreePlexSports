"""Thin Gemini API wrapper.

Isolated here so tests can monkeypatch `generate_json` and no other module
needs to import the google-genai SDK.
"""

from __future__ import annotations

import logging

from sfps.config import Config

log = logging.getLogger(__name__)


class GeminiError(Exception):
    """The Gemini API call failed or returned something unusable."""


def generate_json(
    config: Config,
    system_instruction: str,
    prompt: str,
    response_schema: dict,
) -> str:
    """Run one structured-output generation and return the raw JSON text."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise GeminiError("google-genai package is not installed") from exc

    try:
        client = genai.Client(api_key=config.gemini_api_key)
        response = client.models.generate_content(
            model=config.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.0,
            ),
        )
    except Exception as exc:
        raise GeminiError(f"Gemini API call failed: {type(exc).__name__}") from exc

    text = response.text
    if not text:
        raise GeminiError("Gemini returned an empty response")
    return text
