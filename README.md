# pm — Plex Metadata Agent

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
  -v /mnt/user/appdata/pm/plugins:/plugins \
  -v /mnt/user/appdata/pm/config:/config \
  -v /mnt/user/media:/media \
  pm
```

Or use the included `docker-compose.yml`:

```bash
docker-compose up -d
```

### Local testing

See [docs/local-testing.md](docs/local-testing.md) for a step-by-step guide to
running pm on macOS, Linux, or Windows without Docker.

---

## How It Works

```
Scheduled cron
      ↓
Scan library dirs for video files
      ↓
Parse filename → structured ParsedFilename (actors, genres, site, date, IDs …)
      ↓
Route to plugin by site_id / alias  ─── no plugin found → unmatched report
      ↓
Plugin fetches metadata from its API
      ↓
Write .nfo sidecar + poster/fanart images  ←── portable backup
Write metadata to Plex with field locks    ←── fast path for Plex UI
      ↓
Run report (updated / skipped / unmatched / errors)
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
    aliases = ["MS"]

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        # Call your API here; return MetadataResult or None
        ...
```

See `plugins/example_plugin.py` for a fully-commented reference implementation.

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

# Trigger an immediate run without restarting the container (Unix/Linux/macOS)
docker exec pm kill -USR1 1
```

---

## Configuration

All configuration is via environment variables. See [config.example.yml](config.example.yml)
for the full annotated list, or [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for details.

---

## Tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest app/tests/ -v
```

---

## Deployment (Unraid)

See [docs/UNRAID_SETUP.md](docs/UNRAID_SETUP.md) for the Docker build and Unraid
Docker template configuration.
