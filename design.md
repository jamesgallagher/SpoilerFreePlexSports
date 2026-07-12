# SpoilerFreePlexSports — Design Document

**Status:** Design phase — nothing built yet.
**Repo:** https://github.com/jamesgallagher/SpoilerFreePlexSports
**Last updated:** 2026-07-12

---

## 1. Problem Statement

Plex automatically generates episode thumbnails by grabbing a frame from the video.
For recorded sports, that frame very often contains the **score bug / final score
graphic** — instantly spoiling the game before you press play.

This project is an external companion service ("plugin" in spirit — Plex removed its
real plugin system in 2018, so this runs alongside Plex, not inside it) that watches
for new sports recordings and gets to them **before Plex does**, giving every game:

- A **spoiler-free poster/thumbnail** (downloaded pre-match artwork, or a generated
  neutral placeholder)
- A **background** (fanart) where available
- A **metadata sidecar** describing the game — with **no result information**
- Its own tidy directory, named so Plex's TV agent and Local Media Assets pick
  everything up on the next scan

### Design Philosophy: Spoiler-Free First

Every decision is filtered through one rule: **nothing this tool writes to disk or to
Plex may reveal the outcome of a game.** Concretely:

1. **Never persist scores.** TheSportsDB event responses include `intHomeScore` /
   `intAwayScore` and post-match status fields. These are dropped at the API-client
   boundary — they never reach the sidecar, filenames, logs at INFO level, or Plex.
2. **Prefer pre-match-style artwork.** Event artwork on TheSportsDB is fan-made and
   occasionally themed on the result. A config flag (`ARTWORK_MODE=generate`) forces
   locally generated neutral art (team names / logos + date) instead of downloaded
   event art for users who want zero risk.
3. **Descriptions are pre-match only.** Sidecar/summary text is limited to league,
   round/week, venue, date, teams. No "recap"-style strings from the API.
4. **Beat Plex's frame-grab.** Supplying a local thumb (`<filename>.jpg`) makes Plex
   use it as the episode poster instead of a generated frame. The deployment guide
   will also document turning **off** "Video preview thumbnails" for the sports
   library, since those chapter-strip images are generated from the video itself and
   can still leak scores on the seek bar.

---

## 2. High-Level Architecture

```
                        ┌──────────────────────────────────────────────┐
                        │        SpoilerFreePlexSports (Docker)        │
                        │                                              │
 new recording lands    │  ┌─────────┐   ┌──────────────┐              │
 ───────────────────▶   │  │ Watcher │──▶│ Identifier   │── Gemini API │
   /watch (volume)      │  │(watchdog│   │ (filename +  │   (LLM parse)│
                        │  │ +ledger)│   │  timestamp)  │              │
                        │  └─────────┘   └──────┬───────┘              │
                        │                       ▼                      │
                        │                ┌──────────────┐              │
                        │                │ Matcher      │──TheSportsDB │
                        │                │ (event +     │   API        │
                        │                │  artwork)    │              │
                        │                └──────┬───────┘              │
                        │                       ▼                      │
                        │  ┌───────────────────────────────────┐       │
                        │  │ Organizer                         │       │
                        │  │ • create game directory           │       │
                        │  │ • move media file                 │       │
                        │  │ • write thumb / poster / bg       │       │
                        │  │ • write spoiler-free sidecar      │       │
                        │  │ • fallback: "Unknown Event" thumb │       │
                        │  └───────────────┬───────────────────┘       │
                        └──────────────────┼───────────────────────────┘
                                           ▼
                                   /library (volume)  ──▶  Plex scans it
```

### Pipeline stages

| Stage | Input | Output | External dependency |
|---|---|---|---|
| **Watcher** | New file in `/watch` | Stable, ready-to-process file path | — |
| **Identifier** | Filename + file timestamp | Structured game guess (JSON) | Gemini API |
| **Matcher** | Game guess | Confirmed event + artwork URLs | TheSportsDB API |
| **Organizer** | File + event (or "unknown") | Organized folder in `/library` | — |
| **(Optional) Notifier/Rescan** | Organized folder | Plex partial scan | Plex HTTP API |

Each stage is a plain Python module with a narrow interface, so every stage can be
run and tested standalone via a CLI before the daemon wires them together.

---

## 3. Stage Design

### 3.1 Watcher

- Python `watchdog` observer on `/watch` (recursive — subfolders are traversed by
  both the observer and the sweep).
- **File-stability gate:** sports recordings are written over hours. A file is only
  "ready" when its size has been unchanged for `STABILITY_SECONDS` (default 120) and
  it is openable — a changing filesize means an ongoing recording and the file is
  left alone.
- **Main-video-only filter:** only `.mp4`, `.mkv`, `.avi`, `.mov`, `.mpeg`, `.ts`
  are processed (configurable `MEDIA_EXTENSIONS`); everything else is ignored.
- **Hidden files are ignored:** any file or directory whose name starts with `.`
  (recorder temp files, macOS `._*` AppleDouble) plus known junk dirs (`@eaDir`)
  are skipped during both event handling and sweeps.
- **Ledger:** a small SQLite DB in `/config` records every file processed (by path +
  size + mtime hash) with its outcome (`matched`, `unknown`, `error`). Prevents
  reprocessing on restart; also powers the retry queue (§3.5).
- **Startup sweep:** on boot, scan `/watch` for pre-existing unprocessed files so
  nothing is missed while the container was down.
- Watching a **staging folder** (not the live Plex library) is the recommended
  deployment: Plex never sees the raw file, so there is no race with Plex indexing a
  file we are about to move/rename, and no window where a frame-grab thumb exists.

### 3.2 Identifier (LLM: Groq default, Gemini optional)

Recorded-sports filenames are too messy for regex alone
(`EPL.Arsenal.v.Chelsea.12.07.26.HDTV.ts`, `NFL RedZone Week 5.ts`,
`Fox Sports 505 - 2026-07-12 19-30.ts`…). An LLM call is cheap, fast, and handles the
long tail.

- **Provider:** `LLM_PROVIDER` selects `groq` (default) or `gemini`. Groq is
  OpenAI-compatible and called via httpx (no extra SDK); Gemini uses the
  google-genai SDK (installed only with the `gemini` image extra). The wrapper
  in `llm.py` exposes one `generate_json()` so nothing downstream knows the
  provider. Groq is the default for speed/stability and generous free daily
  limits; the identify source label records which provider produced a guess.
- **Input:** filename, parent folder name(s), file mtime (as a hint for the event
  date), configured timezone.
- **Model:** `GROQ_MODEL` (default `openai/gpt-oss-120b`, strict JSON schema) or
  `GEMINI_MODEL`; structured-output / JSON mode with a fixed schema. Groq strict
  mode requires every property in `required` + `additionalProperties:false`,
  which `llm.py` derives from the base schema automatically.
- **Output schema:**

```json
{
  "identified": true,
  "sport": "Soccer",
  "league": "English Premier League",
  "home_team": "Arsenal",
  "away_team": "Chelsea",
  "event_date": "2026-07-12",
  "round": "Matchweek 3",
  "confidence": 0.92,
  "notes": "date taken from filename, not mtime"
}
```

- **Rules embedded in the prompt:** never guess a result; date reasoning must
  account for timezone and overnight recordings (event date may be file date ± 1);
  return `identified: false` with best-effort fields rather than hallucinating teams.
- **Confidence threshold:** below `MIN_CONFIDENCE` (default 0.6) the file goes down
  the Unknown Event path rather than risking a wrong match (wrong artwork on the
  wrong game is worse than a placeholder).
- A **regex pre-pass** handles trivially parseable names without an API call
  (cost/latency saving), falling back to Gemini when it doesn't match.

### 3.3 Matcher (TheSportsDB)

- v1 API, key from `THESPORTSDB_API_KEY` (free test key `123` works for development;
  premium $9/mo raises rate limits from ~30 to 100 req/min and search results from
  1 to 10).
- **Lookup strategy (in order):**
  1. `searchevents.php?e={Home}_vs_{Away}&d={date}` — teams + exact date
  2. Same query with date ± 1 day (timezone/overnight boundary)
  3. `eventsday.php?d={date}&l={leagueId}` then fuzzy-match team names within the
     day's schedule (catches API name variants: "Man United" vs "Manchester United")
- **Verification:** a candidate event must match on both team names (fuzzy,
  normalized) **and** date window before it's accepted. League mismatch downgrades
  to unknown.
- **Artwork harvested per event:** `strThumb` (→ episode thumb), `strPoster`
  (→ poster), `strFanart` (→ background), `strBanner`, `strSquare` (kept in sidecar
  URLs but not required). Team badges (`strBadge`) and league art are fetched as
  fallbacks for generated artwork.
- **Spoiler firewall:** the API client returns a `SafeEvent` dataclass that simply
  has no score/result fields. Raw responses are never passed downstream or logged
  above DEBUG.

### 3.4 Organizer

Target layout (Plex **TV Shows** library, one "show" per league, date-based episodes,
Local Media Assets enabled):

```
/library/
  English Premier League/
    poster.jpg                      # league poster (from TheSportsDB league art)
    background.jpg                  # league fanart
    Season 2026/
      Arsenal vs Chelsea 2026-07-12/
        English Premier League - 2026-07-12 - Arsenal vs Chelsea.mkv
        English Premier League - 2026-07-12 - Arsenal vs Chelsea.jpg   # episode thumb (spoiler-free)
        English Premier League - 2026-07-12 - Arsenal vs Chelsea.nfo   # Kodi episode NFO (Plex NFO agent)
        poster.jpg                  # game poster (if available)
        background.jpg              # game background (if available)
        game.json                   # spoiler-free metadata sidecar
```

- **Metadata enrichment (matched events only):** `metadata.py` builds a
  spoiler-free summary from the available TheSportsDB fields (sport, league,
  round, participants, venue/city/country, date) and writes a Kodi
  `episodedetails` `.nfo` (`<title>`=strEvent, `<plot>`=summary, `<aired>`) next
  to the video plus a `tvshow.nfo` at the league folder. Plex's built-in NFO
  agent (PMS 1.43.1+) reads these onto the card; with the plain TV agent the
  files are harmlessly ignored (artwork still applies via Local Media Assets).
  The same fields + `summary` + `title` are added to `game.json`. Everything is
  built from `SafeEvent`, so no score can leak. (Resolves the §6 open question
  about pushing summaries into Plex — done via NFO, no API/credentials needed.)

- Filenames follow Plex's date-based episode convention
  (`Show - YYYY-MM-DD - Title.ext`) so the stock Plex TV agent orders them
  correctly without episode numbers.
- **Move, not copy** (same volume ⇒ atomic rename; cross-device fallback:
  copy + verify size + delete source). Setting `PRESERVE_ORIGINAL=true` switches
  to copy-only: the original stays in `/watch` (the ledger fingerprint prevents it
  being processed again; managing the accumulating originals is the user's call).
- **Sidecar `game.json`** (machine-readable; a human-readable `game.txt` twin is a
  cheap add-on):

```json
{
  "matched": true,
  "sport": "Soccer",
  "league": "English Premier League",
  "season": "2026-2027",
  "round": "Matchweek 3",
  "home_team": "Arsenal",
  "away_team": "Chelsea",
  "event_date": "2026-07-12",
  "venue": "Emirates Stadium",
  "thesportsdb_event_id": "1032723",
  "identifier": {"source": "gemini", "confidence": 0.92},
  "variant": "full",
  "artwork": {"thumb": "downloaded", "poster": "downloaded", "background": "none"},
  "processed_at": "2026-07-12T21:14:03+10:00",
  "spoiler_free": true
}
```

  *(Note what is absent: scores, winner, status, highlights links.)*

- **Teamless league-art fallback:** races, tours and individual events (Tour de
  France, a Grand Prix) identify to a *competition* but often have no verifiable
  per-stage event on TheSportsDB. Rather than dropping these to Unknown, the
  matcher (`league_fallback`) discovers the competition's league by searching
  events by the competition name and reading the winning event's `idLeague`
  (the LLM's viewer-facing name — "Tour de France" — rarely matches the DB's
  broader league — "UCI World Tour" — so an event→league link is used, not a
  league-name match), then looks that league up and uses its **poster / banner /
  fanart** as the recording's artwork. Competition branding is generic and
  structurally cannot reveal a result, so this is spoiler-safe; the item is
  filed under the competition (`match_level: "league"` in the sidecar, empty
  `thesportsdb_event_id`). Team games are left to the badge-vs-badge card — a
  real matchup card beats a league poster there.

- **Unknown Event path:** if identification or matching fails, the file still gets
  organized — into `/library/Unknown Events/<original name>/` — with a supplied
  `unknown-event.jpg` placeholder as the thumb (a temporary generated one — plain
  dark card with "Unknown Event" text via Pillow — until the real asset is supplied).
  The ledger marks it `unknown` for later retry/manual fix. **The placeholder is
  itself a spoiler-free win**: it still pre-empts Plex's frame-grab.

### 3.5 Content variants: Highlights & Mini

A recording is often not the full game. Filename tokens mark the variant:

| Tokens (case-insensitive, word-boundary) | Variant | Meaning |
|---|---|---|
| `HL`, `HLS`, `Highlights` | `highlights` | Highlights package |
| `Mini` | `mini` | Condensed / mini match |
| *(none)* | `full` | Full game (default) |

Handling across the pipeline:

- **Detection** is a deterministic regex pre-pass in the identifier (not left to
  Gemini), run on the raw filename. The variant is carried on `GameGuess` and
  through to the sidecar.
- **Matching:** variant tokens are stripped before event lookup — a highlights
  package matches the *same* TheSportsDB event as the full game.
- **Artwork:** the variant uses the main game's thumb and poster, with a small
  **badge composited onto the thumb** (Pillow, bottom-right corner): a
  "HIGHLIGHTS" badge or a "MINI" badge. Badge assets live in `/config/badges/`
  (`highlights.png`, `mini.png`) and are user-replaceable; temporary generated
  badges (text-on-pill) are used until supplied — same approach as the
  Unknown Event placeholder.
- **Naming:** the variant is appended to the game folder and episode title —
  `Arsenal vs Chelsea 2026-07-12 (Highlights)` — so a full game and its
  highlights package of the **same event** coexist without collision, and the
  variant is visible in Plex without opening the item.
- **Sidecar:** `variant` field is `full` | `highlights` | `mini`; the
  human-readable description says "Highlights package" / "Condensed match"
  (still spoiler-free — never e.g. "all 5 goals").

### 3.6 Retry & review (hardening phase)

- TheSportsDB is crowd-sourced; an event sometimes appears/gets artwork days after
  airing. A scheduled retry pass re-attempts `unknown` and `artwork-incomplete`
  ledger entries for `RETRY_DAYS` (default 7).
- A tiny `review` CLI lists unknowns and lets you force a match by event ID
  (`sfps review --set-event 1032723 <file>`), re-running the organizer.

### 3.7 Optional: Plex partial rescan

Deliberately **optional** (not in the core "job done" path): when `PLEX_URL` +
`PLEX_TOKEN` are set, after organizing, call
`section.update(path=<new game folder>)` via `python-plexapi` so the game appears in
Plex within seconds instead of at the next scheduled scan. Without these env vars the
step is skipped silently.

---

## 4. Deployment (Docker)

**Target platform: unRAID.** The image is published to GHCR as
`ghcr.io/jamesgallagher/spoilerfreeplexsports:latest` on **every commit to main**
(CI `publish` job; linux/amd64 + arm64). No version tags / pinning — unRAID pulls
the rolling `latest`. An unRAID Community Applications template XML is a Phase 6
deliverable.

### Image

- Base: `python:3.12-slim`; deps: `watchdog`, `httpx`, `google-genai`, `Pillow`,
  `plexapi` (optional extra). No ffmpeg needed (no transcoding/remuxing in scope).
- Runs as non-root; `PUID`/`PGID` env vars (linuxserver.io convention) so moved
  files carry ownership Plex can read.

### docker-compose sketch

```yaml
services:
  spoilerfreeplexsports:
    image: ghcr.io/jamesgallagher/spoilerfreeplexsports:latest
    environment:
      - GEMINI_API_KEY=${GEMINI_API_KEY}
      - GEMINI_MODEL=gemini-flash-latest
      - THESPORTSDB_API_KEY=${THESPORTSDB_API_KEY}
      - TZ=Australia/Sydney
      - MIN_CONFIDENCE=0.6
      - STABILITY_SECONDS=120
      - ARTWORK_MODE=download        # download | generate
      # optional Plex rescan:
      # - PLEX_URL=http://plex:32400
      # - PLEX_TOKEN=${PLEX_TOKEN}
    volumes:
      - /path/to/staging:/watch      # where recordings land (any DVR/recorder output)
      - /path/to/sports-library:/library   # the Plex sports library root
      - ./config:/config             # ledger DB, unknown-event.jpg, overrides
    restart: unless-stopped
```

### Configuration summary (all env vars)

| Var | Required | Default | Purpose |
|---|---|---|---|
| `LLM_PROVIDER` | no | `groq` | `groq` or `gemini` |
| `GROQ_API_KEY` | yes (groq) | — | Game identification (default provider) |
| `GROQ_MODEL` | no | `openai/gpt-oss-120b` | Groq model (strict JSON schema) |
| `GEMINI_API_KEY` | yes (gemini) | — | Only when `LLM_PROVIDER=gemini` |
| `GEMINI_MODEL` | no | flash-class default | Gemini model selection |
| `THESPORTSDB_API_KEY` | yes | `123` (dev) | Metadata + artwork |
| `TZ` | yes | UTC | Date reasoning for overnight games |
| `MIN_CONFIDENCE` | no | `0.6` | Below ⇒ Unknown Event path |
| `STABILITY_SECONDS` | no | `120` | File-finished detection |
| `MEDIA_EXTENSIONS` | no | `.mp4,.mkv,.avi,.mov,.mpeg,.ts` | Files to process (all else ignored) |
| `PRESERVE_ORIGINAL` | no | `false` | `true` = copy into library, leave original in `/watch` |
| `ARTWORK_MODE` | no | `download` | `generate` = never use downloaded event art |
| `RETRY_DAYS` | no | `7` | Re-attempt unknowns/missing art |
| `PLEX_URL` / `PLEX_TOKEN` | no | — | Enables optional partial rescan |
| `PLEX_LIBRARY_PATH` | no | — | Plex-side path of the library when mounted differently |
| `PUID` / `PGID` | no | `1000` | File ownership |
| `DRY_RUN` | no | `false` | Log actions without moving/writing |
| `LOG_LEVEL` | no | `INFO` | DEBUG may include raw API payloads |

---

## 5. Build Plan — Phased Breakdown

Each phase is independently testable and committable; the daemon only exists from
Phase 5, everything before it is CLI-driven so it's easy to iterate on real
filenames.

### Phase 1 — Skeleton & config
Repo layout (`sfps/` package), config loader (env vars + defaults), logging setup,
`Dockerfile`, CI stub (lint + tests), `sfps process <file> --dry-run` CLI entrypoint
that walks the pipeline with stubbed stages.
**Done when:** container builds and dry-runs a fake file end-to-end with stub output.

### Phase 2 — Identifier
Gemini client with JSON-schema structured output, prompt with date/timezone rules,
regex pre-pass, confidence gating. Fixture suite of ~30 real recorded-sports
filenames (collected from real recorder output) with expected parses.
**Done when:** fixture suite passes; `sfps identify "<filename>"` prints the JSON guess.

### Phase 3 — Matcher
TheSportsDB client (rate-limited, retrying), 3-step lookup strategy, fuzzy team-name
verification, `SafeEvent` spoiler firewall, artwork URL harvesting + download.
**Done when:** `sfps match` takes an identifier JSON and returns a verified event +
downloaded artwork to a temp dir; unit tests cover date-boundary and name-variant cases.

### Phase 4 — Organizer
Folder/naming builder, atomic move with cross-device fallback, Local Media Assets
artwork placement, `game.json` sidecar, Unknown Event path with generated temp
placeholder (Pillow). Content-variant handling (§3.5): HL/HLS/Highlights/Mini
detection, variant token stripping before match, `(Highlights)`/`(Mini)` naming,
badge compositing onto the thumb with user-replaceable badge assets.
**Done when:** `sfps process <file>` fully organizes a real recording into a test
library and a manual Plex scan shows the game with the supplied thumb, no frame-grab.

### Phase 5 — Watcher daemon
`watchdog` observer, stability gate, SQLite ledger, startup sweep, graceful shutdown.
Daemon becomes the container's default command.
**Done when:** dropping a file into `/watch` while the container runs produces an
organized game with zero manual steps.

### Phase 6 — Docker & deployment polish
Compose file, PUID/PGID handling, healthcheck, unRAID Community Applications
template XML, deployment guide **including the Plex library settings checklist**
(Local Media Assets on, video preview thumbnails off, date-based TV library pointed
at `/library`). *(GHCR image publishing was pulled forward and already ships on
every commit to main.)*
**Done when:** clean unRAID deploy from the template + README alone works.

### Phase 6.5 — Intake filters & preserve mode *(added 2026-07-12)*
Feasibility-checked against the Phase 5 implementation; all sane, two of the five
requested behaviours already shipped (stability gate = changing-filesize monitor;
recursive traversal of subfolders — both covered by existing tests).
Remaining work:
1. Default `MEDIA_EXTENSIONS` becomes `.mp4,.mkv,.avi,.mov,.mpeg,.ts`.
2. Hidden-file rule: skip dot-prefixed files/dirs and `@eaDir` in observer + sweep.
3. `PRESERVE_ORIGINAL` toggle (default `false` = move): copy-only mode leaving the
   original in `/watch`; ledger prevents reprocessing; surface in compose +
   unRAID template.
**Done when:** tests cover all three; a preserved original survives a sweep without
being reprocessed.

### Phase 7 — Hardening & quality of life
Retry pass for unknowns/missing artwork, `sfps review` manual-match CLI, generated
team-badge artwork for matched-but-artless games (`ARTWORK_MODE=generate` fully
realized), optional Plex partial rescan, optional notification hook.
**Done when:** a week of real recordings processes with unknowns recoverable without
touching the filesystem by hand.

---

## 6. Risks & Open Questions

| # | Risk / question | Current position |
|---|---|---|
| 1 | **Wrong match = wrong artwork on wrong game.** | Confidence gate + team+date verification; below threshold ⇒ Unknown placeholder (safe failure). |
| 2 | **TheSportsDB per-game artwork is sparse** outside big leagues. | Fall back league/team art → generated badge composite (Phase 7) → placeholder. Teamless events (races/tours) with no verifiable event fall back to competition-level league art (`league_fallback`) before Unknown. Thumb is the must-have; poster/background best-effort. |
| 3 | **Downloaded event art could itself contain a result.** | Rare but real; `ARTWORK_MODE=generate` is the zero-risk escape hatch. |
| 4 | **Plex may still frame-grab** if it scans before art exists. | Staging-folder deployment means art always lands with (before) the media file. |
| 5 | **Overnight/timezone date shifts.** | `TZ` env + ±1-day search window + Gemini prompt rules; fixture-tested. |
| 6 | **Gemini cost/latency.** | Regex pre-pass; flash-class model; one call per file (files arrive at DVR pace, not bulk). |
| 7 | Which leagues/sports does the fixture set need to cover first? | **Open — need James's actual recording filename samples.** |
| 8 | Final "Unknown Event" artwork asset. | **Open — to be supplied; temp Pillow-generated card until then.** |
| 9 | Should sidecar summary also be pushed into Plex item metadata (via API)? | Deferred to Phase 7 consideration; Local Media Assets covers the visuals. |

---

## 7. References

- Plex plugin system retirement (why this is a companion service): https://techcrunch.com/2018/09/26/plex-kills-off-support-for-cloud-sync-plugins-and-bookmarking-features/
- Plex Local Media Assets (TV): https://support.plex.tv/articles/200220717-local-media-assets-tv-shows/
- TheSportsDB API docs: https://www.thesportsdb.com/documentation
- Plex partial-scan API: https://www.plexopedia.com/plex-media-server/api/library/scan-partial/
- python-plexapi library docs: https://python-plexapi.readthedocs.io/en/latest/modules/library.html
- Prior art studied: PDST (https://github.com/Veritas1000/pdst), PlexF1MediaScanner (https://github.com/potchin/PlexF1MediaScanner)
