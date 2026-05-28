# Local Testing Guide

How to run and test `m3` locally on macOS, Linux, or Windows before deploying
to Docker/Unraid.

---

## Prerequisites

- Python 3.12 or later
- `pip` / `venv` (included with Python)
- An API key for whichever site your plugin targets
- Optional: a running Plex server (not required — the app works in sidecar-only
  mode if Plex is unreachable)

---

## 1. Set up a virtual environment

### Quickstart (any platform)

The repo ships with a `Makefile` (macOS/Linux/Git Bash) and a pure-Python
`tasks.py` (all platforms including Windows CMD/PowerShell):

```bash
# macOS / Linux / Git Bash on Windows
make setup

# Windows (CMD or PowerShell — no make required)
python tasks.py setup
```

Both create `.venv/`, upgrade pip, and install all dependencies from
`requirements.txt` and `requirements-dev.txt`.

### Manual setup

#### macOS / Linux

```bash
cd /path/to/m3

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

#### Windows (Command Prompt)

```cmd
cd C:\path\to\m3

python -m venv .venv
.venv\Scripts\activate.bat

pip install -r requirements.txt
```

#### Windows (PowerShell)

```powershell
cd C:\path\to\m3

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

> **Note:** If PowerShell blocks the activation script, run:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

---

## 2. Configure your environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

The `.env` file is loaded automatically at startup (`override=False`, so any
environment variables already set in your shell take precedence). This removes
the need to export a long list of `VAR=value` prefixes before every command.

Key values to set for local testing:

```dotenv
PLEX_URL=http://your-plex-ip:32400
PLEX_TOKEN=your-plex-token
LIBRARY_PATHS=./test-media          # or wherever your test files are
PLUGIN_DIR=./plugins
MYSITE_API_KEY=your-api-key

# Dev-friendly defaults already set in .env.example:
LOG_LEVEL=DEBUG
PLUGIN_RATE_LIMIT_SECS=0            # no delay between API calls during dev
WEB_HOST=127.0.0.1
```

> **Without a `.env` file**: all path defaults (`PLUGIN_DIR`, `REPORT_PATH`,
> `LOG_PATH`, `LIBRARY_PATHS`) point to relative paths (`./plugins`,
> `./reports`, `./logs`, `./media`) so the app starts without any config.

---

## 3. Generate a test media library

Instead of creating dummy files by hand, use the bundled generator:

```bash
# macOS / Linux
python3 scripts/generate_test_library.py

# Windows
python scripts\generate_test_library.py

# Or via task runner:
make gen-test-lib
python tasks.py gen-test-lib
```

This creates `./test-media/` with empty `.mp4` files covering all filename
subtypes: exact match, enhanced, limited, add-form, and intentionally
unmatched. Use `--output <dir>` to write to a different path.

You can also create files manually:

```bash
# macOS / Linux
mkdir -p ./test-media
touch "./test-media/Jane Doe with Drama % examplesite - 12345.mp4"
touch "./test-media/Jane Doe with Drama % examplesite - 19-06-15 - 12345 - An Interesting Plot.mp4"
touch "./test-media/Add Jane Doe And Mary Smith In My Scene At MyStudio.mp4"
```

```powershell
# Windows (PowerShell)
New-Item -ItemType Directory -Force -Path .\test-media
New-Item ".\test-media\Jane Doe with Drama % examplesite - 12345.mp4"
```

---

## 4. Write a plugin for your site

Copy the example plugin and fill in your API logic:

```bash
# macOS / Linux
cp plugins/example_plugin.py plugins/mysite.py

# Windows
copy plugins\example_plugin.py plugins\mysite.py
```

Edit `plugins/mysite.py` — at minimum:
1. Change `site_id` to match the token you'll use in filenames (e.g. `"mysite"`)
2. Change `aliases` if you want shorthand (e.g. `["MS"]`)
3. Set `BASE_URL` to your API's base URL
4. Update `_to_result()` to map your API's response fields to `MetadataResult`
5. Fill in `_fetch_by_id()`, `_fetch_enhanced()`, `_fetch_limited()` with real API calls

---

## 5. Validate your plugin

Before running a full pass, check that your plugin loads and passes structural
validation:

```bash
python -m app.main --validate-plugins
```

This loads all plugins in `PLUGIN_DIR`, checks `site_id` format, subclassing,
and `fetch()` signature, and prints a pass/fail table. Exits with code 1 if any
plugin fails.

---

## 6. Test your plugin in isolation (no full run)

### Test filename parsing (no API call)

```bash
python3 - <<'EOF'
from app.parser import parse

stems = [
    "Jane Doe with Drama % mysite - 12345",
    "Jane Doe with Drama % mysite - 19-06-15 - 12345 - An Interesting Plot",
    "Add Jane Doe And Mary Smith In My Scene At MyStudio With Drama, Comedy",
]

for stem in stems:
    result = parse(stem)
    if result:
        print(f"subtype={result.match_subtype} site={result.site} scene_id={result.scene_id} title={result.title!r}")
    else:
        print(f"UNMATCHED: {stem!r}")
EOF
```

### Test your plugin with `--test-plugin`

Load a single plugin file, call `fetch()` on a parsed filename, and print the
full `MetadataResult` — no library scan, no Plex connection, no writes:

```bash
python -m app.main \
  --test-plugin plugins/mysite.py \
  --filename "Jane Doe with Drama % mysite - 12345.mp4"
```

On Windows:
```cmd
python -m app.main --test-plugin plugins\mysite.py --filename "Jane Doe with Drama %% mysite - 12345.mp4"
```

This is the fastest way to iterate on plugin mapping logic. You'll see each
`MetadataResult` field printed directly.

### Dry-run-strict (no API calls at all)

Test that your filenames parse and route correctly without making any network
requests:

```bash
python -m app.main --once --dry-run-strict
```

Unlike `--dry-run` (which still calls `fetch()`), `--dry-run-strict` skips all
plugin API calls. Useful for verifying parsing and routing on a large library
without needing API keys or network access.

---

## 7. Run a full one-shot pass

With `.env` set up (step 2), just run:

```bash
# macOS / Linux
source .venv/bin/activate
python3 -m app.main --once

# Windows
.venv\Scripts\activate.bat
python -m app.main --once

# Or via task runner (activates venv automatically):
make once
python tasks.py once
```

Add `--force` to re-process files that already have a `.nfo` sidecar:

```bash
python -m app.main --once --force
```

> **Without `.env`**: pass env vars explicitly as before, or export them in
> your shell. The `.env` approach is recommended for local dev.

---

## 8. Run with the web dashboard

Omit `--once` to start the scheduler and web server together (just like in Docker):

```bash
# macOS / Linux
make dev
# or:
python3 -m app.main

# Windows
python tasks.py dev
# or:
python -m app.main
```

Open `http://localhost:8765` in your browser. Click **Run Now** to trigger an
immediate run. Press Ctrl+C to stop.

### Live reload during template / UI development

The web dashboard runs in a daemon thread alongside the scheduler, which means
uvicorn's built-in `--reload` mode cannot be used in that architecture. For live
reloading of templates and routes during UI development, run uvicorn directly in
a separate terminal:

```bash
python -m uvicorn app.web:create_app --reload --port 8765
```

This starts only the web server with auto-reload on file changes. Run the
scheduler separately in another terminal if needed.

---

## 9. Plugin hot-reload during development

Use `--watch` to automatically reload plugins when you save changes:

```bash
python -m app.main --watch
```

This monitors `PLUGIN_DIR` for `.py` file changes and reloads all plugins
automatically — no container restart or signal required. Works on all platforms
including Windows (uses `watchfiles`).

On Unix, you can also use the signal:
```bash
docker exec m3 kill -USR2 1
```

---

## 10. Check the outputs

```bash
# List what was written next to the media file
ls ./test-media/

# Read the NFO sidecar
cat "./test-media/Jane Doe with Drama % mysite - 12345.nfo"

# Read the run summary report
cat ./reports/run_latest.txt

# Tail the log for detail
tail -50 ./logs/m3.log
```

Windows (PowerShell):
```powershell
Get-ChildItem .\test-media\
Get-Content ".\test-media\Jane Doe with Drama % mysite - 12345.nfo"
Get-Content .\reports\run_latest.txt
Get-Content .\logs\m3.log -Tail 50
```

---

## 11. Run the test suite

```bash
# macOS / Linux
make test
make test-cov    # with coverage report

# Windows / all platforms
python tasks.py test
python tasks.py test-cov

# Or manually:
python -m pytest app/tests/ -v
python -m pytest app/tests/ -v --cov=app --cov-report=term-missing
```

---

## 12. Skipping Plex during local testing

If you don't have Plex running locally, the app continues automatically in
sidecar-only mode. You'll see these warnings in the log — they are expected:

```
WARNING app.writers.plex: Failed to connect to Plex at http://...
WARNING app.main: Plex connection failed. Metadata will be written to sidecars only.
```

NFO files and images are still written normally. You can test the full
plugin → parse → fetch → NFO pipeline without a Plex server.

---

## Typical iteration loop

```
1. Edit plugins/mysite.py
2. python -m app.main --test-plugin plugins/mysite.py --filename "Jane Doe % mysite - 12345.mp4"
     → see full MetadataResult or error message immediately
3. python -m app.main --once --force
     → full pipeline pass; check run_latest.txt
4. Check: ./test-media/Jane Doe % mysite - 12345.nfo  →  verify NFO fields
5. Check: ./logs/m3.log  →  full detail trace
6. Repeat
```

Use `--watch` to skip manual reloads while editing:
```bash
python -m app.main --watch  # keeps running; reloads plugins on save
```

---

## Retrying failed files

After a run with errors, re-process only the failed files:

```bash
python -m app.main --retry-failed       # most recent run
python -m app.main --retry-failed=3     # merge failures from last 3 runs
```

`--retry-failed` reads the run report(s) and re-processes every file with
status `error`, `scrape_error`, or `image_error`. It bypasses the normal
skip-if-NFO-exists logic for only the targeted files.

---

## Windows-specific notes

| Topic | Detail |
|---|---|
| Task runner | Use `python tasks.py <task>` instead of `make` if you're on CMD/PowerShell (no Git Bash). All Makefile targets have an equivalent `tasks.py` command. |
| Manual run trigger | `SIGUSR1` (trigger immediate run) is Unix-only. Use the **Run Now** button in the dashboard, or `--once` locally. |
| Plugin hot-reload | `SIGUSR2` is Unix-only. Use `--watch` flag instead — it's cross-platform and works on Windows. |
| Path separators | Use backslashes (`C:\m3-test-media`) for `LIBRARY_PATHS` when running locally on Windows. Inside Docker the paths are always Linux-style. |
| Python command | Use `python` (not `python3`) on most Windows installs. |
| Line endings | The app writes NFO files in UTF-8. If you open them in Notepad and see no line breaks, use Notepad++ or VS Code instead. |
| RotatingFileHandler | `delay=True` is set, deferring file open until the first write. This reduces Windows file-locking issues during log rotation. |
| Path case sensitivity | `LIBRARY_PATHS` entries that differ only by case (e.g. `C:\Media` and `c:\media`) are automatically deduplicated on Windows. |
