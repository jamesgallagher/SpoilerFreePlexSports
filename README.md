# SpoilerFreePlexSports

**Plex keeps spoiling your recorded games: its auto-generated thumbnails grab a
frame from the video — usually one with the score on it.** This service fixes
that.

It watches a staging folder for new sports recordings, identifies the game
(Google Gemini reads the filename, TheSportsDB confirms the event), then moves
each game into a tidy `League/Season/Game` folder with **spoiler-free artwork
and metadata** — before Plex ever sees the file.

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

1. **Docker tab → Template Repositories** (bottom of page) → add:
   `https://github.com/jamesgallagher/SpoilerFreePlexSports`
2. **Add Container** → select the *SpoilerFreePlexSports* template.
3. Fill in:
   - **Watch folder** — where your recorder writes files (files are *moved out*
     once processed). Do **not** point this at the library itself.
   - **Library folder** — the share your Plex sports library reads from.
   - **Gemini API key** — get one at https://aistudio.google.com/apikey
   - **Timezone** — your recorder's timezone (matters for overnight games).
4. Apply. The container starts the watcher daemon and is health-checked via a
   heartbeat.

Files are created as `PUID:PGID` (defaults 99:100, unRAID's `nobody:users`).

## Quick start — docker compose

```bash
curl -O https://raw.githubusercontent.com/jamesgallagher/SpoilerFreePlexSports/main/docker-compose.yml
# edit the two volume paths, then:
GEMINI_API_KEY=your-key docker compose up -d
```

## Setting up the Plex library (important!)

Create a **TV Shows** library pointed at your library folder, then in that
library's settings:

| Setting | Value | Why |
|---|---|---|
| Scanner / Agent | Plex TV Series | Date-based episode naming is used |
| **Local Media Assets** | **Enabled**, ordered **first** (Settings → Agents) | This is how the spoiler-free thumbs/posters get used |
| **Video preview thumbnails** | **Disabled** | Plex generates these from the video — they leak scores on the seek bar |
| Seasons | Show | One season per year per league |

Leave "Generate chapter thumbnails" off for the same reason.

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | **yes** | — | Game identification from filenames |
| `GEMINI_MODEL` | no | `gemini-flash-latest` | Model selection |
| `THESPORTSDB_API_KEY` | no | `123` (free) | Event metadata + artwork; premium key raises limits |
| `TZ` | recommended | `UTC` | Date reasoning for overnight games |
| `PUID` / `PGID` | no | `1000`/`1000` | Ownership of created files (unRAID: 99/100) |
| `STABILITY_SECONDS` | no | `120` | File is processed once its size is unchanged this long |
| `SWEEP_SECONDS` | no | `300` | Periodic watch-folder re-scan (safety net) |
| `MEDIA_EXTENSIONS` | no | `.ts,.mkv,.mp4` | Files to process |
| `ARTWORK_MODE` | no | `download` | `generate` = always create neutral cards locally |
| `MIN_CONFIDENCE` | no | `0.6` | Below this, files take the safe Unknown Event path |
| `PLEX_URL` / `PLEX_TOKEN` | no | — | Optional: instant partial scan after organizing |
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

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .[dev]
ruff check . && pytest
```

Design document: [design.md](design.md). CI publishes
`ghcr.io/jamesgallagher/spoilerfreeplexsports:latest` (amd64 + arm64) on every
commit to main.
