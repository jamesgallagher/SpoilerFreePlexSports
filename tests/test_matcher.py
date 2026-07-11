import dataclasses
import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx

from sfps import matcher
from sfps.config import Config
from sfps.models import GameGuess
from sfps.thesportsdb import TheSportsDBClient

CONFIG = Config.from_env(env={"GEMINI_API_KEY": "x"})

# A raw API event as TheSportsDB returns it — INCLUDING the score fields the
# firewall must strip.
RAW_EVENT = {
    "idEvent": "2052711",
    "strEvent": "Texas Super Kings vs Washington Freedom",
    "strSport": "Cricket",
    "strLeague": "Major League Cricket",
    "idLeague": "5620",
    "strSeason": "2026",
    "intRound": "12",
    "strHomeTeam": "Texas Super Kings",
    "strAwayTeam": "Washington Freedom",
    "dateEvent": "2026-07-11",
    "strVenue": "Grand Prairie Stadium",
    "strThumb": "https://img.example/event/thumb.jpg",
    "strPoster": "https://img.example/event/poster.jpg",
    "strFanart": "",
    "intHomeScore": "184",
    "intAwayScore": "180",
    "strStatus": "Match Finished",
    "strResult": "Texas Super Kings won by 4 runs",
    "strVideo": "https://youtube.example/highlights",
}

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


def make_client(handler) -> TheSportsDBClient:
    return TheSportsDBClient(
        CONFIG, transport=httpx.MockTransport(handler), min_interval=0.0
    )


def events_response(*events):
    return httpx.Response(200, json={"event": list(events) or None})


# --- spoiler firewall --------------------------------------------------------


def test_safe_event_strips_all_result_fields():
    event = matcher._to_safe_event(RAW_EVENT)
    dumped = json.dumps(dataclasses.asdict(event))
    assert "184" not in dumped
    assert "180" not in dumped
    assert "won by" not in dumped
    assert "Finished" not in dumped
    assert "youtube" not in dumped
    # ...while the safe fields survive
    assert event.event_id == "2052711"
    assert event.venue == "Grand Prairie Stadium"
    assert event.artwork == {
        "thumb": "https://img.example/event/thumb.jpg",
        "poster": "https://img.example/event/poster.jpg",
    }


def test_safe_event_type_has_no_score_fields():
    names = {f.name for f in dataclasses.fields(matcher.SafeEvent)}
    assert not names & {"home_score", "away_score", "score", "status", "result", "winner"}


# --- verification ------------------------------------------------------------


DATES = [date(2026, 7, 11), date(2026, 7, 10), date(2026, 7, 12)]


def test_verify_exact():
    assert matcher._verify(RAW_EVENT, GUESS, DATES)


def test_verify_accepts_name_variant():
    guess = dataclasses.replace(GUESS, home_team="Texas", away_team="Washington")
    assert matcher._verify(RAW_EVENT, guess, DATES)


def test_verify_accepts_swapped_home_away():
    guess = dataclasses.replace(
        GUESS, home_team="Washington Freedom", away_team="Texas Super Kings"
    )
    assert matcher._verify(RAW_EVENT, guess, DATES)


def test_verify_accepts_adjacent_date():
    guess = dataclasses.replace(GUESS, event_date="2026-07-12")
    dates = matcher._candidate_dates(guess, None)
    assert matcher._verify(RAW_EVENT, guess, dates)


def test_verify_rejects_date_outside_window():
    dates = [date(2026, 6, 1), date(2026, 5, 31), date(2026, 6, 2)]
    assert not matcher._verify(RAW_EVENT, GUESS, dates)


def test_verify_rejects_wrong_teams():
    guess = dataclasses.replace(GUESS, home_team="Seattle Orcas", away_team="MI New York")
    assert not matcher._verify(RAW_EVENT, guess, DATES)


def test_verify_rejects_league_mismatch():
    guess = dataclasses.replace(GUESS, league="Indian Premier League")
    assert not matcher._verify(RAW_EVENT, guess, DATES)


def test_verify_event_name_for_non_team_sports():
    raw = dict(RAW_EVENT, strEvent="Miami Grand Prix", strHomeTeam="", strAwayTeam="")
    guess = GameGuess(
        identified=True,
        league="Major League Cricket",
        event_name="Miami Grand Prix Sprint Qualifying",
        event_date="2026-07-11",
        confidence=0.9,
    )
    assert matcher._verify(raw, guess, DATES)


# --- match flow (mocked transport) -------------------------------------------


def test_match_via_search_with_date():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        params = parse_qs(urlparse(str(request.url)).query)
        if "searchevents.php" in request.url.path and params.get("d"):
            return events_response(RAW_EVENT)
        return events_response()

    with make_client(handler) as client:
        event = matcher.match(GUESS, CONFIG, client=client)
    assert event is not None
    assert event.event_id == "2052711"
    assert "Texas_Super_Kings_vs_Washington_Freedom" in calls[0]


def test_match_falls_back_to_dateless_search():
    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlparse(str(request.url)).query)
        if "searchevents.php" in request.url.path:
            if params.get("d"):
                return events_response()  # date-filtered search finds nothing
            return events_response(RAW_EVENT)
        return events_response()

    with make_client(handler) as client:
        event = matcher.match(GUESS, CONFIG, client=client)
    assert event is not None


def test_match_falls_back_to_eventsday():
    def handler(request: httpx.Request) -> httpx.Response:
        if "searchevents.php" in request.url.path:
            return events_response()
        if "all_leagues.php" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "leagues": [
                        {"idLeague": "5620", "strLeague": "Major League Cricket"},
                        {"idLeague": "4328", "strLeague": "English Premier League"},
                    ]
                },
            )
        if "eventsday.php" in request.url.path:
            return httpx.Response(200, json={"events": [RAW_EVENT]})
        return events_response()

    with make_client(handler) as client:
        event = matcher.match(GUESS, CONFIG, client=client)
    assert event is not None


def test_match_rejects_unverified_candidate():
    wrong = dict(RAW_EVENT, strHomeTeam="Seattle Orcas", strAwayTeam="MI New York")

    def handler(request: httpx.Request) -> httpx.Response:
        if "searchevents.php" in request.url.path:
            return events_response(wrong)
        return httpx.Response(200, json={"leagues": [], "events": []})

    with make_client(handler) as client:
        assert matcher.match(GUESS, CONFIG, client=client) is None


def test_match_requires_a_date():
    guess = dataclasses.replace(GUESS, event_date="")
    with make_client(lambda r: events_response(RAW_EVENT)) as client:
        assert matcher.match(guess, CONFIG, hint_date=None, client=client) is None


def test_match_uses_hint_date_when_guess_has_none():
    guess = dataclasses.replace(GUESS, event_date="")

    def handler(request: httpx.Request) -> httpx.Response:
        if "searchevents.php" in request.url.path:
            return events_response(RAW_EVENT)
        return events_response()

    with make_client(handler) as client:
        event = matcher.match(guess, CONFIG, hint_date=date(2026, 7, 11), client=client)
    assert event is not None


def test_match_skips_unidentified():
    with make_client(lambda r: events_response(RAW_EVENT)) as client:
        guess = GameGuess(identified=False)
        assert matcher.match(guess, CONFIG, client=client) is None


def test_match_survives_api_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with make_client(handler) as client:
        assert matcher.match(GUESS, CONFIG, client=client) is None


# --- artwork download ---------------------------------------------------------


def test_download_artwork(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8fakejpeg")

    event = matcher._to_safe_event(RAW_EVENT)
    with make_client(handler) as client:
        saved = matcher.download_artwork(event, tmp_path / "art", CONFIG, client=client)
    assert set(saved) == {"thumb", "poster"}
    assert (tmp_path / "art" / "thumb.jpg").read_bytes().startswith(b"\xff\xd8")


def test_download_artwork_partial_failure(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "poster" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"img")

    event = matcher._to_safe_event(RAW_EVENT)
    with make_client(handler) as client:
        saved = matcher.download_artwork(event, tmp_path / "art", CONFIG, client=client)
    assert set(saved) == {"thumb"}
