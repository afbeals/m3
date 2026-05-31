# Local Testing Guide

How to run and test `m3` locally on macOS, Linux, or Windows before deploying
to Docker/Unraid.

---

## Prerequisites

Ensure the following are installed before starting:

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.12+ | **Windows:** During install, check "Add python.exe to PATH" |
| Git | any | [Git for Windows](https://gitforwindows.org/) includes Git Bash |

**Verify Python:** Open a new terminal and run `python --version` — expected: `Python 3.12.x`

Other prerequisites:
- `pip` / `venv` (included with Python)
- An API key for whichever site your plugin targets
- Optional: a running Plex server (not required — the app works in sidecar-only
  mode if Plex is unreachable)

---

## Step 1 — Clone and set up

```bash
git clone https://github.com/YOUR_ORG/m3.git
cd m3
```

Then create the virtual environment.

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

pip install -r requirements.txt -r requirements-dev.txt
```

#### Windows (Command Prompt)

```cmd
cd C:\path\to\m3

python -m venv .venv
.venv\Scripts\activate.bat

pip install -r requirements.txt -r requirements-dev.txt
```

#### Windows (PowerShell)

```powershell
cd C:\path\to\m3

python -m venv .venv
```

**Windows PowerShell** — run this once to allow scripts (if you get an error about scripts being disabled):
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then activate the virtual environment:
```powershell
.venv\Scripts\Activate.ps1
```

```powershell
pip install -r requirements.txt -r requirements-dev.txt
```

---

## 2. Configure your environment

Copy `.env.example` to `.env` and fill in your values:

```bash
# macOS / Linux / Git Bash:
cp .env.example .env

# Windows CMD:
copy .env.example .env

# Windows PowerShell:
Copy-Item .env.example .env
```

The `.env` file is loaded automatically at startup (`override=False`, so any
environment variables already set in your shell take precedence). This removes
the need to export a long list of `VAR=value` prefixes before every command.

> **Note:** The default `LIBRARY_PATHS=./media` requires that directory to exist. Create it now:
> ```bash
> # macOS / Linux / Git Bash:
> mkdir media
>
> # Windows CMD:
> md media
>
> # Windows PowerShell:
> New-Item -ItemType Directory media
> ```
> Or skip this step and use `python tasks.py gen-test-lib` in Step 3 which creates it automatically.

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

By default the generator uses `examplesite` as the site token. To generate files for your own plugin, pass `--site yoursite`:

```bash
python3 scripts/generate_test_library.py --site mysite
```

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
New-Item ".\test-media\Jane Doe with Drama % examplesite - 19-06-15 - 12345 - An Interesting Plot.mp4"
New-Item ".\test-media\Add Jane Doe And Mary Smith In My Scene At MyStudio.mp4"
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
2. Change `aliases` if you want shorthand (e.g. `("MS",)`)  # Use tuple, not list — class-level defaults must be immutable
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

> **Note:** `example_plugin.py` will always show `FAIL` in validation unless `EXAMPLESITE_API_KEY` is set in your `.env`. This is expected behavior for the reference template — it requires an API key to function. Copy it to `plugins/mysite.py` and configure your own credentials.

---

## 6. Test your plugin in isolation (no full run)

### Test filename parsing (no API call)

#### macOS / Linux / Git Bash

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

#### Windows (PowerShell)

PowerShell has no equivalent of `<<'EOF'` heredocs. Save the snippet to a file and run it:

```powershell
@"
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
"@ | python
```

#### Windows (CMD)

```cmd
python -c "from app.parser import parse; stems=['Jane Doe with Drama %% mysite - 12345']; [print(parse(s)) for s in stems]"
```

> **CMD note**: `%` must be escaped as `%%` inside `python -c` on CMD. For multi-line scripts, save them to a `.py` file and run `python <file>.py`.

### Test your plugin with `--test-plugin`

Load a single plugin file, call `fetch()` on a parsed filename, and print the
full `MetadataResult` — no library scan, no Plex connection, no writes:

Pass the filename stem without the `.mp4` extension — the parser expects no file extension:

```bash
python -m app.main \
  --test-plugin plugins/mysite.py \
  --filename "Jane Doe with Drama % mysite - 12345"
```

**Windows CMD** — escape `%` as `%%`:
```cmd
python -m app.main --test-plugin plugins\mysite.py --filename "Jane Doe with Drama %% mysite - 12345"
```

**Windows PowerShell** — `%` is not special, no escaping needed:
```powershell
python -m app.main --test-plugin plugins\mysite.py --filename "Jane Doe with Drama % mysite - 12345"
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

### Expected output

A successful `python tasks.py once` run looks like:

```
INFO     app.main: m3 1.3.0 starting up
INFO     app.main: Loaded 0 plugins (add plugins to ./plugins/)
INFO     app.main: Scanning library paths...
INFO     app.scanner: Scanner found 3 file(s) to process, 0 skipped
INFO     app.main: [1/3] Processing: test-media/Jane Doe % examplesite - 12345.mp4
WARNING  app.main: No plugin registered for site 'examplesite'
INFO     app.main: Run complete. updated=0 skipped=0 unmatched=3 errors=0
```

If you see `No media files found` — check that `LIBRARY_PATHS` in `.env` points to your `media/` folder.
If you see `unmatched=N` — this is expected; you need a plugin for each site token.

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

> **Tip:** The scheduler runs at 3am by default — nothing happens immediately! To trigger a run:
> - Open the dashboard at `http://localhost:8765` and click **Run Now**
> - Or use `python tasks.py once` for a one-shot run that exits when complete

### Live template / UI development

For iterating on HTML templates, the fastest workflow is:

1. Run `python tasks.py dev` (or `python tasks.py dev-reload` which starts with `DEBUG=true`)
2. Edit templates in `app/web/templates/`
3. Press `Ctrl+C` to stop, then restart with `python tasks.py dev`

> **Note:** uvicorn's `--reload` mode is not compatible with m3's architecture (the web server runs in a daemon thread). Use the restart approach above for template iteration. The `python tasks.py dev-reload` command starts the app with `DEBUG=true` which logs a reminder about this.

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
2. python -m app.main --test-plugin plugins/mysite.py --filename "Jane Doe % mysite - 12345"
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
