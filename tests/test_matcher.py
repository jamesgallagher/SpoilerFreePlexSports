import dataclasses
import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx

from sfps import matcher
from sfps.config import Config
from sfps.matcher import team_badges as real_team_badges  # bypasses conftest stub
from sfps.models import GameGuess
from sfps.thesportsdb import TheSportsDBClient

CONFIG = Config.from_env(env={"GROQ_API_KEY": "x"})

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
    # Thumb only: the library's Plex setup doesn't support poster/backdrop
    # artwork, so strPoster must not cross the firewall even though it's present.
    assert event.artwork == {"thumb": "https://img.example/event/thumb.jpg"}


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


def test_verify_ignores_league_name_mismatch():
    """League name is NOT a veto: a strong teams+date match must survive an
    LLM naming the competition differently ('International Test' vs the DB's
    'Major League Cricket'). Sport still matches, so this is accepted."""
    guess = dataclasses.replace(GUESS, league="Some Other League Name")
    assert matcher._verify(RAW_EVENT, guess, DATES)


def test_verify_rejects_sport_mismatch():
    """But a same-name, same-date clash in a DIFFERENT sport is rejected
    (rugby Australia v France must not match a soccer one)."""
    guess = dataclasses.replace(GUESS, sport="Soccer")  # RAW_EVENT is Cricket
    assert not matcher._verify(RAW_EVENT, guess, DATES)


def test_verify_allows_missing_sport():
    """If the LLM didn't determine a sport, don't veto on it."""
    guess = dataclasses.replace(GUESS, sport="")
    assert matcher._verify(RAW_EVENT, guess, DATES)


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


def test_build_queries_adds_sport_qualified_variant():
    guess = GameGuess(
        identified=True, sport="Rugby Union", home_team="Australia", away_team="France"
    )
    assert matcher._build_queries(guess) == [
        "Australia vs France",
        "Australia Rugby vs France Rugby",
    ]


def test_build_queries_no_variant_when_sport_already_present():
    guess = GameGuess(
        identified=True,
        sport="Cricket",
        home_team="Texas Super Kings",
        away_team="Washington Freedom",
    )
    # sport keyword not in club names, so a variant IS added for cricket clubs
    assert len(matcher._build_queries(guess)) == 2

    guess2 = GameGuess(
        identified=True, sport="Rugby", home_team="Australia Rugby", away_team="France Rugby"
    )
    # keyword already in both names -> no redundant variant
    assert matcher._build_queries(guess2) == ["Australia Rugby vs France Rugby"]


def test_build_queries_event_name_only():
    guess = GameGuess(identified=True, event_name="Miami Grand Prix", sport="Motorsport")
    assert matcher._build_queries(guess) == ["Miami Grand Prix"]


def test_match_sport_qualified_team_names():
    """Reproduces event 2449593: Gemini identifies 'Australia' vs 'France'
    (Rugby Union) but TheSportsDB names the teams 'Australia Rugby' /
    'France Rugby' and the event 'Australia Rugby vs France Rugby'. A bare
    'Australia vs France' query returns nothing; the sport-qualified variant
    must be tried and must find + verify the event."""
    rugby_event = {
        "idEvent": "2449593",
        "strEvent": "Australia Rugby vs France Rugby",
        "strSport": "Rugby",
        "strLeague": "Nations Championship",
        "strHomeTeam": "Australia Rugby",
        "strAwayTeam": "France Rugby",
        "dateEvent": "2026-07-11",
    }
    guess = GameGuess(
        identified=True,
        sport="Rugby Union",
        # Groq actually returned this DIFFERENT league name for the same
        # competition; it must not veto the match (only sport does).
        league="International Test",
        home_team="Australia",
        away_team="France",
        event_date="2026-07-12",  # recording start; real event is 2026-07-11 (±1 window)
        confidence=0.9,
        source="groq",
    )
    queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "searchevents.php" in request.url.path:
            q = parse_qs(urlparse(str(request.url)).query)["e"][0]
            queries.append(q)
            # only the sport-qualified event name exists on TheSportsDB
            if q == "Australia_Rugby_vs_France_Rugby":
                return events_response(rugby_event)
            return events_response()
        return events_response()

    with make_client(handler) as client:
        event = matcher.match(guess, CONFIG, client=client)

    assert event is not None, "teams+date+sport match must win despite league-name mismatch"
    assert event.event_id == "2449593"
    assert "Australia_vs_France" in queries  # plain query tried first
    assert "Australia_Rugby_vs_France_Rugby" in queries  # sport-qualified fallback


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


def test_find_league_id_rejects_wrong_sport(monkeypatch):
    """Reproduces the real bug: 'World Rugby Nations Championship' name-scored
    0.54 against 'English League Championship' (Soccer) - just over the 0.5
    threshold on shared vocabulary alone. Sport filtering must reject it."""
    leagues = [
        {"idLeague": "4329", "strLeague": "English League Championship", "strSport": "Soccer"},
    ]
    with make_client(lambda r: httpx.Response(200, json={"leagues": leagues})) as client:
        assert (
            matcher._find_league_id(client, "World Rugby Nations Championship", "Rugby Union")
            is None
        )


def test_find_league_id_accepts_matching_sport():
    leagues = [
        {"idLeague": "4986", "strLeague": "World Rugby Championship", "strSport": "Rugby"},
        {"idLeague": "4329", "strLeague": "English League Championship", "strSport": "Soccer"},
    ]
    with make_client(lambda r: httpx.Response(200, json={"leagues": leagues})) as client:
        found = matcher._find_league_id(client, "World Rugby Nations Championship", "Rugby Union")
    assert found == "4986"


def test_find_league_id_fails_open_when_sport_field_missing():
    """Defensive default: don't reject a league just because a response omits
    strSport (the real API always sets it; only test mocks might not)."""
    leagues = [{"idLeague": "1", "strLeague": "World Rugby Nations Championship"}]
    with make_client(lambda r: httpx.Response(200, json={"leagues": leagues})) as client:
        found = matcher._find_league_id(client, "World Rugby Nations Championship", "Rugby Union")
    assert found == "1"


def test_unmatched_new_tournament_does_not_chase_wrong_league():
    """End-to-end reproduction of the Australia v France log: a brand-new
    competition has zero events anywhere, and a same-worded wrong-sport league
    must not be treated as a candidate (previously wasted 3 eventsday calls
    chasing English League Championship / Soccer)."""
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
    eventsday_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "searchevents.php" in request.url.path:
            return events_response()
        if "all_leagues.php" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "leagues": [
                        {
                            "idLeague": "4329",
                            "strLeague": "English League Championship",
                            "strSport": "Soccer",
                        }
                    ]
                },
            )
        if "eventsday.php" in request.url.path:
            eventsday_calls.append(str(request.url))
            return httpx.Response(200, json={"events": []})
        raise AssertionError(f"unexpected call: {request.url}")

    with make_client(handler) as client:
        assert matcher.match(guess, CONFIG, client=client) is None
    assert eventsday_calls == []  # no league found -> step 3 skipped entirely


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


# --- team badges ---------------------------------------------------------------


def test_team_badges_found():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "searchteams.php" in request.url.path
        query = parse_qs(urlparse(str(request.url)).query)["t"][0]
        name = query.replace("_", " ")
        return httpx.Response(
            200,
            json={
                "teams": [
                    {
                        "strTeam": name,
                        "strSport": "Cricket",
                        "strBadge": f"https://img.example/{name}.png",
                    }
                ]
            },
        )

    with make_client(handler) as client:
        urls = real_team_badges(
            "Texas Super Kings", "Washington Freedom", CONFIG, sport="Cricket", client=client
        )
    assert set(urls) == {"home", "away"}
    assert "Texas Super Kings" in urls["home"]


def test_team_badges_rejects_wrong_team():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "teams": [
                    {
                        "strTeam": "Chennai Super Kings",
                        "strSport": "Cricket",
                        "strBadge": "https://x/b.png",
                    }
                ]
            },
        )

    with make_client(handler) as client:
        urls = real_team_badges(
            "Texas Super Kings", "Washington Freedom", CONFIG, sport="Cricket", client=client
        )
    # "Chennai Super Kings" vs "Washington Freedom" must not fuzzy-match
    assert "away" not in urls


def test_team_badges_empty_for_non_team_event():
    with make_client(lambda r: httpx.Response(200, json={"teams": None})) as client:
        assert real_team_badges("", "", CONFIG, sport="Cricket", client=client) == {}


def test_team_badges_falls_back_to_sport_qualified_query():
    """A bare country name ('Australia') only resolves to its Soccer entry on
    TheSportsDB; the Rugby team is indexed as 'Australia Rugby'. The first,
    unqualified query must be tried, rejected for wrong sport, and a second
    sport-qualified query attempted before giving up."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)["t"][0]
        calls.append(query)
        if query == "Australia":
            return httpx.Response(
                200,
                json={"teams": [{"strTeam": "Australia", "strSport": "Soccer", "strBadge": "https://x/soccer.png"}]},
            )
        if query == "Australia_Rugby":
            return httpx.Response(
                200,
                json={
                    "teams": [
                        {
                            "strTeam": "Australia Rugby",
                            "strSport": "Rugby",
                            "strBadge": "https://x/rugby.png",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"teams": None})

    with make_client(handler) as client:
        urls = real_team_badges("Australia", "France", CONFIG, sport="Rugby Union", client=client)
    assert calls[0] == "Australia"  # unqualified query tried first
    assert "Australia_Rugby" in calls  # sport-qualified fallback was attempted
    assert urls.get("home") == "https://x/rugby.png"  # correct sport selected, not soccer


def test_team_badges_no_fallback_when_sport_already_matches():
    """If the unqualified query already yields a sport-correct badge for both
    sides, no qualified fallback query is issued (saves API calls)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)["t"][0]
        calls.append(query)
        name = query.replace("_", " ")
        return httpx.Response(
            200,
            json={"teams": [{"strTeam": name, "strSport": "Cricket", "strBadge": f"https://x/{name}.png"}]},
        )

    with make_client(handler) as client:
        urls = real_team_badges(
            "Texas Super Kings", "Washington Freedom", CONFIG, sport="Cricket", client=client
        )
    assert calls == ["Texas_Super_Kings", "Washington_Freedom"]
    assert set(urls) == {"home", "away"}


# --- league-art fallback for teamless events ---------------------------------


# A raw league payload as lookupleague.php returns it (UCI World Tour / cycling).
RAW_LEAGUE = {
    "idLeague": "4465",
    "strLeague": "UCI World Tour",
    "strSport": "Cycling",
    "strPoster": "https://img.example/league/poster.jpg",
    "strBanner": "https://img.example/league/banner.jpg",
    "strFanart1": "https://img.example/league/fanart.jpg",
    "strBadge": "https://img.example/league/badge.png",
}

# A teamless guess: a Tour de France stage the identifier named a competition
# for but that TheSportsDB has no verifiable specific event for.
TEAMLESS_GUESS = GameGuess(
    identified=True,
    sport="Cycling",
    league="Tour de France",
    event_name="Tour de France Stage 8 Highlights",
    event_date="2026-07-12",
    round="Stage 8",
    variant="highlights",
    confidence=0.95,
    source="groq",
)


def _tour_event(**over):
    return {
        "idEvent": "9001",
        "strEvent": "Tour de France Stage 21",
        "strSport": "Cycling",
        "strLeague": "UCI World Tour",
        "idLeague": "4465",
        "dateEvent": "2026-07-26",
        **over,
    }


def _league_fallback_handler(**flags):
    """Handler serving searchevents (event->league link) then lookupleague."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "searchevents.php" in request.url.path:
            if flags.get("no_events"):
                return events_response()
            return events_response(_tour_event(**flags.get("event_over", {})))
        if "lookupleague.php" in request.url.path:
            params = parse_qs(urlparse(str(request.url)).query)
            assert params["id"][0] == "4465"
            league = flags.get("league", RAW_LEAGUE)
            return httpx.Response(200, json={"leagues": [league] if league else None})
        raise AssertionError(f"unexpected call: {request.url}")

    return handler


def test_league_artwork_urls_returns_thumb_only_preferring_fanart():
    """Only a thumb is ever produced - the library doesn't support poster/
    backdrop artwork - and fanart (16:9) is preferred over the league poster."""
    urls = matcher.league_artwork_urls(RAW_LEAGUE)
    assert urls == {"thumb": "https://img.example/league/fanart.jpg"}


def test_league_artwork_urls_thumb_falls_back_to_banner_then_poster():
    assert matcher.league_artwork_urls({"strBanner": "b", "strPoster": "p"})["thumb"] == "b"
    assert matcher.league_artwork_urls({"strPoster": "p"})["thumb"] == "p"
    assert matcher.league_artwork_urls({}) == {}


def test_league_fallback_files_teamless_event_with_league_art():
    with make_client(_league_fallback_handler()) as client:
        event = matcher.league_fallback(TEAMLESS_GUESS, CONFIG, client=client)
    assert event is not None
    assert event.event_id == ""  # league-level art, not a specific verified event
    assert event.league == "UCI World Tour"  # the DB's league, not the LLM's "Tour de France"
    assert event.name == "Tour de France Stage 8 Highlights"
    assert event.sport == "Cycling"
    assert event.event_date == "2026-07-12"
    assert event.artwork["thumb"] == "https://img.example/league/fanart.jpg"


def test_league_fallback_skips_team_games():
    """Team games keep the badge-vs-badge card path; league art is teamless-only."""
    with make_client(_league_fallback_handler()) as client:
        assert matcher.league_fallback(GUESS, CONFIG, client=client) is None


def test_league_fallback_none_when_no_league_discovered():
    with make_client(_league_fallback_handler(no_events=True)) as client:
        assert matcher.league_fallback(TEAMLESS_GUESS, CONFIG, client=client) is None


def test_league_fallback_none_when_league_has_no_art():
    handler = _league_fallback_handler(league={"idLeague": "4465", "strLeague": "UCI World Tour"})
    with make_client(handler) as client:
        assert matcher.league_fallback(TEAMLESS_GUESS, CONFIG, client=client) is None


def test_league_fallback_discovery_ignores_wrong_sport_event():
    """A same-named event in another sport must not hijack the league lookup."""
    handler = _league_fallback_handler(event_over={"strSport": "Soccer", "idLeague": "999"})
    with make_client(handler) as client:
        assert matcher.league_fallback(TEAMLESS_GUESS, CONFIG, client=client) is None


def test_league_fallback_uses_hint_date_when_guess_has_none():
    guess = dataclasses.replace(TEAMLESS_GUESS, event_date="")
    with make_client(_league_fallback_handler()) as client:
        event = matcher.league_fallback(
            guess, CONFIG, hint_date=date(2026, 7, 12), client=client
        )
    assert event is not None
    assert event.event_date == "2026-07-12"


def test_league_fallback_skips_unidentified():
    with make_client(_league_fallback_handler()) as client:
        assert matcher.league_fallback(GameGuess(identified=False), CONFIG, client=client) is None


def test_league_fallback_survives_api_outage():
    with make_client(lambda r: httpx.Response(503)) as client:
        assert matcher.league_fallback(TEAMLESS_GUESS, CONFIG, client=client) is None


# --- artwork download ---------------------------------------------------------


def test_download_artwork(tmp_path):
    """download_artwork downloads event.artwork, which only ever has a
    "thumb" key - the library's Plex setup doesn't support poster/backdrop
    artwork, so that's all the firewall (_ARTWORK_FIELDS) ever lets through."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8fakejpeg")

    event = matcher._to_safe_event(RAW_EVENT)
    with make_client(handler) as client:
        saved = matcher.download_artwork(event, tmp_path / "art", CONFIG, client=client)
    assert set(saved) == {"thumb"}
    assert (tmp_path / "art" / "thumb.jpg").read_bytes().startswith(b"\xff\xd8")


def test_download_urls_partial_failure(tmp_path):
    """download_urls (the general multi-kind downloader) tolerates one URL
    failing without losing the others. Exercised directly with a synthetic
    multi-key map, since a real SafeEvent's artwork dict only ever carries
    a single "thumb" key."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "poster" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=b"img")

    urls = {"thumb": "https://img.example/thumb.jpg", "poster": "https://img.example/poster.jpg"}
    with make_client(handler) as client:
        saved = matcher.download_urls(urls, tmp_path / "art", CONFIG, client=client)
    assert set(saved) == {"thumb"}
