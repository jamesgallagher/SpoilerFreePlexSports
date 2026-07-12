"""Configuration loaded from environment variables.

Every knob in design.md §4 lives here. `Config.from_env()` never raises on
missing values — `validate()` reports problems so the CLI can decide whether
they are fatal (real run) or just warnings (dry run / stub phases).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in _TRUE


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None and value.strip() else default
    except ValueError:
        return default


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None and value.strip() else default
    except ValueError:
        return default


def _extensions(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    parts = []
    for raw in value.split(","):
        ext = raw.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        parts.append(ext)
    return tuple(parts) if parts else default


@dataclass(frozen=True)
class Config:
    """Runtime configuration. See design.md §4 for the full table."""

    # LLM provider for game identification: "groq" | "gemini"
    llm_provider: str = "groq"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    thesportsdb_api_key: str = "123"  # free/dev key; premium key recommended

    # Behaviour
    timezone: str = "UTC"
    min_confidence: float = 0.6
    stability_seconds: int = 120
    sweep_seconds: int = 300  # periodic re-scan of /watch (inotify safety net)
    media_extensions: tuple[str, ...] = (".mp4", ".mkv", ".avi", ".mov", ".mpeg", ".ts")
    artwork_mode: str = "download"  # "download" | "generate"
    preserve_original: bool = False  # true = copy into library, leave original in /watch
    retry_days: int = 7
    dry_run: bool = False
    log_level: str = "INFO"

    # Paths (docker volume mount points by default)
    watch_dir: Path = field(default_factory=lambda: Path("/watch"))
    library_dir: Path = field(default_factory=lambda: Path("/library"))
    config_dir: Path = field(default_factory=lambda: Path("/config"))

    # Optional Plex partial-rescan integration
    plex_url: str = ""
    plex_token: str = ""
    # If Plex mounts the library at a different path than this container's
    # /library, set the Plex-side path here (e.g. /data/sports).
    plex_library_path: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        defaults = cls()
        return cls(
            llm_provider=env.get("LLM_PROVIDER", defaults.llm_provider).strip().lower(),
            groq_api_key=env.get("GROQ_API_KEY", defaults.groq_api_key),
            groq_model=env.get("GROQ_MODEL", defaults.groq_model),
            gemini_api_key=env.get("GEMINI_API_KEY", defaults.gemini_api_key),
            gemini_model=env.get("GEMINI_MODEL", defaults.gemini_model),
            thesportsdb_api_key=env.get("THESPORTSDB_API_KEY", defaults.thesportsdb_api_key),
            timezone=env.get("TZ", defaults.timezone),
            min_confidence=_float(env.get("MIN_CONFIDENCE"), defaults.min_confidence),
            stability_seconds=_int(env.get("STABILITY_SECONDS"), defaults.stability_seconds),
            sweep_seconds=_int(env.get("SWEEP_SECONDS"), defaults.sweep_seconds),
            media_extensions=_extensions(env.get("MEDIA_EXTENSIONS"), defaults.media_extensions),
            artwork_mode=env.get("ARTWORK_MODE", defaults.artwork_mode).strip().lower(),
            preserve_original=_bool(env.get("PRESERVE_ORIGINAL"), defaults.preserve_original),
            retry_days=_int(env.get("RETRY_DAYS"), defaults.retry_days),
            dry_run=_bool(env.get("DRY_RUN"), defaults.dry_run),
            log_level=env.get("LOG_LEVEL", defaults.log_level).strip().upper(),
            watch_dir=Path(env.get("WATCH_DIR", str(defaults.watch_dir))),
            library_dir=Path(env.get("LIBRARY_DIR", str(defaults.library_dir))),
            config_dir=Path(env.get("CONFIG_DIR", str(defaults.config_dir))),
            plex_url=env.get("PLEX_URL", defaults.plex_url).rstrip("/"),
            plex_token=env.get("PLEX_TOKEN", defaults.plex_token),
            plex_library_path=env.get("PLEX_LIBRARY_PATH", defaults.plex_library_path),
        )

    @property
    def llm_api_key(self) -> str:
        """API key for the active LLM provider."""
        return self.groq_api_key if self.llm_provider == "groq" else self.gemini_api_key

    @property
    def llm_model(self) -> str:
        """Model id for the active LLM provider."""
        return self.groq_model if self.llm_provider == "groq" else self.gemini_model

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty = all good)."""
        problems: list[str] = []
        if self.llm_provider not in ("groq", "gemini"):
            problems.append(
                f"LLM_PROVIDER must be 'groq' or 'gemini', got '{self.llm_provider}'"
            )
        elif not self.llm_api_key:
            key = "GROQ_API_KEY" if self.llm_provider == "groq" else "GEMINI_API_KEY"
            problems.append(f"{key} is not set (required for game identification)")
        if not self.thesportsdb_api_key:
            problems.append("THESPORTSDB_API_KEY is not set")
        if self.artwork_mode not in ("download", "generate"):
            problems.append(
                f"ARTWORK_MODE must be 'download' or 'generate', got '{self.artwork_mode}'"
            )
        if not 0.0 <= self.min_confidence <= 1.0:
            problems.append(f"MIN_CONFIDENCE must be between 0 and 1, got {self.min_confidence}")
        if bool(self.plex_url) != bool(self.plex_token):
            problems.append("PLEX_URL and PLEX_TOKEN must be set together (or neither)")
        return problems
