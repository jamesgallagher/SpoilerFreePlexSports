"""Matcher stage: GameGuess -> verified SafeEvent (or None).

Lookup strategy (design.md §3.3):
  1. searchevents with participants + exact date, then date ± 1 day
  2. searchevents with participants only, verified against the date window
  3. eventsday for the league's schedule on each candidate date, fuzzy-matched

THE SPOILER FIREWALL LIVES HERE: `_to_safe_event` whitelists fields from the
raw API response. Scores (`intHomeScore`, `intAwayScore`), status, and result
strings exist in the raw payload and MUST NOT be copied out. Raw payloads may
only be logged at DEBUG.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from sfps.config import Config
from sfps.models import GameGuess, SafeEvent
from sfps.thesportsdb import TheSportsDBClient, TheSportsDBError

log = logging.getLogger(__name__)

# Similarity thresholds (0-1) for fuzzy verification
_TEAM_THRESHOLD = 0.7
_LEAGUE_THRESHOLD = 0.5
_SPORT_THRESHOLD = 0.4  # e.g. "Rugby Union" vs "Rugby" must pass; vs "Soccer" must not

# Which artwork fields cross the firewall, and what we call them
_ARTWORK_FIELDS = {
    "strThumb": "thumb",
    "strPoster": "poster",
    "strFanart": "fanart",
    "strBanner": "banner",
    "strSquare": "square",
}


def _normalize(name: str) -> str:
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return re.sub(r"\s+", " ", text).strip()


def _similar(a: str, b: str) -> float:
    """Fuzzy similarity with containment shortcut ("Texas" in "Texas Super Kings")."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _candidate_dates(guess: GameGuess, hint_date: date | None) -> list[date]:
    """Dates to try, best first: guess date (±1) falling back to file date (±1)."""
    anchor = _parse_date(guess.event_date) or hint_date
    if anchor is None:
        return []
    return [anchor, anchor - timedelta(days=1), anchor + timedelta(days=1)]


def _to_safe_event(raw: dict) -> SafeEvent:
    """Whitelist copy — the only way event data leaves the raw API payload."""
    artwork = {
        kind: raw[field]
        for field, kind in _ARTWORK_FIELDS.items()
        if raw.get(field)
    }
    round_raw = raw.get("intRound") or raw.get("strRound") or ""
    return SafeEvent(
        event_id=str(raw.get("idEvent") or ""),
        name=str(raw.get("strEvent") or ""),
        sport=str(raw.get("strSport") or ""),
        league=str(raw.get("strLeague") or ""),
        season=str(raw.get("strSeason") or ""),
        round=str(round_raw),
        home_team=str(raw.get("strHomeTeam") or ""),
        away_team=str(raw.get("strAwayTeam") or ""),
        event_date=str(raw.get("dateEvent") or ""),
        venue=str(raw.get("strVenue") or ""),
        artwork=artwork,
    )


def _verify(raw: dict, guess: GameGuess, dates: list[date]) -> bool:
    """A candidate must match participants AND fall inside the date window."""
    event_date = _parse_date(str(raw.get("dateEvent") or ""))
    if event_date is None or event_date not in dates:
        log.debug("verify: date %s outside window", raw.get("dateEvent"))
        return False

    if guess.home_team and guess.away_team:
        home = _similar(guess.home_team, str(raw.get("strHomeTeam") or ""))
        away = _similar(guess.away_team, str(raw.get("strAwayTeam") or ""))
        # Recorders sometimes flip home/away — accept the swapped pairing too
        home_sw = _similar(guess.home_team, str(raw.get("strAwayTeam") or ""))
        away_sw = _similar(guess.away_team, str(raw.get("strHomeTeam") or ""))
        teams_ok = (home >= _TEAM_THRESHOLD and away >= _TEAM_THRESHOLD) or (
            home_sw >= _TEAM_THRESHOLD and away_sw >= _TEAM_THRESHOLD
        )
        if not teams_ok:
            log.debug("verify: team similarity too low (%.2f/%.2f)", home, away)
            return False
    elif guess.event_name:
        if _similar(guess.event_name, str(raw.get("strEvent") or "")) < _TEAM_THRESHOLD:
            log.debug("verify: event name similarity too low")
            return False
    else:
        return False

    if guess.league and raw.get("strLeague") and (
        _similar(guess.league, str(raw.get("strLeague"))) < _LEAGUE_THRESHOLD
    ):
        log.debug("verify: league mismatch '%s' vs '%s'", guess.league, raw.get("strLeague"))
        return False
    return True


def _find_league_id(client: TheSportsDBClient, league_name: str, sport: str = "") -> str | None:
    """Best-matching league id, sport-filtered to avoid cross-sport false positives.

    League names share a lot of generic vocabulary ("Championship", "League",
    "Cup"), so name similarity alone can score a same-worded league in the
    wrong sport above threshold (e.g. "World Rugby Nations Championship" vs
    "English League Championship" scores 0.54 on name alone). Reject any
    league whose sport clearly doesn't match first. Fails open when a mocked/
    partial API response omits strSport, since the real API always sets it.
    """
    best_id, best_score = None, 0.0
    for league in client.all_leagues():
        league_sport = str(league.get("strSport") or "")
        if sport and league_sport and _similar(sport, league_sport) < _SPORT_THRESHOLD:
            continue
        score = _similar(league_name, str(league.get("strLeague") or ""))
        if score > best_score:
            best_id, best_score = str(league.get("idLeague") or ""), score
    return best_id if best_score >= _LEAGUE_THRESHOLD else None


def match(
    guess: GameGuess,
    config: Config,
    hint_date: date | None = None,
    client: TheSportsDBClient | None = None,
) -> SafeEvent | None:
    """Find and verify the event on TheSportsDB; None means Unknown Event path."""
    if not guess.identified:
        log.info("match: skipped (no identification to match)")
        return None

    dates = _candidate_dates(guess, hint_date)
    if not dates:
        # A dateless match cannot be verified; a wrong match is worse than none.
        log.info("match: no event date or file date available to verify against -> unmatched")
        return None

    queries = _build_queries(guess)
    if not queries:
        log.info("match: guess has neither teams nor event name -> unmatched")
        return None

    own_client = client is None
    if own_client:
        client = TheSportsDBClient(config)
    try:
        return _match_with_client(client, queries, guess, dates)
    except TheSportsDBError as exc:
        log.warning("match: TheSportsDB unavailable (%s) -> unmatched", exc)
        return None
    finally:
        if own_client:
            client.close()


def _build_queries(guess: GameGuess) -> list[str]:
    """Search-event query variants, most likely first.

    TheSportsDB names its event and its teams the same way, and national
    teams for non-soccer sports are sport-qualified: the Australia rugby
    side is "Australia Rugby", so the event is "Australia Rugby vs France
    Rugby" and a bare "Australia vs France" query returns nothing. When the
    identified team names omit the sport keyword, add a sport-qualified
    variant so those events are still found.
    """
    if guess.home_team and guess.away_team:
        home, away = guess.home_team, guess.away_team
        queries = [f"{home} vs {away}"]
        keyword = guess.sport.split()[0] if guess.sport else ""
        if keyword and keyword.lower() not in home.lower() and keyword.lower() not in away.lower():
            queries.append(f"{home} {keyword} vs {away} {keyword}")
        return queries
    if guess.event_name:
        return [guess.event_name]
    return []


def _match_with_client(
    client: TheSportsDBClient, queries: list[str], guess: GameGuess, dates: list[date]
) -> SafeEvent | None:
    # Step 1: participants + explicit date (exact, then ±1), each query variant
    for d in dates:
        for query in queries:
            for raw in client.search_events(query, date=d.isoformat()):
                if _verify(raw, guess, dates):
                    return _accept(raw, "search+date")

    # Step 2: participants only, verified against the window
    for query in queries:
        for raw in client.search_events(query):
            if _verify(raw, guess, dates):
                return _accept(raw, "search")

    # Step 3: league schedule on each candidate day, fuzzy-matched
    if guess.league:
        league_id = _find_league_id(client, guess.league, guess.sport)
        if league_id:
            for d in dates:
                for raw in client.events_on_day(d.isoformat(), league_id):
                    if _verify(raw, guess, dates):
                        return _accept(raw, "eventsday")

    log.info("match: no verified event found for %s -> unmatched", " / ".join(queries))
    return None


def _accept(raw: dict, via: str) -> SafeEvent:
    event = _to_safe_event(raw)
    log.info(
        "match: verified via %s -> [%s] %s (%s, %s) artwork=%s",
        via,
        event.event_id,
        event.name,
        event.league,
        event.event_date,
        ",".join(sorted(event.artwork)) or "none",
    )
    return event


def download_urls(
    urls: dict[str, str], dest_dir: Path, config: Config, client: TheSportsDBClient | None = None
) -> dict[str, Path]:
    """Download a kind->url map into dest_dir; returns kind -> file path."""
    own_client = client is None
    if own_client:
        client = TheSportsDBClient(config)
    saved: dict[str, Path] = {}
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        for kind, url in urls.items():
            suffix = Path(url.split("?")[0]).suffix or ".jpg"
            target = dest_dir / f"{kind}{suffix}"
            try:
                target.write_bytes(client.download(url))
                saved[kind] = target
                log.info("artwork: downloaded %s -> %s", kind, target.name)
            except Exception as exc:  # noqa: BLE001 - a missing image must not kill the run
                log.warning("artwork: failed to download %s (%s)", kind, exc)
    finally:
        if own_client:
            client.close()
    return saved


def download_artwork(
    event: SafeEvent, dest_dir: Path, config: Config, client: TheSportsDBClient | None = None
) -> dict[str, Path]:
    """Download the event's artwork into dest_dir; returns kind -> file path."""
    return download_urls(event.artwork, dest_dir, config, client=client)


def _search_team_badge(client: TheSportsDBClient, team: str, sport: str) -> str | None:
    """Find one team's badge URL, sport-filtered, with a qualified-name fallback.

    A bare country name (e.g. "Australia") often only resolves to that
    country's Soccer team on TheSportsDB — national teams for other sports
    are indexed under a qualified name ("Australia Rugby", "France Rugby").
    Try the plain name first, then "<team> <sport keyword>" if that yields
    nothing sport-matching.
    """
    queries = [team]
    if sport:
        keyword = sport.split()[0]
        if keyword.lower() not in team.lower():
            queries.append(f"{team} {keyword}")

    for query in queries:
        try:
            candidates = client.search_teams(query)
        except TheSportsDBError as exc:
            log.warning("badges: search failed for '%s' (%s)", query, exc)
            continue
        best, best_score = None, 0.0
        for raw in candidates:
            badge = raw.get("strBadge")
            if not badge:
                continue
            name_score = _similar(team, str(raw.get("strTeam") or ""))
            if name_score < _TEAM_THRESHOLD:
                continue
            team_sport = str(raw.get("strSport") or "")
            if sport and team_sport and _similar(sport, team_sport) < _SPORT_THRESHOLD:
                continue  # e.g. reject a Soccer badge when the game is Rugby
            score = name_score + (1.0 if not sport else _similar(sport, team_sport))
            if score > best_score:
                best, best_score = str(badge), score
        if best:
            return best
    return None


def team_badges(
    home_team: str,
    away_team: str,
    config: Config,
    sport: str = "",
    client: TheSportsDBClient | None = None,
) -> dict[str, str]:
    """Badge URLs for two teams ({"home": url, "away": url}) when found, sport-filtered."""
    urls: dict[str, str] = {}
    if not (home_team and away_team):
        return urls
    own_client = client is None
    if own_client:
        client = TheSportsDBClient(config)
    try:
        for side, team in (("home", home_team), ("away", away_team)):
            badge = _search_team_badge(client, team, sport)
            if badge:
                urls[side] = badge
    finally:
        if own_client:
            client.close()
    return urls


def to_safe_event(raw: dict) -> SafeEvent:
    """Public spoiler-firewall crossing for raw event payloads (retry/review)."""
    return _to_safe_event(raw)
