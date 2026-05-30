# m3 — Plex Metadata Agent

A scheduled Python service that reads media filenames from Plex library
directories, routes each file to a site-specific metadata plugin, fetches
metadata from external APIs, writes NFO sidecar files + poster images to disk,
and pushes the same metadata to Plex via its API with field locks.

---

## Quick Start

### Docker (Unraid / standard Docker host)

```bash
docker run -d \
  -e PLEX_URL=http://192.168.1.100:32400 \
  -e PLEX_TOKEN=your-plex-token \
  -e LIBRARY_PATHS=/media/movies,/media/adult \
  -e PLUGIN_DIR=/plugins \
  -e REPORT_PATH=/config/reports \
  -e LOG_PATH=/config/logs \
  -v /mnt/user/appdata/m3/plugins:/plugins \
  -v /mnt/user/appdata/m3/config:/config \
  -v /mnt/user/media:/media \
  m3
```

Or use the included `docker-compose.yml`:

```bash
docker-compose up -d
```

### Local testing

```bash
# macOS / Linux / Git Bash
make setup        # create .venv and install all deps
cp .env.example .env   # fill in your values
make dev          # start scheduler + web dashboard

# Windows (CMD / PowerShell — no make required)
python tasks.py setup
copy .env.example .env
python tasks.py dev
```

See [docs/local-testing.md](docs/local-testing.md) for a full step-by-step guide
covering macOS, Linux, and Windows.

---

## How It Works

```
Scheduled cron
      ↓
Scan library dirs for video files
  ├─[NFO exists, stem matches] → skip (already processed)
  ├─[1 orphan NFO + 1 new video in same dir] → rename workflow ─────────────┐
  └─[no NFO] → new file                                                      │
      ↓                                                          [Rename workflow]
Parse filename → structured ParsedFilename (actors, genres,     rename .nfo + images
  site, date, IDs …)                                            patch XML art paths
      ↓                                                          re-push to Plex
Route to plugin by site_id / alias  ─── no plugin found → unmatched report
      ↓
Plugin fetches metadata from its API
      ↓
Write .nfo sidecar + poster/fanart images  ←── portable backup
Write metadata to Plex with field locks    ←── fast path for Plex UI
      ↓
Run report (updated / renamed / skipped / unmatched / errors)
```

Full architecture details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

## Filename Grammar

Filenames follow one of two forms:

**General form:**
```
<Actors> with <Genres> % <Site> [- <Date>] [- <ID/Title/Actor>...]
```

**Manual Add form:**
```
Add [<Date>] <Actor> [And <Actor>...] [In <Title>] [At <Studio>] [With <Genre>...]
```

Full grammar, all subtypes, and worked examples: [docs/FILENAME_PATTERNS.md](docs/FILENAME_PATTERNS.md)

---

## Writing a Plugin

Drop a `.py` file into the plugins directory. Minimal example:

```python
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

class MyPlugin(MetadataPlugin):
    site_id = "mysite"
    aliases = ("MS",)  # Use tuple (not list) — the class-level default must be immutable

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        # Call your API here; return MetadataResult or None
        ...
```

See `plugins/example_plugin.py` for a fully-commented reference implementation.

Plugins are instantiated once at startup and shared across all files in every run — they must be stateless (no per-run instance variables). If you need per-run state, use local variables inside `fetch()`.

---

## Web Dashboard

When running normally (not `--once`), m3 serves a built-in dashboard at
`http://<host>:8765` (default port):

| Page | URL | What you see |
|---|---|---|
| Latest run | `/` | Run status, stats, next scheduled time, Run Now button |
| Run history | `/runs` | All past runs with duration and success rate; paginated 25/page |
| Run detail | `/runs/<timestamp>` | Per-file status with inline ↺ retry buttons; click any path to view file history |
| Unmatched digest | `/unmatched` | Files never claimed by any plugin, across all runs; filterable by path |
| File history | `/files?path=…` | All runs a specific file appeared in, with status and message |
| Plugin list | `/plugins` | All loaded plugins and their site IDs |
| Config | `/config` | Active env var values (Plex token masked) |
| Log viewer | `/logs` | Last N lines of m3.log (default 200, max 2000); `?tail=N` or line-count buttons in the UI |
| Health check | `/healthz` | Docker health-check endpoint; add `?verbose=1` for last-run time, `?check=plex` to probe Plex reachability |

**Run Now** — the recommended way to trigger an immediate run. For headless setups (no browser), `docker exec m3 kill -USR1 1` (Unix only) has the same effect. If a run is already in progress, clicking Run Now shows a flash message and does not queue a second run.

**Inline retry** — on the run-detail page, any `error`, `scrape_error`, or `unmatched` row has a
↺ button that re-queues that single file for immediate reprocessing.

**`/healthz?verbose=1`** — extended response for uptime monitors (Uptime Kuma, Grafana, etc.):

```json
{
  "status": "ok",
  "last_run": "2025-05-17T03:00:01",
  "hours_since_last_run": 6.4,
  "run_count": 42
}
```

Alert when `hours_since_last_run` exceeds your expected run interval (e.g. > 26h for a nightly schedule).

**Status indicator** — the header shows "▶ Running — Ns" while a run is in progress
and "Idle" otherwise, polled automatically every 2 seconds via HTMX.

To disable the dashboard: set `WEB_ENABLED=false`. To change the port: set `WEB_PORT=<port>`.

---

## Run Modes

```bash
# Start scheduler (default — runs on RUN_SCHEDULE cron, default 3am nightly)
python -m app.main

# Run once and exit
python -m app.main --once

# Run once, re-process files that already have .nfo sidecars
python -m app.main --once --force

# Dry run — parse + route + fetch but skip all writes
python -m app.main --once --dry-run

# Dry-run-strict — skip plugin fetch() entirely; test parsing + routing with no API calls
python -m app.main --once --dry-run-strict

# Trigger an immediate run without restarting the container (Unix/Linux/macOS)
docker exec m3 kill -USR1 1

# Reload plugins without restarting the container (Unix/Linux/macOS)
docker exec m3 kill -USR2 1

# Watch plugins dir and hot-reload on changes — cross-platform (local dev)
python -m app.main --watch

# List all files that would be unmatched (no plugin claimed them)
python -m app.main --list-unmatched

# Validate all plugins — pass/fail table, exits 1 on failure
python -m app.main --validate-plugins

# Test a single plugin file + filename, print MetadataResult (no writes, no Plex)
python -m app.main --test-plugin plugins/mysite.py --filename "Jane Doe % mysite - 12345.mp4"

# Re-process only failed files from the most recent run
python -m app.main --retry-failed

# Re-process failures merged across the last N runs (deduplicated)
python -m app.main --retry-failed=3

# Test the filename parser standalone
python -m app.tools.parse "Jane Doe with Drama % mysite - 12345"
python -m app.tools.parse --json "Jane Doe % MS - eager-hands"   # JSON output
```

---

## Configuration

All configuration is via environment variables. See [config.example.yml](config.example.yml)
for the full annotated list, or [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details.

Key variables:

| Variable | Default | Purpose |
|---|---|---|
| `PLEX_URL` | *(required)* | Plex server URL |
| `PLEX_TOKEN` | *(required)* | Plex authentication token |
| `LIBRARY_PATHS` | `./media` (local) / `/media` (Docker) | Comma-separated paths to scan |
| `LIBRARY_EXCLUDE_PATTERNS` | *(empty)* | Comma-separated glob patterns to skip (e.g. `*.part,/media/incoming/**`) |
| `RUN_SCHEDULE` | `0 3 * * *` | Cron expression for scheduled runs |
| `PLUGIN_RATE_LIMIT_SECS` | `1.0` | Seconds between plugin API calls (0 to disable) |
| `PLUGIN_FETCH_TIMEOUT_SECS` | `60.0` | Seconds before a single plugin fetch() is aborted and marked as error |
| `NOTIFY_URL` | *(empty)* | Webhook URL for post-run JSON summary (Apprise, Gotify, etc.) |
| `NOTIFY_MIN_ERRORS` | `1` | Defaults to `1` (only notify on errors); set to `0` to notify after every run including clean ones. |
| `APP_NAME` | `m3` | Display name in dashboard header and run reports |
| `WEB_PORT` | `8765` | Dashboard port |
| `WEB_HOST` | `0.0.0.0` | Dashboard bind address (`127.0.0.1` to restrict to localhost) |
| `LOG_LEVEL` | `INFO` | `DEBUG` for troubleshooting |
| `TZ` | `UTC` | Container timezone for scheduled runs (e.g. `America/New_York`) |

---

## Tests

```bash
# macOS / Linux
make setup && make test
make test-cov    # with coverage report

# Windows / all platforms
python tasks.py setup
python tasks.py test
python tasks.py test-cov

# Or manually:
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest app/tests/ -v
```

CI runs automatically on every push and PR via GitHub Actions
(`ubuntu-latest` + `windows-latest`, Python 3.12). Coverage threshold: 80%.

---

## Deployment (Unraid)

See [docs/UNRAID_SETUP.md](docs/UNRAID_SETUP.md) for the Docker build and Unraid
Docker template configuration.
