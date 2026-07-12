import json

import httpx
import pytest

from sfps import llm
from sfps.config import Config

# Bound before conftest's autouse stub replaces llm.generate_json, so the
# dispatch tests below exercise the real dispatcher (which still looks up
# _groq_generate / _gemini_generate as module globals we can monkeypatch).
real_generate_json = llm.generate_json

GROQ_CFG = Config.from_env(env={"GROQ_API_KEY": "test-key", "GROQ_MODEL": "openai/gpt-oss-120b"})
SCHEMA = {
    "type": "object",
    "properties": {
        "identified": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["identified"],
}


def test_strict_schema_marks_all_required_and_closed():
    out = llm._strict_schema(SCHEMA)
    assert out["required"] == ["identified", "confidence"]  # ALL props, not just original
    assert out["additionalProperties"] is False
    # original is untouched (no mutation)
    assert SCHEMA["required"] == ["identified"]


def test_strict_schema_recurses_into_nested_objects():
    nested = {
        "type": "object",
        "properties": {"meta": {"type": "object", "properties": {"a": {"type": "string"}}}},
    }
    out = llm._strict_schema(nested)
    assert out["properties"]["meta"]["additionalProperties"] is False
    assert out["properties"]["meta"]["required"] == ["a"]


def test_groq_builds_openai_compatible_request_and_parses(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, body=json)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"identified": true, "confidence": 0.9}'}}]},
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    text = llm._groq_generate(GROQ_CFG, "system text", "user text", SCHEMA)

    assert json.loads(text)["identified"] is True
    assert captured["url"] == llm.GROQ_URL
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    body = captured["body"]
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["temperature"] == 0
    assert body["messages"][0] == {"role": "system", "content": "system text"}
    assert body["messages"][1] == {"role": "user", "content": "user text"}
    fmt = body["response_format"]["json_schema"]
    assert fmt["strict"] is True
    assert fmt["schema"]["additionalProperties"] is False


def test_groq_reasoning_field_is_ignored(monkeypatch):
    """gpt-oss returns a separate `reasoning` field; only `content` is used."""

    def fake_post(url, headers, json, timeout):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"identified": false}', "reasoning": "lots of it"}}
                ]
            },
        )

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert json.loads(llm._groq_generate(GROQ_CFG, "s", "u", SCHEMA)) == {"identified": False}


def test_groq_rate_limit_raises_llmerror(monkeypatch):
    monkeypatch.setattr(llm.httpx, "post", lambda *a, **kw: httpx.Response(429, text="slow down"))
    with pytest.raises(llm.LLMError, match="rate limit"):
        llm._groq_generate(GROQ_CFG, "s", "u", SCHEMA)


def test_groq_http_error_raises_llmerror(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("groq unreachable")

    monkeypatch.setattr(llm.httpx, "post", boom)
    with pytest.raises(llm.LLMError):
        llm._groq_generate(GROQ_CFG, "s", "u", SCHEMA)


def test_groq_empty_content_raises_llmerror(monkeypatch):
    monkeypatch.setattr(
        llm.httpx,
        "post",
        lambda *a, **kw: httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
    )
    with pytest.raises(llm.LLMError, match="empty"):
        llm._groq_generate(GROQ_CFG, "s", "u", SCHEMA)


def test_groq_malformed_json_body_raises_llmerror(monkeypatch):
    monkeypatch.setattr(
        llm.httpx, "post", lambda *a, **kw: httpx.Response(200, json={"unexpected": "shape"})
    )
    with pytest.raises(llm.LLMError, match="not understood"):
        llm._groq_generate(GROQ_CFG, "s", "u", SCHEMA)


def test_dispatch_routes_to_groq(monkeypatch):
    monkeypatch.setattr(llm, "_groq_generate", lambda *a: "GROQ")
    monkeypatch.setattr(llm, "_gemini_generate", lambda *a: "GEMINI")
    assert real_generate_json(GROQ_CFG, "s", "u", SCHEMA) == "GROQ"


def test_dispatch_routes_to_gemini(monkeypatch):
    monkeypatch.setattr(llm, "_groq_generate", lambda *a: "GROQ")
    monkeypatch.setattr(llm, "_gemini_generate", lambda *a: "GEMINI")
    cfg = Config.from_env(env={"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "k"})
    assert real_generate_json(cfg, "s", "u", SCHEMA) == "GEMINI"
