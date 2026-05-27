# m3 — Architecture

A scheduled Python service that reads media filenames from Plex library directories, routes each file to a matching metadata plugin, retrieves metadata from external APIs/sites, writes NFO sidecar files + posters to disk, and pushes the same metadata to Plex via its API with field locks. A built-in web dashboard provides run history, plugin inspection, manual triggers, and live run status.

---

## Design Goals

- **Non-destructive** — never modifies or renames original media files
- **Portable metadata** — NFO/poster sidecars travel with media; Plex DB is a fast cache, not the source of truth
- **Extensible** — new sites/APIs added as drop-in Python plugin files, no core changes needed
- **Self-contained** — runs as a single Docker container on Unraid; scheduled internally via APScheduler; web dashboard served from same process
- **Transparent** — unmatched files and errors surfaced in logs, run report, and web dashboard; never silently skipped or mis-tagged

---

## High-Level Flow

```
[APScheduler cron / "Run now" button / SIGUSR1]
       │
       ▼
[Library Scanner]  (app/scanner.py)
  - Enumerates configured library directories in a single O(n) per-directory pass
  - Classifies each video file:
      • NFO exists with matching stem → skip (already processed)
      • NFO missing → new file (full workflow below)
      • Exactly 1 orphan NFO + 1 new video in same directory → rename workflow
      • Ambiguous (multiple orphans or multiple new videos) → treat all as new
  - --force bypasses rename detection and skip logic entirely
       │
       ├─[renamed files]──────────────────────────────────────────────────────┐
       │                                                                        │
       │  [Rename Workflow]  (app/main.py, app/writers/nfo.py, plex.py)        │
       │    - rename_nfo_assets(): renames .nfo + images, patches XML art paths│
       │    - push_nfo_to_plex(): re-pushes fields from renamed NFO to Plex,   │
       │      uploads local images (no redundant download from source)         │
       │    - recorded as status="renamed" in the run report                   │
       │                                                                        │
       ▼                                                                       ▼
[Universal Parser]  (app/parser.py)           [Run Report: renamed += 1]
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
[Plugin] (one per file, rate-limited by PLUGIN_RATE_LIMIT_SECS)
  - Receives: ParsedFilename (typed, structured)
  - Plugin decides which subtype fields to use (Enhanced / Limited / Exact)
  - Calls its API or scrapes HTML via app/scrape.py
  - Returns: normalized MetadataResult
       │
       ▼
[Writers — run sequentially]
  ├── [NFO Writer]     writes <filename>.nfo + poster.jpg + fanart.jpg next to media
  └── [Plex Writer]    pushes MetadataResult to Plex HTTP API with field locks
       │
       ▼
[Run Report]  (app/reporter.py)
  - Written to configured report path after each run (skipped on --dry-run)
  - Lists: updated, renamed, skipped, unmatched, scrape_errors, errors, duration
```

**Web dashboard** runs concurrently in a daemon thread (uvicorn + FastAPI):
```
[uvicorn daemon thread]
  GET /          → latest run summary + next scheduled time
  GET /runs      → run history with duration column
  GET /runs/{ts} → per-run detail with status filters + inline retry buttons
  GET /unmatched → cross-run unmatched file digest
  GET /files     → per-file processing history across all runs (?path=…)
  GET /plugins   → loaded plugin list
  GET /config    → active env var values (token masked)
  GET /api/status → HTMX-polled run-in-progress badge
  GET /logs      → last N lines of m3.log (?tail=N, default 200, max 2000)
  POST /trigger/run   → schedule an immediate run
  POST /trigger/file  → re-process a single file (runs in a daemon thread)
  GET /healthz   → Docker HEALTHCHECK endpoint; ?check=plex for live Plex probe
```

---

## Directory Layout

```
m3/
├── Dockerfile
├── docker-compose.yml            # example Unraid-compatible compose
├── unraid-template.xml           # Unraid Community Apps XML template
├── requirements.txt
├── requirements-dev.txt          # pytest + coverage (local dev only)
├── config.example.yml            # annotated env var reference (docs only)
│
├── app/
│   ├── main.py                   # entrypoint; scheduler; web server; --once/--force flags
│   ├── config.py                 # loads/validates all config from env vars
│   ├── scheduler.py              # APScheduler cron; Windows-safe SIGUSR1/SIGUSR2 handlers
│   ├── runstate.py               # thread-safe run-in-progress flag for web UI
│   ├── utils.py                  # shared retry_with_backoff and call_with_timeout helpers
│   ├── logging_setup.py          # rotating file + stdout handlers; retention cleanup
│   ├── parser.py                 # universal filename decoder → ParsedFilename
│   ├── scanner.py                # walks library dirs, yields media files
│   ├── router.py                 # O(1) site_id dispatch; alias resolution
│   ├── reporter.py               # builds + writes run report; cleans up old logs/reports
│   ├── scrape.py                 # shared HTML fetch helper (retry/UA/validation)
│   │
│   ├── plugins/
│   │   ├── base.py               # MetadataPlugin base class, MetadataResult, ParsedFilename
│   │   └── loader.py             # importlib-based discovery from mounted plugin dir
│   │
│   ├── tools/
│   │   └── parse.py              # CLI tool: test the filename parser without running a full pass
│   │                             #   python -m app.tools.parse "Jane Doe % mysite - 12345"
│   │                             #   python -m app.tools.parse --json "Jane Doe % MS - eager-hands"
│   │
│   ├── web/
│   │   ├── __init__.py           # FastAPI app factory (create_app)
│   │   ├── routes.py             # all route handlers
│   │   ├── history.py            # run report reader + aggregate_unmatched
│   │   ├── static/style.css      # dark terminal-aesthetic stylesheet
│   │   └── templates/            # Jinja2 templates (base, latest, runs, run_detail,
│   │       ├── ...               #   unmatched, plugins, config)
│   │       └── partials/         # HTMX partial fragments (status_badge)
│   │
│   ├── writers/
│   │   ├── nfo.py                # writes NFO XML + downloads poster/fanart
│   │   └── plex.py               # PlexAPI integration; field-locked updates
│   │
│   └── tests/
│       ├── test_config.py           # 8 tests — config loading + env var parsing
│       ├── test_parser.py           # 29 tests — all forms, subtypes, edge cases
│       ├── test_router.py           # 5 tests — dispatch, aliases, unmatched
│       ├── test_scanner.py          # 18 tests — video detection, sidecar skip, force, path dedup
│       ├── test_plugin_loader.py    # 5 tests — discovery, bad files, missing dir
│       ├── test_reporter.py         # 19 tests — counters, file output, atomic write
│       ├── test_nfo_writer.py       # 19 tests — NFO XML, art block, atomic write, images
│       ├── test_plex_writer.py      # 19 tests — connect, find item, push
│       ├── test_run_integration.py  # 17 tests — run() orchestration, dry-run
│       ├── test_scrape.py           # 11 tests — fetch_html, retry, ScrapeError
│       ├── test_web_history.py      # 19 tests — list_runs, get_run, aggregate_unmatched + cache invalidation
│       ├── test_web_routes.py       # 51 tests — all routes via TestClient; flash/scope/coalescing/version/tail/badge/HTMX-error-fragment
│       ├── test_phase_fixes.py      # 26 tests — targeted regression tests
│       ├── test_main_dry_run.py     # 2 tests  — dry-run skips connect_plex, logs would-push
│       ├── test_main_fetch_timeout.py # 2 tests — plugin timeout → status=error, run continues
│       ├── test_main_webhook.py     # 7 tests  — webhook payload shape, no-url skip, network failure, NOTIFY_MIN_ERRORS threshold
│       └── test_scheduler.py        # 3 tests  — atomic registry replace, TZ wiring
│
├── plugins/                      # mounted from host at /plugins; drop .py files here
│   ├── example_plugin.py         # reference JSON API plugin (fully commented)
│   └── example_html_plugin.py    # reference HTML scraping plugin (BeautifulSoup)
│
└── docs/
    ├── ARCHITECTURE.md           # this file
    ├── FILENAME_PATTERNS.md      # full filename grammar + parser design
    ├── UNRAID_SETUP.md           # Docker build + Unraid deployment guide
    ├── PLUGIN_WRITING.md         # step-by-step plugin authoring guide
    ├── TROUBLESHOOTING.md        # common issues + fixes
    ├── FUTURE_UPDATES.md         # planned improvements (SQLite history, auth, orphan cleanup)
    ├── local-testing.md          # local dev testing guide (macOS, Linux, Windows)
    └── NOTE_filename_grammar_source.md
```

---

## Plugin Interface

Each plugin is a Python file in the mounted plugins directory. It must define a subclass of `MetadataPlugin`:

```python
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

class MyPlugin(MetadataPlugin):
    site_id = "mysite"          # matches the site token in filenames
    aliases = ["MS"]            # optional shorthand aliases

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        # parsed.match_subtype: "exact" | "enhanced" | "limited" | "add"
        # parsed.scene_id, parsed.title, parsed.actors, parsed.date available
        # Return None if the record isn't found (not a crash)
        ...
```

For HTML-only sites, use the shared scrape helper:

```python
from app.scrape import fetch_html, ScrapeError, SelectorMissingError
from bs4 import BeautifulSoup

html = fetch_html(f"https://mysite.com/scenes/{scene_id}")
soup = BeautifulSoup(html, "html.parser")
el = soup.select_one("h1.scene-title")
if not el:
    raise SelectorMissingError("title not found at h1.scene-title")
```

`SelectorMissingError` (a subclass of `ScrapeError`) is recorded as `status="scrape_error"` in the run report — distinct from a generic plugin crash — so the user can see "site changed its markup" vs "plugin has a bug".

See `docs/PLUGIN_WRITING.md` for a full step-by-step guide.

---

## HTML Scraping Helper (`app/scrape.py`)

`fetch_html(url, *, timeout=30, max_attempts=3) -> str`

- Browser-like User-Agent to avoid CDN bot-blocking
- Retries with exponential backoff via `app/utils.retry_with_backoff`
- Validates `text/html` content-type and rejects >10 MB responses
- Raises `ScrapeError` on network/HTTP errors; `SelectorMissingError` (site markup change) is raised by the plugin, not this helper

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
  <tag>custom-tag-1</tag>
  <actor><name>Actor Name</name></actor>
  <art>
    <poster>My Movie-poster.jpg</poster>
    <fanart>My Movie-fanart.jpg</fanart>
  </art>
  <source>https://mysite.com/item/12345</source>
  <uniqueid type="m3">12345</uniqueid>
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

Uses `python-plexapi`. Each field is written with `.locked = 1` so Plex's built-in agent doesn't overwrite it on the next scheduled refresh. Without locks, Plex silently discards custom metadata when its own agent (e.g. The Movie Database) runs:

```python
item.edit(**{
    "title.value": result.title,
    "title.locked": 1,
    ...
})
item.uploadPoster(url=result.poster_url)
item.uploadArt(url=result.fanart_url)
```

Plex connection is re-established at the start of every run (not once at startup) so scheduled runs don't use a stale connection after a Plex server restart.

**Fallback behaviour**: if Plex is unreachable, the run continues in sidecar-only mode. The Plex push is always non-fatal.

---

## Configuration

All config via environment variables. See `config.example.yml` for the full annotated reference.

| Variable | Default | Purpose |
|---|---|---|
| `PLEX_URL` | *(required)* | Plex server URL reachable from inside the container |
| `PLEX_TOKEN` | *(required)* | Plex authentication token |
| `LIBRARY_PATHS` | `/media` | Comma-separated container paths to scan |
| `PLUGIN_DIR` | `/plugins` | Where plugin `.py` files are discovered |
| `REPORT_PATH` | `/config/reports` | Where run reports are written |
| `LOG_PATH` | `/config/logs` | Where rotating log files are written |
| `RUN_SCHEDULE` | `0 3 * * *` | Cron expression (5-field) |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `LOG_RETENTION_DAYS` | `30` | Delete log files older than N days |
| `REPORT_RETENTION_DAYS` | `90` | Delete report files older than N days |
| `PLUGIN_RATE_LIMIT_SECS` | `1.0` | Seconds to wait between plugin fetch() calls |
| `PLUGIN_FETCH_TIMEOUT_SECS` | `60.0` | Seconds before a single plugin fetch() is marked as timed out |
| `WEB_ENABLED` | `true` | Enable the web dashboard |
| `WEB_PORT` | `8765` | Port the dashboard listens on |
| `WEB_HOST` | `0.0.0.0` | Host the dashboard binds to |
| `APP_NAME` | `m3` | Display name in the dashboard header and title |
| `NOTIFY_URL` | *(empty)* | Webhook URL to POST a JSON run summary after each run |
| `NOTIFY_MIN_ERRORS` | `0` | Minimum combined error count before the webhook fires (0 = always fire) |
| `LIBRARY_EXCLUDE_PATTERNS` | *(empty)* | Comma-separated glob patterns to skip during scanning (e.g. `*.part,/media/incoming/**`) |

---

## Scheduling

- APScheduler `BlockingScheduler` with a `CronTrigger` runs in the main thread
- Web dashboard (uvicorn) runs in a daemon thread alongside the scheduler
- `max_instances=1` prevents concurrent runs if a previous run is still in progress
- Manual trigger via web UI: `POST /trigger/run` calls `scheduler.add_job(..., replace_existing=True)`
- Manual trigger via signal: `docker exec m3 kill -USR1 1` (Unix only; Windows-guarded in `build_scheduler`)
- Plugin hot-reload via signal: `docker exec m3 kill -USR2 1` — `register_sigusr2_reload()` is called in `main()` immediately after `build_scheduler()`; it registers a SIGUSR2 handler that atomically replaces the shared `registry` dict contents (clear + update under a lock) so all live references see the new plugins without a container restart. `app.state.plugin_router` (the `Router` wrapping the registry in the web app) reads the same dict, so the web UI's plugin list and dispatch also reflect the reload automatically (Unix only; skipped on Windows)

## Run Modes

```bash
# Default — start scheduler + web dashboard, run nightly per RUN_SCHEDULE
docker run m3

# Manual one-shot — run immediately and exit (web dashboard does NOT start)
docker exec m3 python -m app.main --once

# Force re-process — ignore existing sidecars, re-fetch everything
docker exec m3 python -m app.main --once --force

# Dry run — parse + route + fetch but skip all writes and report
docker exec m3 python -m app.main --once --dry-run

# Diagnose unmatched files — scan library, print files no plugin claims, exit
docker exec m3 python -m app.main --list-unmatched
```

`--force` only applies to the run it's passed to. Scheduled runs always use normal skip logic.

---

## Logging

- `app/logging_setup.py` configures Python `logging` with two handlers:
  - `RotatingFileHandler` → `/config/logs/m3.log` (10 MB × 5 files) for history
  - `StreamHandler` → stdout for `docker logs` visibility
- `LOG_LEVEL` env var controls verbosity (default `INFO`)
- Log files older than `LOG_RETENTION_DAYS` (default 30) are deleted at the end of each run

---

## Run Report

After each run, `reporter.py` writes two files to `REPORT_PATH` (skipped when `--dry-run`):

- `run_<timestamp>.json` — full structured report (machine-readable, per-file results, duration)
- `run_latest.txt` — human-readable summary

File statuses:

| Status | Meaning |
|---|---|
| `updated` | Metadata fetched and written (NFO + Plex) |
| `renamed` | File renamed: sidecar assets renamed, Plex re-pushed from existing NFO; no API fetch |
| `skipped` | Sidecar already exists and `--force` not set |
| `unmatched` | Filename unparseable or no plugin registered for site |
| `add_form` | Manual Add form filename — needs human follow-up |
| `scrape_error` | Plugin raised `ScrapeError` — site markup changed or record gone |
| `error` | Plugin or writer raised an unexpected exception |

---

## Docker

`python:3.12-slim` base. Non-root `m3` user. Health check via `GET /healthz` on the web dashboard. See `Dockerfile` for the authoritative build definition.

---

## Key Dependencies

| Package | Purpose |
|---|---|
| `python-plexapi` | Plex HTTP API client |
| `apscheduler<4.0` | In-process cron scheduler |
| `httpx` | HTTP client for plugin API calls, image downloads, HTML scraping |
| `lxml` | NFO XML generation |
| `beautifulsoup4` | HTML parsing in HTML-scraping plugins |
| `fastapi` | Web dashboard framework |
| `uvicorn` | ASGI server for the web dashboard |
| `jinja2` | HTML templating for the web dashboard |
| `python-multipart` | Form parsing for `/trigger/file` and `/trigger/run` endpoints |

---

## Extension Points

- **New JSON API plugin**: drop a `.py` file in the plugins directory, restart the container
- **New HTML scraping plugin**: same as above, use `fetch_html` + BeautifulSoup
- **New metadata fields**: add to `MetadataResult`, update `nfo.py` and `plex.py` writers
- **New web page**: add a route in `app/web/routes.py` and a template in `app/web/templates/`
