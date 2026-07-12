from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

from sfps import plex
from sfps.config import Config

SECTIONS_XML = """<?xml version="1.0"?>
<MediaContainer size="2">
  <Directory key="5" type="show" title="Sports">
    <Location id="10" path="/data/sports"/>
  </Directory>
  <Directory key="2" type="movie" title="Movies">
    <Location id="11" path="/data/movies"/>
  </Directory>
</MediaContainer>
"""


def make_config(**extra) -> Config:
    env = {
        "GEMINI_API_KEY": "x",
        "PLEX_URL": "http://plex:32400",
        "PLEX_TOKEN": "tok",
        "LIBRARY_DIR": "/library",
    }
    env.update(extra)
    return Config.from_env(env=env)


def test_disabled_without_credentials():
    cfg = Config.from_env(env={"GEMINI_API_KEY": "x"})
    assert not plex.enabled(cfg)
    assert plex.rescan(cfg, Path("/library/x")) is False


def test_path_mapping():
    cfg = make_config(PLEX_LIBRARY_PATH="/data/sports")
    mapped = plex.plex_path(cfg, Path("/library/Formula 1/Season 2026/Race"))
    assert mapped == "/data/sports/Formula 1/Season 2026/Race"


def test_path_unmapped_when_not_configured():
    cfg = make_config()
    assert plex.plex_path(cfg, Path("/library/A/B")) == "/library/A/B"


def test_rescan_hits_matching_section():
    cfg = make_config(PLEX_LIBRARY_PATH="/data/sports")
    refreshes = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Plex-Token"] == "tok"
        if request.url.path == "/library/sections":
            return httpx.Response(200, text=SECTIONS_XML)
        if request.url.path == "/library/sections/5/refresh":
            refreshes.append(parse_qs(urlparse(str(request.url)).query)["path"][0])
            return httpx.Response(200)
        raise AssertionError(f"unexpected call: {request.url}")

    ok = plex.rescan(
        cfg,
        Path("/library/Major League Cricket/Season 2026/Game"),
        transport=httpx.MockTransport(handler),
    )
    assert ok
    assert refreshes == ["/data/sports/Major League Cricket/Season 2026/Game"]


def test_rescan_no_matching_section_returns_false():
    cfg = make_config()  # /library/... doesn't map to any Plex location

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections":
            return httpx.Response(200, text=SECTIONS_XML)
        raise AssertionError("refresh must not be called")

    assert plex.rescan(cfg, Path("/library/X"), transport=httpx.MockTransport(handler)) is False


def test_rescan_survives_plex_being_down():
    cfg = make_config()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("plex offline")

    assert plex.rescan(cfg, Path("/library/X"), transport=httpx.MockTransport(handler)) is False
