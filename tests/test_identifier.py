import json
from datetime import datetime

import pytest

from sfps import identifier, llm
from sfps.config import Config

# Real recorder output — the Phase 2 fixture set (design.md §5 Phase 2)
REAL_FILENAMES = [
    "JWC South Africa v Wales.mkv",
    "Nations Champs '26 RSA v ENG.mkv",
    "Formula_1_Highlights___Miami_Grand_Prix__Sprint_Qualifying_20260502_224400.ts",
    "Live Major League Cricket_Texas_v_Washington_20260711_220434.mkv",
]


@pytest.fixture
def config() -> Config:
    return Config.from_env(env={"GROQ_API_KEY": "test-key", "TZ": "Australia/Sydney"})


# --- timestamp pre-pass -----------------------------------------------------


def test_extracts_recorder_timestamp():
    ts = identifier.extract_timestamp(
        "Formula_1_Highlights___Miami_Grand_Prix__Sprint_Qualifying_20260502_224400.ts"
    )
    assert ts == datetime(2026, 5, 2, 22, 44, 0)


def test_extracts_recorder_timestamp_cricket():
    name = "Live Major League Cricket_Texas_v_Washington_20260711_220434.mkv"
    assert identifier.extract_timestamp(name) == datetime(2026, 7, 11, 22, 4, 34)


def test_extracts_iso_date():
    ts = identifier.extract_timestamp("EPL Arsenal vs Chelsea 2026-07-12.ts")
    assert ts == datetime(2026, 7, 12)


def test_no_timestamp_returns_none():
    assert identifier.extract_timestamp("JWC South Africa v Wales.mkv") is None


def test_invalid_timestamp_digits_ignored():
    assert identifier.extract_timestamp("show_20261399_990000.ts") is None


# --- variant detection -------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "variant"),
    [
        (REAL_FILENAMES[2], "highlights"),  # the F1 Highlights sample
        ("EPL Arsenal v Chelsea HL.mkv", "highlights"),
        ("NRL.Round.5.HLS.ts", "highlights"),
        ("Super Rugby Mini Crusaders v Blues.mkv", "mini"),
        ("JWC South Africa v Wales.mkv", "full"),
        # substrings must NOT trigger: 'hl' inside a word, 'mini' inside a word
        ("Ashland vs Chlothar.mkv", "full"),
        ("Minions League Final.mkv", "full"),
    ],
)
def test_detect_variant(filename: str, variant: str):
    assert identifier.detect_variant(filename) == variant


def test_variant_carried_on_guess(monkeypatch, config: Config):
    _mock_response(monkeypatch, {"identified": True, "confidence": 0.9})
    guess = identifier.identify_name("EPL Arsenal v Chelsea HL.mkv", None, config)
    assert guess.variant == "highlights"


def test_variant_set_even_on_llm_failure(monkeypatch, config: Config):
    def boom(config, system_instruction, prompt, response_schema):
        raise llm.LLMError("down")

    monkeypatch.setattr(llm, "generate_json", boom)
    guess = identifier.identify_name("Mini Match Arsenal v Chelsea.mkv", None, config)
    assert guess.variant == "mini"
    assert not guess.identified


# --- prompt building --------------------------------------------------------


def test_prompt_contains_filename_and_hints(config: Config):
    prompt = identifier._build_prompt(
        "Live Major League Cricket_Texas_v_Washington_20260711_220434.mkv",
        mtime=datetime(2026, 7, 12, 1, 30),
        config=config,
    )
    assert "Texas_v_Washington" in prompt
    assert "2026-07-11T22:04:34" in prompt
    assert "2026-07-12T01:30:00" in prompt
    assert "Australia/Sydney" in prompt


# --- response handling (Gemini mocked) --------------------------------------


def _mock_response(monkeypatch, payload):
    def fake_generate_json(config, system_instruction, prompt, response_schema):
        return json.dumps(payload) if isinstance(payload, dict) else payload

    monkeypatch.setattr(llm, "generate_json", fake_generate_json)


def test_identify_parses_good_response(monkeypatch, config: Config):
    _mock_response(
        monkeypatch,
        {
            "identified": True,
            "sport": "Rugby",
            "league": "World Rugby U20 Championship",
            "home_team": "South Africa",
            "away_team": "Wales",
            "event_date": "2026-07-05",
            "confidence": 0.85,
            "notes": "JWC expanded to World Rugby U20 Championship",
        },
    )
    guess = identifier.identify_name("JWC South Africa v Wales.mkv", None, config)
    assert guess.identified
    assert guess.league == "World Rugby U20 Championship"
    assert guess.home_team == "South Africa"
    assert guess.source == "groq"  # provider name from config


def test_identify_clamps_confidence(monkeypatch, config: Config):
    _mock_response(monkeypatch, {"identified": True, "confidence": 3.5})
    guess = identifier.identify_name("x.ts", None, config)
    assert guess.confidence == 1.0


def test_partial_date_is_blanked(monkeypatch, config: Config):
    """Gemini sometimes returns a bare year ('2026'); the matcher must never see it."""
    _mock_response(monkeypatch, {"identified": True, "confidence": 0.8, "event_date": "2026"})
    guess = identifier.identify_name("x.ts", None, config)
    assert guess.event_date == ""


def test_full_iso_date_is_kept(monkeypatch, config: Config):
    _mock_response(
        monkeypatch, {"identified": True, "confidence": 0.8, "event_date": "2026-07-11"}
    )
    guess = identifier.identify_name("x.ts", None, config)
    assert guess.event_date == "2026-07-11"


def test_identify_handles_garbage_response(monkeypatch, config: Config):
    _mock_response(monkeypatch, "this is not json {")
    guess = identifier.identify_name("x.ts", None, config)
    assert not guess.identified
    assert "bad response" in guess.notes


def test_identify_handles_llm_error(monkeypatch, config: Config):
    def boom(config, system_instruction, prompt, response_schema):
        raise llm.LLMError("simulated outage")

    monkeypatch.setattr(llm, "generate_json", boom)
    guess = identifier.identify_name("x.ts", None, config)
    assert not guess.identified
    assert "llm error" in guess.notes


@pytest.mark.parametrize("filename", REAL_FILENAMES)
def test_real_filenames_flow_through(monkeypatch, config: Config, filename: str):
    """Plumbing check: every real sample reaches Gemini and parses back."""
    seen = {}

    def capture(config, system_instruction, prompt, response_schema):
        seen["prompt"] = prompt
        return json.dumps({"identified": False, "confidence": 0.0, "notes": "mock"})

    monkeypatch.setattr(llm, "generate_json", capture)
    guess = identifier.identify_name(filename, None, config)
    assert filename in seen["prompt"]
    assert guess.source == "groq"


def test_spoiler_rule_present_in_system_prompt():
    """The system prompt must forbid score/result output (design.md §1)."""
    assert "NEVER" in identifier.SYSTEM_INSTRUCTION
    assert "scores" in identifier.SYSTEM_INSTRUCTION
