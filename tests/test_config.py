from pathlib import Path

from sfps.config import Config


def test_defaults():
    cfg = Config.from_env(env={})
    assert cfg.thesportsdb_api_key == "123"
    assert cfg.min_confidence == 0.6
    assert cfg.stability_seconds == 120
    assert cfg.media_extensions == (".ts", ".mkv", ".mp4")
    assert cfg.artwork_mode == "download"
    assert cfg.dry_run is False
    assert cfg.watch_dir == Path("/watch")
    assert cfg.library_dir == Path("/library")


def test_env_overrides():
    cfg = Config.from_env(
        env={
            "GEMINI_API_KEY": "abc",
            "MIN_CONFIDENCE": "0.8",
            "STABILITY_SECONDS": "30",
            "MEDIA_EXTENSIONS": "mkv, .MP4",
            "ARTWORK_MODE": "Generate",
            "DRY_RUN": "true",
            "WATCH_DIR": "/staging",
        }
    )
    assert cfg.gemini_api_key == "abc"
    assert cfg.min_confidence == 0.8
    assert cfg.stability_seconds == 30
    assert cfg.media_extensions == (".mkv", ".mp4")
    assert cfg.artwork_mode == "generate"
    assert cfg.dry_run is True
    assert cfg.watch_dir == Path("/staging")


def test_bad_numeric_values_fall_back_to_defaults():
    cfg = Config.from_env(env={"MIN_CONFIDENCE": "lots", "STABILITY_SECONDS": "soon"})
    assert cfg.min_confidence == 0.6
    assert cfg.stability_seconds == 120


def test_validate_reports_missing_gemini_key():
    cfg = Config.from_env(env={})
    problems = cfg.validate()
    assert any("GEMINI_API_KEY" in p for p in problems)


def test_validate_ok_with_keys():
    cfg = Config.from_env(env={"GEMINI_API_KEY": "abc"})
    assert cfg.validate() == []


def test_validate_rejects_bad_artwork_mode():
    cfg = Config.from_env(env={"GEMINI_API_KEY": "abc", "ARTWORK_MODE": "steal"})
    assert any("ARTWORK_MODE" in p for p in cfg.validate())


def test_validate_requires_plex_url_and_token_together():
    cfg = Config.from_env(env={"GEMINI_API_KEY": "abc", "PLEX_URL": "http://plex:32400"})
    assert any("PLEX_URL" in p for p in cfg.validate())
