# Changelog

All notable changes to m3 are documented here.

## [2.0.0] — plugin framework, web improvements, Windows-ready

### Highlights

A large quality and hardening pass across all layers: plugin API expanded with new metadata fields, web dashboard gains dry-run mode and a filename test modal, Docker Hub CI/CD workflow added, Windows development experience fully documented and tested.

### Plugin system

- **New `MetadataResult` fields** — `release_date` (ISO date, auto-derives `year`; writes `<premiered>` to NFO and `originallyAvailableAt` to Plex), `directors` (list; writes `<director>` to NFO and pushes to Plex), `studio` (writes `<studio>` to NFO and Plex).
- **`PluginValidationError`** — distinct from `ValueError`; raised by `__post_init__` for invalid data; recorded as `status=plugin_error` in reports.
- **`setup()` lifecycle hook** — called once after plugin instantiation; raise to prevent registration (e.g. missing API key). All example plugins demonstrate this.
- **`ScrapeError` re-exported** from `app.plugins.base` — single import line for all plugin primitives.
- **`close()` lifecycle hook** — called on hot-reload to release resources (e.g. `httpx.Client`).
- **Element-type validation** — list fields (`actors`, `genres`, etc.) now validate all elements are strings.
- **`site_id` whitespace guard** — `__init_subclass__` now uses `.strip()` to catch whitespace-only `site_id` values.
- **`all_ids()` strips aliases** — whitespace stripped from aliases before registration.
- **Loader: deferred `sys.modules` cleanup** — shared helper modules stay available across the full plugin load loop; cleaned up after all files are processed.
- **Loader: eviction safety** — on collision, all alias keys for the evicted plugin are removed before `close()` is called; `closed_instances` set prevents double-close.

### Real plugins added

- **`plugins/tmdb.py`** — TMDb v3 JSON API plugin (free key required). Uses `append_to_response=credits,images` for a single-request full-detail fetch. Full comments explaining every decision.
- **`plugins/thetvdb.py`** — TheTVDB HTML scraper (no key required). Verified against live pages. Selectors documented.
- **`plugins/example_plugin.py`** and **`plugins/example_html_plugin.py`** — heavily commented templates; demonstrate all `MetadataResult` fields, `setup()`, `PluginValidationError`, result scoring, session reuse, CSS selector registry, and lazy-load image fallback.

### Web dashboard

- **Dry-run mode** — "Run now" button now has a "Dry run (routing only, no writes)" checkbox. Triggers a `dry_run_strict` run; results show as `status=skipped` with routing preview.
- **Filename test modal** — "⚡ Test a filename" button on the Plugins page. Paste any filename stem; shows parse result, which plugin handles it, and the full `MetadataResult` — all in the dashboard without touching the CLI.
- **`plugin_error` status** — filter tab, stat card, and badge added to run detail page.
- **Pagination fix** — "Page X of Y" now shows the real total run count, not the capped 100-entry limit.
- **Stats partial** — `/api/stats` endpoint returns just the stats block; dashboard polls it instead of the full `/` page.
- **Log panel partial** — `/api/log-panel` returns only the log content; refresh button no longer injects full HTML into `#log-panel`.
- **`plugin_errors` and `image_errors` in webhook payload** — downstream alert systems can now distinguish all failure modes.
- **CSP fix** — all inline `<script>` blocks moved to `/static/dashboard.js`; `script-src 'self'` now actually enforces correctly.
- **`trigger_file` uses resolved path** — symlinks and `..` components normalised before NFO construction.
- **Trigger records cached** — file history page no longer re-reads all trigger JSON files on every request.
- **`status=renamed` documented** in TROUBLESHOOTING.md.

### Writers (NFO/Plex)

- **`write_images` before `write_nfo`** — image extension (jpg/png/webp) now determined before the NFO is written, so `<art>` block references the correct file.
- **Content-type extension detection** — images saved with correct extension derived from `Content-Type` header; `.jpg` fallback only when no image exists.
- **`originallyAvailableAt`** pushed to Plex from both `push_to_plex` and `push_nfo_to_plex`.
- **`push_nfo_to_plex` reads `<director>` and `<studio>`** from NFO.
- **Partial image cleanup** — partially-downloaded images deleted before recording `image_error`.
- **`image_error` message corrected** — now says "NFO not written" (NFO is skipped when images fail).
- **XXE-safe parser** — `rename_nfo_assets` now uses `XMLParser(resolve_entities=False)`.
- **0-byte image rejected** — `Content-Length: 0` response rejected before writing.

### Data correctness

- **`dry_run=True` records `status=skipped`** — run summaries are now meaningful in dry-run mode.
- **Report filename collision** — `os.getpid()` appended to prevent same-second clobber.
- **`plex_failed=True` for renamed files** — Plex push failure on rename now surfaces in reports.
- **`get_run` LRU cache** — double-checked locking prevents TOCTOU eviction of valid entries.
- **`list_runs` mtime sort** — uses `(mtime, filename)` as sort key; filename tiebreaker handles Windows 10ms filesystem resolution.
- **`--retry-failed` retries `image_error` and `plugin_error`** (previously only `error` and `scrape_error`).

### Code quality

- **`FileStatus` Literal type** defined in `reporter.py`; used throughout.
- **`total_scanned` unconditional** — unknown status values no longer silently undercount the total.
- **`_stop_event` removed** — rate-limit sleep uses `time.sleep`; the unused event was misleading.
- **`_run_with_media` forwards `dry_run`** — `--retry-failed --dry-run` now works correctly.
- **`notify_min_errors` default raised to 1** — webhook no longer fires on every clean run.
- **`PLUGIN_FETCH_TIMEOUT_SECS=0` warns** at startup.
- **`PLUGIN_RATE_LIMIT_SECS=0` warns** at startup.
- **`safe_app_name` strips Windows-illegal chars** from log filename.
- **`cleanup_old_files` backup detection** tightened to numeric suffix only.
- **`retry_with_backoff` guards** — `max_attempts < 1` and `backoff_base < 0` now raise `ValueError`.

### Windows development

- **`tasks.py setup()`** validates Python 3.12+ at startup with a clear message.
- **`tasks.py dev/once/dev-reload`** use `run_interactive()` with `CREATE_NEW_PROCESS_GROUP` so Ctrl+C works correctly in PowerShell.
- **`tzdata`** added to `requirements.txt` — non-UTC TZ values work on Windows without system timezone database.
- **`lxml>=5.3.0`** — pre-built wheels available for Python 3.13 on Windows.
- **`watchfiles`** moved to `requirements-dev.txt` — not installed in production Docker image.
- **`.gitignore`** active `plugins/*.py` rule — custom plugins with API keys not accidentally committed.
- **`test_validate_paths`** Windows-compatible — uses file-as-directory to guarantee OSError on all platforms.
- **`trigger_reload` SIGUSR2** accessed via `getattr` — no `AttributeError` at import time on Windows.

### Docker / Unraid

- **`.github/workflows/docker.yml`** — auto-builds and pushes to Docker Hub on every `main` push (requires `DOCKER_HUB_USER` variable + `DOCKER_HUB_TOKEN` secret).
- **`tasks.py build/push/release`** — `python tasks.py release` handles version tagging on all platforms.
- **`unraid-template.xml`** — `<Repository>` updated to `YOURDOCKERHUBUSER/m3:latest` placeholder; `NOTIFY_MIN_ERRORS` default corrected to `1`.
- **Dockerfile** — `HEALTHCHECK` comment fixed; base image `python:3.12-slim`.

### Documentation

- **Prerequisites section** — Python PATH warning, Python 3.12 requirement, Git install.
- **`.env.example`** created — all env vars documented; `PLEX_URL`/`PLEX_TOKEN` marked optional (sidecar-only mode works without Plex).
- **UNRAID_SETUP.md** — Docker Desktop for Windows workflow (WSL2, GUI auth, PowerShell syntax, Docker Hub Desktop verification); Unraid "Check for Updates → Update" replaces manual `docker pull`.
- **PLUGIN_WRITING.md** — `MetadataResult` field reference table; `setup()`/`close()` lifecycle; `PluginValidationError` manual raise example; minimal runnable stub; heredoc replaced with Windows-compatible save-to-file approach.
- **TROUBLESHOOTING.md** — "Local development on Windows" section; `status=renamed` entry; `image_error` root-cause diagnosis; `add_form` guidance.
- **FILENAME_PATTERNS.md** — `studio_id`/`actress_id` clarified as never populated by the parser.

## [1.3.0] — developer ergonomics, Windows support, new CLI flags

### New features

- **`--dry-run-strict`** — skips plugin `fetch()` calls entirely (returns `None` for all files). Lets you test filename parsing, routing, and the pipeline structure with zero network access and no API keys required. Regular `--dry-run` still calls `fetch()` but skips writes; `--dry-run-strict` goes one step further.
- **`--test-plugin <path>`** — loads a single plugin file, calls `fetch()` on a parsed filename (supply with `--filename "..."`) and prints the full `MetadataResult` fields. No library scan, no writes, no Plex connection. Fastest way to iterate on plugin mapping logic.
- **`--validate-plugins`** — scans all plugins in `PLUGIN_DIR`, checks each for correct `site_id` format, `MetadataPlugin` subclass, importability, and `fetch()` signature. Prints a pass/fail table; exits with code 1 if any plugin fails. Use before deploying a batch of new plugins.
- **`--watch`** — monitors `PLUGIN_DIR` for `.py` file changes and hot-reloads plugins automatically. Cross-platform via `watchfiles` (works on Windows; replaces the Unix-only `SIGUSR2` signal for local development).
- **`scripts/generate_test_library.py`** — generates a fake media library in `./test-media/` with empty `.mp4` files covering all parse subtypes (exact/enhanced/limited/add/unmatched). No real media needed; use for smoke-testing the pipeline.
- **`Makefile`** — `make setup`, `test`, `test-cov`, `lint`, `format`, `typecheck`, `dev`, `dev-reload`, `once`, `validate`, `gen-test-lib`, `clean` targets. Git Bash / WSL required for `make` on Windows; equivalent `python tasks.py <task>` works everywhere.
- **`tasks.py`** — pure-Python cross-platform task runner. All Makefile targets available as `python tasks.py <task>`, including `dev-reload` (uses env dict, not Unix inline assignment) and `clean` (uses `shutil` + `pathlib`, not `rm -rf`).
- **`.env` file support** — `python-dotenv` is now a dependency. A `.env` file in the working directory is loaded at startup (`override=False`, so container env vars always win). `.env.example` is included as a fully annotated reference covering all variables with dev-friendly defaults.

### Correctness & Windows
- **`plugin_error` status** — `MetadataResult.__post_init__` now raises `PluginValidationError` (a subclass of `ValueError`) instead of a bare `ValueError` when validation fails (missing/blank title, out-of-range rating, etc.). Recorded as `status=plugin_error` in the run report, distinct from `scrape_error` (site issue) and `error` (unexpected crash). Retry with `--retry-failed` after fixing the plugin's `_to_result()` mapping.
- **`--retry-failed` now also retries files with `status=plugin_error`.**
- **Log file rotation** — `RotatingFileHandler` now uses `delay=True`, deferring file open until the first write. Reduces Windows file-locking contention during log rotation.
- **Path deduplication** — `_dedup_paths()` in `scanner.py` now uses `pathlib.Path.resolve()` and case-insensitive comparison on Windows (`sys.platform == "win32"`). Prevents double-scanning when `LIBRARY_PATHS` contains aliases that differ only by case (e.g. `C:\Media` and `c:\media`).
- **Local-friendly config defaults** — path defaults changed from absolute container paths (`/plugins`, `/config/reports`, `/config/logs`, `/media`) to relative paths (`./plugins`, `./reports`, `./logs`, `./media`). Docker users set these via env vars (unchanged). Running locally without env vars now works out of the box.
- **uvicorn reload** — removed `reload=debug_mode` from the `uvicorn.Config` in the daemon thread (reload requires being the main process; it silently failed in a daemon thread). `DEBUG=true` now logs instructions to run uvicorn directly for live template reload: `python -m uvicorn app.web:create_app --reload`.

### Tests & CI
- **`conftest.py`** — shared pytest fixtures (`config`, `app`, `client`, `write_run`) extracted to `app/tests/conftest.py`, replacing duplicated `_make_config()` / `_make_app()` helpers across test files.
- **GitHub Actions CI** — `.github/workflows/test.yml` runs the full test suite with coverage on `ubuntu-latest` and `windows-latest` (Python 3.12) on every push and PR.
- **Coverage threshold** — `fail_under = 85` enforced in `pyproject.toml` via `[tool.coverage.report]`.
- **Ruff config** — `[tool.ruff]` and `[tool.ruff.lint]` sections added to `pyproject.toml`; `ruff` added to dev extras and `requirements-dev.txt`.
- **Pytest markers** — `unit` and `integration` markers registered in `pyproject.toml`.

## [1.2.0] — rename to m3, image error visibility, HTMX feedback, dev ergonomics

### New features
- **`image_error` status** — when NFO is written successfully but one or more images fail to download, the file is now recorded as `image_error` rather than silently logged as `updated`. Visible in the run detail page (stat card, filter tab, retry button) and plain-text report.
- **`--retry-failed` now also retries files with `status=image_error`.**
- **Trigger record history** — `/trigger/file` (inline ↺ retry) now writes a lightweight `trigger_*.json` record after each manual retrigger. The file history page (`/files?path=…`) surfaces these alongside regular run entries, marked with a ↺ indicator.
- **`--retry-failed=N`** — extend `--retry-failed` to merge failures across N recent runs. `--retry-failed` (no argument) still defaults to the most recent run; `--retry-failed=3` collects unique failures from the last 3 runs. Duplicate paths across runs are deduplicated.
- **`.env` file support** — `python-dotenv` is now a dependency. If a `.env` file exists in the working directory at startup, it is loaded automatically (container env vars take precedence via `override=False`). Production containers are unaffected; `.env` is a development convenience.
- **Docker version tagging** — `UNRAID_SETUP.md` build instructions now tag both `:latest` and `:{version}` simultaneously, enabling rollback to a previous image without rebuilding.

### Correctness
- `write_images()` return value was previously ignored in `main.py`; image download failures had no effect on the reported status. Now correctly records `image_error` and stops processing the file's status as `updated`.
- Log filename is now derived from `APP_NAME` env var (default `m3`) rather than being hardcoded as `m3.log`. Also fixes the rotated-backup cleanup check and the log viewer route to use the dynamic name.
- `/trigger/run` HTMX callers now receive a `200` HTML fragment response instead of a `303` redirect (which HTMX treats as full-page navigation, silently discarding the `hx-target` swap).

### Tests
- Extended `/trigger/run` test coverage: new tests for HTMX `already-running` warning fragment (200, no redirect), HTMX success fragment (200, no redirect), and non-HTMX redirect path (303). Existing non-HTMX tests preserved unchanged.

### Rename
- Application renamed from `pm` to `m3` throughout: module namespace (`m3_plugin.*`), Docker image name, log filenames, User-Agent header, docs.

## [1.1.0] — cycle 11: rename detection, correctness, UX, new features

### New features
- **Rename detection** — when exactly one video is renamed in a directory between runs, m3 detects the 1:1 orphan-NFO / new-video pairing and renames the existing `.nfo`, `-poster.jpg`, and `-fanart.jpg` sidecars automatically; patches XML art paths; re-pushes metadata to Plex from the existing NFO (no redundant API fetch). Status: `renamed` in the run report and dashboard.
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
