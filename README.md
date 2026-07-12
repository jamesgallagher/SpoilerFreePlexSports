# SpoilerFreePlexSports

**Plex keeps spoiling your recorded games: its auto-generated thumbnails grab a
frame from the video — usually one with the score on it.** This service fixes
that.

It watches a staging folder for new sports recordings, identifies the game
(an LLM reads the filename — Groq by default, Gemini optional — and TheSportsDB
confirms the event), then moves each game into a tidy `League/Season/Game`
folder with **spoiler-free artwork and metadata** — before Plex ever sees the
file.

```
/watch/EPL Arsenal v Chelsea HL.ts        (recorder drops a file)
        │
        ▼  identify (Gemini) → match (TheSportsDB) → organize
        │
/library/English Premier League/
  Season 2026/
    Arsenal vs Chelsea 2026-07-12 (Highlights)/
      English Premier League - 2026-07-12 - Arsenal vs Chelsea (Highlights).ts
      English Premier League - 2026-07-12 - Arsenal vs Chelsea (Highlights).jpg   ← pre-match art + HIGHLIGHTS badge
      poster.jpg / background.jpg
      game.json                                                                    ← metadata, no scores, ever
```

**Nothing this tool writes can reveal a result.** Scores are stripped from API
responses at the client boundary, sidecar metadata is pre-match only, and
unmatched recordings still get a neutral "Unknown Event" placeholder — which
still beats Plex's frame-grab.

---

## Quick start — unRAID

unRAID's "Template repositories" field for third-party template URLs has been
deprecated in favor of Community Applications, so the reliable way to install
a personal-project image like this one is to **add the container manually** —
no template needed.

1. Make sure the GHCR image is public: it must be pullable without
   credentials (see [Publishing](#publishing) below if you maintain a fork).
2. **Docker tab → Add Container**.
3. Toggle **Advanced View** (top right) and fill in:

   | Field | Value |
   |---|---|
   | **Name** | `SpoilerFreePlexSports` |
   | **Repository** | `ghcr.io/jamesgallagher/spoilerfreeplexsports:latest` |
   | **Registry URL** | `https://ghcr.io/jamesgallagher/spoilerfreeplexsports` |
   | **Icon URL** | `https://raw.githubusercontent.com/jamesgallagher/SpoilerFreePlexSports/main/assets/icon.png` |
   | **WebUI** | *(leave blank — this is a headless daemon, no web interface)* |
   | **Network Type** | `bridge` (no ports are used; networking doesn't matter) |

4. **Add Path, Port, Variable...** three times for the volumes:

   | Config Type | Name | Container Path | Host Path |
   |---|---|---|---|
   | Path | Watch folder | `/watch` | your staging/recording folder — files are *moved out* once processed; don't point this at the library itself |
   | Path | Library folder | `/library` | the share your Plex sports library reads from |
   | Path | App data | `/config` | e.g. `/mnt/user/appdata/spoilerfreeplexsports` — ledger DB, heartbeat, custom badges/placeholder art |

5. **Add Path, Port, Variable...** again for each variable:

   | Config Type | Name | Key | Value | Notes |
   |---|---|---|---|---|
   | Variable | Groq API key | `GROQ_API_KEY` | *your key* | **required** — free key at https://console.groq.com/keys |
   | Variable | TheSportsDB API key | `THESPORTSDB_API_KEY` | `123` | free/dev key; a $9/mo premium key raises rate limits |
   | Variable | Timezone | `TZ` | e.g. `Australia/Sydney` | matters for overnight games crossing a date boundary |
   | Variable | PUID | `PUID` | `99` | unRAID's `nobody` |
   | Variable | PGID | `PGID` | `100` | unRAID's `users` |

   The rest of the [configuration table](#configuration) below is optional —
   add any of those the same way if you want to override a default.

6. **Apply**. The container starts the watcher daemon and is health-checked
   via a heartbeat (`docker ps` will show it as `healthy` once running).

Files are created as `PUID:PGID` (99:100 above = unRAID's `nobody:users`).

<a name="publishing"></a>
> **Maintainer note:** the image is built and published to GHCR automatically
> by CI on every push to `main`. If you fork this repo, make your package
> public once: GitHub → your profile → **Packages** →
> `spoilerfreeplexsports` → Package settings → Change visibility → Public —
> otherwise unRAID's pull will fail with 401 Unauthorized.

A ready-made [unRAID template XML](templates/spoilerfreeplexsports.xml) also
lives in this repo (used if `Community Applications` ever indexes this
project, or if you self-host a template repository) — the manual steps above
give the exact same result.

## Quick start — docker compose

```bash
curl -O https://raw.githubusercontent.com/jamesgallagher/SpoilerFreePlexSports/main/docker-compose.yml
# edit the two volume paths, then:
GROQ_API_KEY=your-key docker compose up -d
```

## Setting up the Plex library (important!)

Create a **TV Shows** library pointed at your library folder. For each matched
game the service writes three things: local artwork, a Kodi-style `.nfo`
(title, description, date), and its own `game.json` record. How much of that
Plex shows depends on the library's **Agent**:

| Setting | Value | Why |
|---|---|---|
| **Agent** | **Plex NFO** (recommended) | Reads the `.nfo` so the card shows the enriched title + description (e.g. *"Rugby Nations Championship (Round 2): Australia Rugby vs France Rugby at Suncorp Stadium, Brisbane on 11 July 2026."*). Requires **PMS 1.43.1+**. Falls back to **Plex TV Series** if you only want artwork. |
| **Local Media Assets** | **Enabled**, ordered **first** (Settings → Agents) | This is how the spoiler-free thumbs/posters get used |
| **Video preview thumbnails** | **Disabled** | Plex generates these from the video — they leak scores on the seek bar |
| Seasons | Show | One season per year per league |

Leave "Generate chapter thumbnails" off for the same reason. With the **Plex
NFO** agent you get the full enriched card; with **Plex TV Series** you still
get the spoiler-free artwork, just not the text description.

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LLM_PROVIDER` | no | `groq` | `groq` or `gemini` — which LLM identifies games |
| `GROQ_API_KEY` | **yes** (groq) | — | Groq key ([console.groq.com/keys](https://console.groq.com/keys)) |
| `GROQ_MODEL` | no | `openai/gpt-oss-120b` | Groq model; supports strict JSON schema |
| `GEMINI_API_KEY` | yes (gemini) | — | Only if `LLM_PROVIDER=gemini` (needs the `gemini` image extra) |
| `GEMINI_MODEL` | no | `gemini-flash-latest` | Gemini model selection |
| `THESPORTSDB_API_KEY` | no | `123` (free) | Event metadata + artwork; premium key raises limits |
| `TZ` | recommended | `UTC` | Date reasoning for overnight games |
| `PUID` / `PGID` | no | `1000`/`1000` | Ownership of created files (unRAID: 99/100) |
| `STABILITY_SECONDS` | no | `120` | File is processed once its size is unchanged this long |
| `SWEEP_SECONDS` | no | `300` | Periodic watch-folder re-scan (safety net) |
| `MEDIA_EXTENSIONS` | no | `.mp4,.mkv,.avi,.mov,.mpeg,.ts` | Files to process; everything else (and hidden files) is ignored |
| `PRESERVE_ORIGINAL` | no | `false` | `true` = copy into the library, leave the original in `/watch` |
| `ARTWORK_MODE` | no | `download` | `generate` = always build badge/neutral cards locally |
| `MIN_CONFIDENCE` | no | `0.6` | Below this, files take the safe Unknown Event path |
| `RETRY_DAYS` | no | `7` | How long unknowns/missing artwork are retried (every 6h) |
| `PLEX_URL` / `PLEX_TOKEN` | no | — | Optional: instant partial scan after organizing |
| `PLEX_LIBRARY_PATH` | no | — | Plex's path to the library, if mounted differently than `/library` |
| `DRY_RUN` | no | `false` | Log planned actions without touching anything |
| `LOG_LEVEL` | no | `INFO` | `DEBUG` may include raw API payloads (contain scores) |

### Custom artwork assets (optional)

Drop these into your `/config` mount to override the generated versions:

- `unknown-event.jpg` — thumb used for recordings that couldn't be matched
- `badges/highlights.png` — badge composited onto highlights thumbs
- `badges/mini.png` — badge for condensed/mini matches

### Content variants

Filenames containing `HL`, `HLS` or `Highlights` are treated as highlights
packages; `Mini` as condensed matches. They match the same event as the full
game, get a badge on the thumb, a `(Highlights)` / `(Mini)` suffix in naming
(so they can coexist with the full game), and a spoiler-safe description in
the metadata.

## CLI

The container runs `sfps daemon` by default. All stages are also runnable
one-off (handy for testing a filename before committing to a recording rule):

```bash
docker exec spoilerfreeplexsports sfps identify "JWC South Africa v Wales.mkv"
docker exec spoilerfreeplexsports sfps process "/watch/some file.ts" --dry-run
docker exec spoilerfreeplexsports sfps review     # list unmatched recordings
docker exec spoilerfreeplexsports sfps review --set-event 2466440 "/library/Unknown Events/some game"
docker exec spoilerfreeplexsports sfps retry      # re-attempt unknowns + missing artwork now
docker exec spoilerfreeplexsports sfps config     # effective config + problems
docker exec spoilerfreeplexsports sfps health     # heartbeat check
```

## How matching stays safe

- A guess below `MIN_CONFIDENCE`, with no usable date, or that fails
  team+date verification against TheSportsDB goes to
  `library/Unknown Events/` with a placeholder thumb — a wrong match (wrong
  artwork on the wrong game) is treated as worse than no match.
- The processed-file ledger (`/config/ledger.db`) means restarts and
  re-scans never double-process; a re-recorded file (different size) is
  picked up as new work.
- TheSportsDB is crowd-sourced and often gains events/artwork days after a
  game airs: a retry pass runs every 6 hours (for `RETRY_DAYS`) that
  re-attempts Unknown Events and upgrades generated thumbs to real event
  artwork. `sfps review --set-event <id> <dir>` lets you force a match by
  hand for anything left over.
- Matched games with no downloadable event art get a **generated
  badge-vs-badge card** built from the teams' real badges (with a neutral
  text card as the last resort).
- **Teamless events** — races, tours, individual sports (e.g. a Tour de
  France stage) — that identify to a competition but have no verifiable
  per-event record fall back to the **competition's own poster/banner**
  from TheSportsDB and are filed under that competition, instead of landing
  in Unknown Events. Competition branding can't reveal a result, so it stays
  spoiler-safe.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .[dev]
ruff check . && pytest
```

Design document: [design.md](design.md). CI publishes
`ghcr.io/jamesgallagher/spoilerfreeplexsports:latest` (amd64 + arm64) on every
commit to main.
