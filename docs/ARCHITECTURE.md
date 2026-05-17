# pm — Architecture

A scheduled Python service that reads media filenames from Plex library directories, routes each file to a matching metadata plugin, retrieves metadata from external APIs/sites, writes NFO sidecar files + posters to disk, and pushes the same metadata to Plex via its API with field locks.

---

## Design Goals

- **Non-destructive** — never modifies or renames original media files
- **Portable metadata** — NFO/poster sidecars travel with media; Plex DB is a fast cache, not the source of truth
- **Extensible** — new sites/APIs added as drop-in Python plugin files, no core changes needed
- **Self-contained** — runs as a single Docker container on Unraid; scheduled internally via APScheduler
- **Transparent** — unmatched files and errors surfaced in logs and a run report, never silently skipped or mis-tagged

---

## High-Level Flow

```
[APScheduler cron]
       │
       ▼
[Library Scanner]
  - Enumerates configured library directories
  - Collects all media files (video extensions)
  - Skips files with up-to-date sidecar (unless --force)
       │
       ▼
[Universal Parser]  (app/parser.py)
  - Decodes filename into a structured ParsedFilename using the project grammar
  - See FILENAME_PATTERNS.md for the full DSL (two top-level forms, four match subtypes)
  - Failure to parse → add to unmatched report, skip
       │
       ▼
[Router]  (app/router.py)
  - O(1) dispatch: looks up plugin in {site_id: plugin} dict (aliases included)
  - "Add" form (no site) → dispatched to manual-add writer; no API call
  - No plugin registered for site → add to unmatched report, skip
       │
       ▼
[Plugin] (one per file)
  - Receives: ParsedFilename (typed, structured)
  - Plugin decides which subtype fields to use (Enhanced / Limited / Exact)
  - Calls its API/scraper
  - Returns: normalized MetadataResult
       │
       ▼
[Writers — run in parallel]
  ├── [NFO Writer]     writes <filename>.nfo + poster.jpg + fanart.jpg next to media
  └── [Plex Writer]    pushes MetadataResult to Plex HTTP API with field locks
       │
       ▼
[Run Report]
  - Written to configured report path after each run
  - Lists: matched, updated, skipped (already up-to-date), unmatched, errors
```

---

## Directory Layout

```
pm/
├── Dockerfile
├── docker-compose.yml            # example Unraid-compatible compose
├── requirements.txt
├── requirements-dev.txt          # pytest + coverage (local dev only)
├── .gitignore
│
├── app/
│   ├── main.py                   # entrypoint; starts scheduler; --once / --force flags
│   ├── config.py                 # loads/validates config from env vars
│   ├── scheduler.py              # APScheduler cron; Windows-safe SIGUSR1 handler
│   ├── logging_setup.py          # rotating file + stdout handlers; retention cleanup
│   ├── parser.py                 # universal filename decoder → ParsedFilename
│   ├── scanner.py                # walks library dirs, yields media files
│   ├── router.py                 # O(1) site_id dispatch; alias resolution
│   ├── reporter.py               # builds + writes run report; cleans up old logs/reports
│   │
│   ├── plugins/
│   │   ├── base.py               # MetadataPlugin base class, MetadataResult, ParsedFilename
│   │   └── loader.py             # importlib-based discovery from mounted plugin dir
│   │
│   ├── writers/
│   │   ├── nfo.py                # writes NFO XML + downloads poster/fanart
│   │   └── plex.py               # PlexAPI integration; field-locked updates
│   │
│   └── tests/
│       ├── test_parser.py        # 32 tests — all forms, subtypes, edge cases
│       ├── test_router.py        # 5 tests — dispatch, aliases, unmatched
│       ├── test_scanner.py       # 7 tests — video detection, sidecar skip, force
│       └── test_plugin_loader.py # 5 tests — discovery, bad files, missing dir
│
├── plugins/                      # mounted from host at /plugins; drop .py files here
│   └── example_plugin.py         # reference implementation with full comments
│
└── docs/
    ├── ARCHITECTURE.md           # this file
    ├── FILENAME_PATTERNS.md      # full filename grammar + parser design
    ├── UNRAID_SETUP.md           # Docker build + Unraid deployment guide
    ├── local-testing.md          # local dev testing guide (macOS, Linux, Windows)
    ├── NOTE_filename_grammar_source.md  # verbatim source note
    └── filename_patterns_raw.txt # raw note text
```

---

## Plugin Interface

Each plugin is a Python file in the mounted plugins directory. It must define a subclass of `MetadataPlugin`:

```python
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

class MyPlugin(MetadataPlugin):
    # Canonical site identifier — matches the `site` field parsed from filenames
    site_id = "mysite"

    # Optional shorthand aliases (e.g., "MS" routes to this plugin too)
    aliases: list[str] = ["MS", "mysite-shorthand"]

    def fetch(self, parsed: ParsedFilename) -> MetadataResult:
        # parsed.match_subtype tells you which search type was detected
        # parsed.scene_id, parsed.title, parsed.actors, parsed.date, etc. are available
        # Call your API here, return a MetadataResult
        ...
```

Plugins no longer declare filename patterns — the universal parser handles all filename decoding. Plugins only register their `site_id` and optional `aliases`, and implement `fetch()`. See `FILENAME_PATTERNS.md` for the full grammar.

`MetadataResult` is a dataclass:

```python
@dataclass
class MetadataResult:
    title: str
    summary: str | None = None
    rating: float | None = None         # e.g., 7.5
    genres: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)
    poster_url: str | None = None       # remote URL for poster image
    fanart_url: str | None = None       # remote URL for background image
    year: int | None = None
    content_rating: str | None = None   # e.g., "NR", "R", "TV-MA"
    source_url: str | None = None       # canonical URL for the item on the source site
    source_id: str | None = None        # site-internal ID; stored in NFO for traceability
```

Plugins have full access to `os.environ` for their API keys.

---

## Filename Pattern DSL

All filename parsing is handled by a single universal parser (`app/parser.py`). Filenames follow one of two top-level forms:

**Form 1 — Manual Add** (starts with keyword `Add`):
```
Add [<Date>] <Actor> [And <Actor> ...] [In <Title>] [At <Studio>] [With <Genre1>, <Genre2>]
```

**Form 2 — General** (everything else):
```
<Actors> with <Genres> % <Site> [- <Date>] [- <ID/Title/Actor>...]
```
The `%` separator splits actors+genres from the site-specific match payload. The parser determines which of four match subtypes applies (Enhanced / Limited / Exact Match / Direct URL) based on the structure of the payload.

See `FILENAME_PATTERNS.md` for the full grammar, all subtypes, worked examples, and parser implementation notes.

---

## NFO Format

Follows the [Kodi NFO spec](https://kodi.wiki/view/NFO_files/Movies) so the sidecars work with Kodi, Jellyfin, and Plex (via XBMCnfoMoviesImporter):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>Full Resolved Title</title>
  <year>2023</year>
  <rating>7.5</rating>
  <mpaa>NR</mpaa>
  <plot>Summary text here.</plot>
  <genre>Drama</genre>
  <genre>Thriller</genre>
  <tag>custom-tag-1</tag>
  <actor><name>Actor Name</name></actor>
  <art>
    <poster>poster.jpg</poster>
    <fanart>fanart.jpg</fanart>
  </art>
  <!-- pm metadata -->
  <source>https://mysite.com/item/12345</source>
  <uniqueid type="mysite">12345</uniqueid>
</movie>
```

Sidecar files written next to media:
```
/media/
  My Movie.mp4
  My Movie.nfo          ← NFO sidecar
  My Movie-poster.jpg   ← poster
  My Movie-fanart.jpg   ← background art (if available)
```

---

## Plex API Integration

Uses `python-plexapi`. Each field is written with a lock so Plex's built-in agent doesn't overwrite it on the next scheduled refresh:

```python
item.edit(**{
    "title.value": result.title,
    "title.locked": 1,
    "summary.value": result.summary,
    "summary.locked": 1,
    "rating.value": result.rating,
    "rating.locked": 1,
    # genres, labels, tags via addGenre() / addLabel() / addTag() with lock=True
})
item.uploadPoster(url=result.poster_url)
item.uploadArt(url=result.fanart_url)
```

Plex connection is established once at startup and reused across runs.

---

## Configuration

All config via environment variables (standard Docker on Unraid). An optional mounted YAML for structured, non-secret config:

**Environment variables:**
```bash
PLEX_URL=http://192.168.1.100:32400
PLEX_TOKEN=your-plex-token

LIBRARY_PATHS=/media/movies,/media/adult     # comma-separated host paths
PLUGIN_DIR=/plugins                           # mounted plugin directory
REPORT_PATH=/config/reports                   # where run reports are written
LOG_PATH=/config/logs                         # where rotating log files are written
RUN_SCHEDULE="0 3 * * *"                      # cron expression (default: 3am nightly)
LOG_LEVEL=INFO
LOG_RETENTION_DAYS=30                         # delete log files older than this
REPORT_RETENTION_DAYS=90                      # delete report files older than this

# Plugin-specific secrets (each plugin reads its own keys via os.environ)
MYSITE_API_KEY=...
OTHERSITE_API_KEY=...
```

**Unraid Docker template volumes:**
```
/mnt/user/appdata/pm/plugins  → /plugins     (plugin drop-in dir)
/mnt/user/appdata/pm/config   → /config      (reports, state)
/mnt/user/media               → /media       (library root, read-write for sidecars)
```

---

## Scheduling

- APScheduler runs inside the container (`BlockingScheduler` with a `CronTrigger`)
- Schedule is parsed from `RUN_SCHEDULE` env var on startup
- Concurrent runs are prevented (next scheduled run skips if previous is still running)

## Run Modes

```bash
# Default — start scheduler, run nightly per RUN_SCHEDULE
docker run pm

# Manual one-shot — run immediately and exit (respects sidecar skip logic)
docker exec pm python -m app.main --once

# Force re-process — ignore existing sidecars, re-fetch everything
docker exec pm python -m app.main --once --force
```

`--force` only applies to the run it's passed to. Scheduled runs always use normal skip logic.

**Manual trigger without restart (Unix/Linux/macOS only):**
```bash
docker exec pm kill -USR1 1
```
`SIGUSR1` is not available on Windows. On Windows use `--once` via `docker exec` instead.

---

## Logging

- `app/logging_setup.py` configures Python `logging` with two handlers:
  - `RotatingFileHandler` → `/config/logs/pm.log` (10 MB × 5 files) for history
  - `StreamHandler` → stdout for `docker logs` visibility
- `LOG_LEVEL` env var controls verbosity (default `INFO`)
- At the end of each run, log files older than `LOG_RETENTION_DAYS` (default 30) are deleted
- Run reports older than `REPORT_RETENTION_DAYS` (default 90) are also cleaned up

---

## Run Report

After each run, `reporter.py` writes two files to `REPORT_PATH`:

- `run_<timestamp>.json` — full structured report (machine-readable, per-file results)
- `run_latest.txt` — human-readable summary:

```
pm run — 2025-05-15 03:00:01
─────────────────────────────
  Total files scanned : 142
  Updated             : 12
  Skipped (up-to-date): 128
  Manual Add (pending): 3
  Unmatched           : 2
  Errors              : 0

Manual Add files (no plugin; need studio setup):
  /media/movies/Add Jane Doe At UnknownStudio.mp4
  ...

Unmatched files (could not parse or no plugin registered):
  /media/movies/Some Unknown Title.mp4
  /media/movies/Another File.mkv

Errors:
  (none)
```

---

## Docker

`python:3.12-slim` base. Non-root user. Health check via a simple file-last-modified check on the report directory.

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
RUN useradd -m pm && chown -R pm /app
USER pm
ENTRYPOINT ["python", "-m", "app.main"]
```

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `python-plexapi` | Plex HTTP API client |
| `apscheduler` | In-process cron scheduler |
| `httpx` | HTTP client for plugin API calls and image downloads |
| `lxml` | NFO XML generation |

---

## Extension Points

- **New plugin**: drop a `.py` file in the plugins directory, restart the container
- **New metadata fields**: add to `MetadataResult`, update `nfo.py` and `plex.py` writers
- **Web UI** (future): FastAPI layer over `scheduler.py` + `reporter.py` — no core changes needed
