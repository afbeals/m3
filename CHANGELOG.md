# Changelog

All notable changes to pm are documented here.

## [1.1.0] — cycle 11: rename detection, correctness, UX, new features

### New features
- **Rename detection** — when exactly one video is renamed in a directory between runs, pm detects the 1:1 orphan-NFO / new-video pairing and renames the existing `.nfo`, `-poster.jpg`, and `-fanart.jpg` sidecars automatically; patches XML art paths; re-pushes metadata to Plex from the existing NFO (no redundant API fetch). Status: `renamed` in the run report and dashboard.
- `LIBRARY_EXCLUDE_PATTERNS` env var — comma-separated glob patterns to skip files during scanning (e.g. `*.part,/media/incoming/**`)
- `--retry-failed` CLI flag — reads the latest run report and re-processes all `error` / `scrape_error` files without a full library rescan
- `/healthz?check=plex` — live Plex reachability probe; returns `{"plex":"ok"}` or 503 without affecting process liveness
- Run detail pagination — large runs (>200 files) are paged; `?page=N` supported alongside existing `?status=` filter
- File history page (`/files?path=…`) — shows every run a given file appeared in, with status and message

### Correctness
- `Content-Length: 0` no longer bypasses the download size guard in `_download_image`
- Log viewer (`/logs`) no longer crashes on non-UTF-8 log characters (`errors="replace"`)
- `/trigger/file` path-scope check uses `Path.is_relative_to()` instead of `os.path.commonpath`, correctly rejecting sibling directories
- Labels now pushed and cleared correctly during the rename workflow (were silently dropped previously)

### Documentation & comments
- WHY comments added: `_file_trigger_locks` no-removal rationale, `is_relative_to()` threat model, two-stage Content-Length/streaming size guard, rename detection ambiguity handling
- README: updated How It Works diagram, config table, clarified `--force`, `--dry-run` scope, plugin statelessness requirement
- `ARCHITECTURE.md`: updated flow diagram, routes list, and status table to reflect rename workflow
- `UNRAID_SETUP.md`: added `TZ`, `LIBRARY_EXCLUDE_PATTERNS`, `PLUGIN_FETCH_TIMEOUT_SECS` to env var table and docker-compose example
- `TROUBLESHOOTING.md`: added rename workflow section and `--retry-failed` recovery guidance
- `config.example.yml` and `docker-compose.yml`: documented `LIBRARY_EXCLUDE_PATTERNS` with examples

### Tests
- 254 tests passing (was 221); new coverage for rename detection, `push_nfo_to_plex`, `LIBRARY_EXCLUDE_PATTERNS`, file history, run_detail pagination, `/healthz?check=plex`

## [1.0.0] — initial public release

### Core pipeline
- Universal filename parser (two top-level forms, four match subtypes: exact / enhanced / limited / add)
- O(1) plugin router with alias support; plugin hot-reload via SIGUSR2 (Unix)
- NFO XML writer following Kodi spec (Plex / Jellyfin / Kodi compatible)
- Image downloader with size cap and streaming reads
- Plex API writer with field locks; fallback sidecar-only mode when Plex is unreachable
- Per-plugin fetch timeout (configurable via `PLUGIN_FETCH_TIMEOUT_SECS`)
- Atomic writes via `.tmp` + `os.replace` for all NFO, image, and report files
- Dry-run mode (`--once --dry-run`) — parses, routes, fetches; skips all writes
- `--list-unmatched` diagnostic mode — scans library, prints files no plugin claims

### Scheduler & web dashboard
- APScheduler cron scheduler with `max_instances=1`; TZ-aware via the `TZ` env var
- Run-in-progress badge polled via HTMX; targeted card swap preserves scroll position
- Run history with paginated table, per-status filter tabs, inline retry buttons
- Cross-run unmatched digest with search filter and per-row retry
- Config page annotates library paths with exists/missing status
- Log viewer (last 200 lines) with auto-scroll on initial load only
- `/healthz` endpoint with version + hours-since-last-run (verbose mode)
- Manual trigger guards: already-running flash message, per-path coalescing (409)
- Library path scope validation on `/trigger/file`

### Notifications
- Webhook POST after every completed (non-dry-run) run; payload includes `app_name`, `first_error`, `first_scrape_error`

### Infrastructure
- `tzdata` installed in Docker image; `TZ` env var correctly wires to APScheduler
- `PLUGIN_FETCH_TIMEOUT_SECS` env var (default 60 s)
- Version surfaced in `/healthz` JSON, User-Agent header, and dashboard footer
- Stale `.tmp` orphan sweep at startup (files > 30 min old)
- SIGUSR2 plugin reload uses atomic dict replacement under lock
