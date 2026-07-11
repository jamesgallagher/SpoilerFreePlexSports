import json

import pytest

from sfps import gemini


@pytest.fixture(autouse=True)
def _no_real_gemini(monkeypatch):
    """Safety net: no test may hit the real Gemini API.

    Individual tests override this with their own monkeypatch when they need
    a specific canned response.
    """

    def offline_stub(config, system_instruction, prompt, response_schema):
        return json.dumps(
            {"identified": False, "confidence": 0.0, "notes": "conftest offline stub"}
        )

    monkeypatch.setattr(gemini, "generate_json", offline_stub)
