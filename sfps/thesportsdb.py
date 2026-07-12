"""TheSportsDB v1 API client.

Raw API responses (which contain scores) never leave this module + matcher
pair above DEBUG log level. Rate-limited to stay inside the free tier
(~30 req/min) and retries transient failures.
"""

from __future__ import annotations

import logging
import time

import httpx

from sfps.config import Config

log = logging.getLogger(__name__)

BASE_URL = "https://www.thesportsdb.com/api/v1/json"

_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3


class TheSportsDBError(Exception):
    """The API is unreachable or persistently failing."""


class TheSportsDBClient:
    """Minimal client for the endpoints the matcher needs."""

    def __init__(
        self,
        config: Config,
        transport: httpx.BaseTransport | None = None,
        min_interval: float | None = None,
    ) -> None:
        self._key = config.thesportsdb_api_key
        # Free key: 30 req/min -> stay just above 2s between calls.
        if min_interval is None:
            min_interval = 2.1 if self._key == "123" else 0.7
        self._min_interval = min_interval
        self._last_request = 0.0
        self._client = httpx.Client(
            base_url=f"{BASE_URL}/{self._key}",
            timeout=20.0,
            transport=transport,
            headers={"User-Agent": "SpoilerFreePlexSports"},
        )
        # Separate client for artwork downloads (arbitrary hosts, no base_url)
        self._dl_client = httpx.Client(
            timeout=30.0,
            transport=transport,
            follow_redirects=True,
            headers={"User-Agent": "SpoilerFreePlexSports"},
        )
        self._leagues_cache: list[dict] | None = None

    def close(self) -> None:
        self._client.close()
        self._dl_client.close()

    def __enter__(self) -> TheSportsDBClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- plumbing -------------------------------------------------------

    def _get(self, endpoint: str, params: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self._client.get(endpoint, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("thesportsdb: %s failed (%s), attempt %d", endpoint, exc, attempt)
                continue
            if response.status_code in _RETRY_STATUS:
                last_error = TheSportsDBError(f"HTTP {response.status_code}")
                log.warning(
                    "thesportsdb: %s -> HTTP %d, attempt %d",
                    endpoint,
                    response.status_code,
                    attempt,
                )
                time.sleep(attempt)  # crude backoff on top of the rate limit
                continue
            if response.status_code != 200:
                raise TheSportsDBError(f"{endpoint} -> HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                raise TheSportsDBError(f"{endpoint} returned invalid JSON") from exc
        raise TheSportsDBError(f"{endpoint} failed after {_MAX_ATTEMPTS} attempts: {last_error}")

    # -- endpoints ------------------------------------------------------

    def search_events(
        self, query: str, date: str | None = None, season: str | None = None
    ) -> list[dict]:
        """searchevents.php — query like "Arsenal vs Chelsea" or an event name."""
        params: dict = {"e": query.replace(" ", "_")}
        if date:
            params["d"] = date
        if season:
            params["s"] = season
        data = self._get("/searchevents.php", params)
        return data.get("event") or []

    def events_on_day(self, date: str, league_id: str) -> list[dict]:
        """eventsday.php — full schedule for a league on a date."""
        data = self._get("/eventsday.php", {"d": date, "l": league_id})
        return data.get("events") or []

    def lookup_event(self, event_id: str) -> dict | None:
        """lookupevent.php — one event by id (used by retry/review)."""
        data = self._get("/lookupevent.php", {"id": event_id})
        events = data.get("events") or []
        return events[0] if events else None

    def search_teams(self, name: str) -> list[dict]:
        """searchteams.php — teams by name (badge lookup for generated art)."""
        data = self._get("/searchteams.php", {"t": name.replace(" ", "_")})
        return data.get("teams") or []

    def all_leagues(self) -> list[dict]:
        """all_leagues.php — every league (id, name, sport). Cached per client."""
        if self._leagues_cache is None:
            data = self._get("/all_leagues.php", {})
            self._leagues_cache = data.get("leagues") or []
        return self._leagues_cache

    def download(self, url: str) -> bytes:
        """Fetch an artwork URL (no API key involved)."""
        response = self._dl_client.get(url)
        response.raise_for_status()
        return response.content
