import json

import pytest

from sfps import llm, matcher


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """Safety net: no test may hit a real LLM API (Groq or Gemini).

    Individual tests override this with their own monkeypatch when they need
    a specific canned response.
    """

    def offline_stub(config, system_instruction, prompt, response_schema):
        return json.dumps(
            {"identified": False, "confidence": 0.0, "notes": "conftest offline stub"}
        )

    monkeypatch.setattr(llm, "generate_json", offline_stub)


@pytest.fixture(autouse=True)
def _no_real_badge_lookups(monkeypatch):
    """Safety net: generated thumbs must not hit TheSportsDB for team badges.

    Tests of the real function bind it directly at import time
    (`from sfps.matcher import team_badges`), which bypasses this stub.
    """
    monkeypatch.setattr(
        matcher, "team_badges", lambda home_team, away_team, config, sport="", client=None: {}
    )
