import json
from pathlib import Path

import pytest
from PIL import Image

from sfps import matcher, organizer
from sfps.config import Config
from sfps.models import GameGuess, SafeEvent

EVENT = SafeEvent(
    event_id="2466440",
    name="Texas Super Kings vs Washington Freedom",
    sport="Cricket",
    league="Major League Cricket",
    season="2026",
    round="27",
    home_team="Texas Super Kings",
    away_team="Washington Freedom",
    event_date="2026-07-11",
    venue="Grand Prairie Stadium",
    artwork={
        "thumb": "https://img.example/thumb.jpg",
        "poster": "https://img.example/poster.jpg",
        "fanart": "https://img.example/fanart.jpg",
    },
)

GUESS = GameGuess(
    identified=True,
    sport="Cricket",
    league="Major League Cricket",
    home_team="Texas Super Kings",
    away_team="Washington Freedom",
    event_date="2026-07-11",
    confidence=0.95,
    source="gemini",
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.from_env(
        env={
            "GROQ_API_KEY": "x",
            "LIBRARY_DIR": str(tmp_path / "library"),
            "CONFIG_DIR": str(tmp_path / "config"),
        }
    )


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    f = tmp_path / "watch" / "Live Major League Cricket_Texas_v_Washington_20260711_220434.mkv"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00" * 4096)
    return f


@pytest.fixture
def fake_downloads(monkeypatch):
    """Replace network artwork download with locally generated images."""

    def fake(event, dest_dir, config, client=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved = {}
        for kind in event.artwork:
            p = dest_dir / f"{kind}.jpg"
            Image.new("RGB", (320, 180), (10, 90, 40)).save(p, "JPEG")
            saved[kind] = p
        return saved

    monkeypatch.setattr(matcher, "download_artwork", fake)
    return fake


# --- naming -------------------------------------------------------------------


def test_build_names_full():
    game_dir, episode = organizer.build_names(EVENT, "full", ".mkv")
    assert game_dir == Path(
        "Major League Cricket/Season 2026/"
        "Texas Super Kings vs Washington Freedom 2026-07-11"
    )
    assert episode == (
        "Major League Cricket - 2026-07-11 - Texas Super Kings vs Washington Freedom.mkv"
    )


def test_build_names_highlights_suffix():
    game_dir, episode = organizer.build_names(EVENT, "highlights", ".ts")
    assert game_dir.name.endswith("(Highlights)")
    assert episode.endswith("(Highlights).ts")


def test_build_names_non_team_event():
    f1 = SafeEvent(
        event_id="1",
        name="Miami Grand Prix Sprint Qualifying",
        league="Formula 1",
        event_date="2026-05-01",
    )
    game_dir, episode = organizer.build_names(f1, "full", ".mkv")
    assert "Miami Grand Prix Sprint Qualifying" in game_dir.name
    assert episode == "Formula 1 - 2026-05-01 - Miami Grand Prix Sprint Qualifying.mkv"


def test_sanitize_strips_forbidden_characters():
    assert organizer._sanitize('AC/DC: The "Match"? *|<>') == "AC DC The Match"


# --- organize: matched --------------------------------------------------------


def test_organize_matched(config: Config, recording: Path, fake_downloads):
    result = organizer.organize(recording, GUESS, EVENT, config, dry_run=False)

    assert result.status == "organized"
    target = Path(result.target_dir)
    stem = "Major League Cricket - 2026-07-11 - Texas Super Kings vs Washington Freedom"
    assert (target / f"{stem}.mkv").is_file()
    assert (target / f"{stem}.jpg").is_file()  # episode thumb, Local Media Assets naming
    assert (target / "poster.jpg").is_file()
    assert (target / "background.jpg").is_file()
    assert not recording.exists()  # moved, not copied

    sidecar = json.loads((target / "game.json").read_text(encoding="utf-8"))
    assert sidecar["matched"] is True
    assert sidecar["thesportsdb_event_id"] == "2466440"
    assert sidecar["variant"] == "full"
    assert sidecar["spoiler_free"] is True
    assert sidecar["artwork"]["thumb"] == "downloaded"


def test_sidecar_never_contains_scores(config: Config, recording: Path, fake_downloads):
    result = organizer.organize(recording, GUESS, EVENT, config, dry_run=False)
    text = Path(result.sidecar).read_text(encoding="utf-8").lower()
    for banned in ("score", "winner", "result", "won by", "status"):
        assert banned not in text


def test_organize_highlights_badges_thumb(config: Config, recording: Path, fake_downloads):
    guess = GameGuess(**{**GUESS.__dict__, "variant": "highlights"})
    result = organizer.organize(recording, guess, EVENT, config, dry_run=False)

    target = Path(result.target_dir)
    assert target.name.endswith("(Highlights)")
    sidecar = json.loads((target / "game.json").read_text(encoding="utf-8"))
    assert sidecar["variant"] == "highlights"
    assert sidecar["variant_description"] == "Highlights package"
    assert sidecar["artwork"]["thumb"] == "downloaded+badge"

    # badged thumb differs from the plain downloaded image
    thumb = next(target.glob("*(Highlights).jpg"))
    plain = Image.new("RGB", (320, 180), (10, 90, 40))
    assert thumb.stat().st_size > 0
    badged = Image.open(thumb)
    assert badged.size == plain.size


def test_organize_generates_card_when_no_thumb(config: Config, recording: Path, monkeypatch):
    def no_art(event, dest_dir, config, client=None):
        return {}

    monkeypatch.setattr(matcher, "download_artwork", no_art)
    result = organizer.organize(recording, GUESS, EVENT, config, dry_run=False)
    target = Path(result.target_dir)
    sidecar = json.loads((target / "game.json").read_text(encoding="utf-8"))
    assert sidecar["artwork"]["thumb"] == "generated"
    thumbs = [p for p in target.glob("*.jpg") if p.name not in ("poster.jpg", "background.jpg")]
    assert len(thumbs) == 1
    assert Image.open(thumbs[0]).size == (1280, 720)


def test_organize_collision_errors(config: Config, recording: Path, fake_downloads):
    first = organizer.organize(recording, GUESS, EVENT, config, dry_run=False)
    assert first.status == "organized"
    # same file arrives again
    again = recording
    again.parent.mkdir(parents=True, exist_ok=True)
    again.write_bytes(b"\x00" * 128)
    second = organizer.organize(again, GUESS, EVENT, config, dry_run=False)
    assert second.status == "error"
    assert again.exists()  # source untouched on error


def test_organize_preserve_original(tmp_path: Path, recording: Path, fake_downloads):
    cfg = Config.from_env(
        env={
            "GROQ_API_KEY": "x",
            "LIBRARY_DIR": str(tmp_path / "library"),
            "CONFIG_DIR": str(tmp_path / "config"),
            "PRESERVE_ORIGINAL": "true",
        }
    )
    result = organizer.organize(recording, GUESS, EVENT, cfg, dry_run=False)
    assert result.status == "organized"
    assert recording.exists(), "original must remain in place"
    assert Path(result.media_file).is_file()
    assert Path(result.media_file).stat().st_size == recording.stat().st_size


def test_generated_badge_matchup_card(config: Config, recording: Path, monkeypatch):
    """No downloadable event art + badges available -> badge-vs-badge card."""
    from PIL import Image as PILImage

    monkeypatch.setattr(
        matcher, "download_artwork", lambda event, dest, cfg, client=None: {}
    )
    monkeypatch.setattr(
        matcher,
        "team_badges",
        lambda home, away, cfg, sport="", client=None: {
            "home": "http://x/h.png",
            "away": "http://x/a.png",
        },
    )

    def fake_badge_downloads(urls, dest_dir, cfg, client=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for kind, color in (("home", (200, 180, 20)), ("away", (180, 20, 20))):
            p = dest_dir / f"{kind}.png"
            PILImage.new("RGBA", (200, 200), color).save(p, "PNG")
            out[kind] = p
        return out

    monkeypatch.setattr(matcher, "download_urls", fake_badge_downloads)

    result = organizer.organize(recording, GUESS, EVENT, config, dry_run=False)
    target = Path(result.target_dir)
    sidecar = json.loads((target / "game.json").read_text(encoding="utf-8"))
    assert sidecar["artwork"]["thumb"] == "generated-badges"
    thumbs = [p for p in target.glob("*.jpg") if p.name not in ("poster.jpg", "background.jpg")]
    assert PILImage.open(thumbs[0]).size == (1280, 720)


# --- organize: unknown ----------------------------------------------------------


def test_organize_unknown_placeholder(config: Config, recording: Path):
    guess = GameGuess(identified=False, source="gemini")
    result = organizer.organize(recording, guess, None, config, dry_run=False)

    assert result.status == "unknown"
    target = Path(result.target_dir)
    assert organizer.UNKNOWN_DIR_NAME in str(target)
    assert (target / recording.name).is_file()
    thumb = target / f"{recording.stem}.jpg"
    assert thumb.is_file()
    assert Image.open(thumb).size == (1280, 720)

    sidecar = json.loads((target / "game.json").read_text(encoding="utf-8"))
    assert sidecar["matched"] is False
    assert sidecar["original_filename"] == recording.name


def test_organize_unknown_but_identified_uses_badge_matchup_card(
    config: Config, recording: Path, monkeypatch
):
    """Reproduces the Australia v France log: Gemini identifies the game but
    TheSportsDB has no record of the (brand-new) competition. The result must
    be a real team-badge card, not a bare 'Unknown Event' label."""
    from PIL import Image as PILImage

    guess = GameGuess(
        identified=True,
        sport="Rugby Union",
        league="World Rugby Nations Championship",
        home_team="Australia",
        away_team="France",
        event_date="2026-07-11",
        confidence=0.85,
        variant="highlights",
        source="gemini",
    )
    monkeypatch.setattr(
        matcher,
        "team_badges",
        lambda home, away, cfg, sport="", client=None: {
            "home": "http://x/aus.png",
            "away": "http://x/fra.png",
        },
    )

    def fake_badge_downloads(urls, dest_dir, cfg, client=None):
        dest_dir.mkdir(parents=True, exist_ok=True)
        out = {}
        for kind, color in (("home", (200, 180, 20)), ("away", (0, 60, 160))):
            p = dest_dir / f"{kind}.png"
            PILImage.new("RGBA", (200, 200), color).save(p, "PNG")
            out[kind] = p
        return out

    monkeypatch.setattr(matcher, "download_urls", fake_badge_downloads)

    result = organizer.organize(recording, guess, None, config, dry_run=False)

    assert result.status == "unknown"
    target = Path(result.target_dir)
    sidecar = json.loads((target / "game.json").read_text(encoding="utf-8"))
    assert sidecar["matched"] is False
    assert sidecar["home_team"] == "Australia"
    assert sidecar["artwork"]["thumb"] == "generated-badges-unverified+badge"  # + Highlights badge
    thumb = target / f"{recording.stem}.jpg"
    assert PILImage.open(thumb).size == (1280, 720)


def test_organize_unknown_but_identified_no_badges_uses_descriptive_text(
    config: Config, recording: Path
):
    """No badges available (conftest stub returns {}) -> still better than the
    bare 'Unknown Event' label: a descriptive team/league text card."""
    guess = GameGuess(
        identified=True,
        sport="Rugby Union",
        league="World Rugby Nations Championship",
        home_team="Australia",
        away_team="France",
        event_date="2026-07-11",
        confidence=0.85,
        source="gemini",
    )
    result = organizer.organize(recording, guess, None, config, dry_run=False)
    sidecar = json.loads((Path(result.target_dir) / "game.json").read_text(encoding="utf-8"))
    assert sidecar["artwork"]["thumb"] == "generated-unverified"
    assert sidecar["artwork"]["thumb"] != "placeholder-generated"


def test_organize_unknown_event_name_only_uses_descriptive_text(config: Config, recording: Path):
    """Non-team sports (e.g. motorsport) identified-but-unmatched: event_name
    drives the card, not the bare filename."""
    guess = GameGuess(
        identified=True,
        sport="Motorsport",
        event_name="Miami Grand Prix Sprint Qualifying",
        event_date="2026-05-01",
        confidence=0.9,
        source="gemini",
    )
    result = organizer.organize(recording, guess, None, config, dry_run=False)
    sidecar = json.loads((Path(result.target_dir) / "game.json").read_text(encoding="utf-8"))
    assert sidecar["artwork"]["thumb"] == "generated-unverified"
    assert sidecar["event_name"] == "Miami Grand Prix Sprint Qualifying"


def test_organize_unknown_custom_asset_wins_even_when_identified(
    config: Config, recording: Path
):
    """A user-supplied unknown-event.jpg always takes priority, even over an
    identified guess that could produce a badge card."""
    custom = config.config_dir / "unknown-event.jpg"
    custom.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 360), (80, 0, 0)).save(custom, "JPEG")

    guess = GameGuess(
        identified=True, home_team="Australia", away_team="France", confidence=0.85
    )
    result = organizer.organize(recording, guess, None, config, dry_run=False)
    sidecar = json.loads((Path(result.target_dir) / "game.json").read_text(encoding="utf-8"))
    assert sidecar["artwork"]["thumb"] == "placeholder-custom"


def test_organize_unknown_uses_custom_asset(config: Config, recording: Path):
    custom = config.config_dir / "unknown-event.jpg"
    custom.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 360), (80, 0, 0)).save(custom, "JPEG")

    guess = GameGuess(identified=False)
    result = organizer.organize(recording, guess, None, config, dry_run=False)
    sidecar = json.loads((Path(result.target_dir) / "game.json").read_text(encoding="utf-8"))
    assert sidecar["artwork"]["thumb"] == "placeholder-custom"
    thumb = Path(result.target_dir) / f"{recording.stem}.jpg"
    assert Image.open(thumb).size == (640, 360)


# --- dry run ---------------------------------------------------------------------


def test_organize_dry_run_writes_nothing(config: Config, recording: Path):
    result = organizer.organize(recording, GUESS, EVENT, config, dry_run=True)
    assert result.status == "planned"
    assert recording.exists()
    assert not config.library_dir.exists()
