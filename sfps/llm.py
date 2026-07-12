"""LLM provider wrapper for game identification.

Isolated here so tests monkeypatch `generate_json` and no other module knows
which provider is in use. Supports Groq (default, OpenAI-compatible, via httpx)
and Gemini (fallback, via the google-genai SDK). Both take the same lenient
JSON schema; the Groq path adapts it to strict structured-output rules.
"""

from __future__ import annotations

import logging

import httpx

from sfps.config import Config

log = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMError(Exception):
    """The LLM call failed or returned something unusable."""


def generate_json(
    config: Config,
    system_instruction: str,
    prompt: str,
    response_schema: dict,
) -> str:
    """Run one structured-output generation and return the raw JSON text."""
    if config.llm_provider == "groq":
        return _groq_generate(config, system_instruction, prompt, response_schema)
    return _gemini_generate(config, system_instruction, prompt, response_schema)


def _strict_schema(schema: dict) -> dict:
    """Adapt a lenient JSON schema to Groq strict mode: every property required
    and additionalProperties disabled (recursively for nested objects)."""
    if schema.get("type") != "object" or "properties" not in schema:
        return schema
    out = dict(schema)
    out["properties"] = {k: _strict_schema(v) for k, v in schema["properties"].items()}
    out["required"] = list(out["properties"].keys())
    out["additionalProperties"] = False
    return out


def _groq_generate(
    config: Config, system_instruction: str, prompt: str, response_schema: dict
) -> str:
    body = {
        "model": config.groq_model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "game_identification",
                "strict": True,
                "schema": _strict_schema(response_schema),
            },
        },
    }
    try:
        response = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {config.groq_api_key}"},
            json=body,
            timeout=60.0,
        )
    except httpx.HTTPError as exc:
        raise LLMError(f"Groq request failed: {type(exc).__name__}") from exc

    if response.status_code == 429:
        raise LLMError("Groq rate limit hit (HTTP 429)")
    if response.status_code != 200:
        # Body may echo the request; keep it out of INFO logs to be safe.
        log.debug("groq error body: %s", response.text[:500])
        raise LLMError(f"Groq returned HTTP {response.status_code}")

    try:
        data = response.json()
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Groq response not understood: {type(exc).__name__}") from exc
    if not text:
        raise LLMError("Groq returned an empty response")
    return text


def _gemini_generate(
    config: Config, system_instruction: str, prompt: str, response_schema: dict
) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise LLMError(
            "google-genai is not installed; install with pip install .[gemini] "
            "or set LLM_PROVIDER=groq"
        ) from exc

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
        raise LLMError(f"Gemini API call failed: {type(exc).__name__}") from exc

    text = response.text
    if not text:
        raise LLMError("Gemini returned an empty response")
    return text
