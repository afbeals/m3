# Local Testing Guide

How to run and test `pm` locally on macOS, Linux, or Windows before deploying
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

### macOS / Linux

```bash
cd /path/to/pm

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### Windows (Command Prompt)

```cmd
cd C:\path\to\pm

python -m venv .venv
.venv\Scripts\activate.bat

pip install -r requirements.txt
```

### Windows (PowerShell)

```powershell
cd C:\path\to\pm

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

> **Note:** If PowerShell blocks the activation script, run:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

---

## 2. Create a test media file

The scanner looks for real video files on disk. Create a dummy file named
exactly as you'd name a real media file — the filename stem is what gets parsed.

### macOS / Linux

```bash
mkdir -p /tmp/pm-test-media

# General form with site token
touch "/tmp/pm-test-media/Jane Doe with Drama % examplesite - 12345.mp4"

# Enhanced search with date and scene ID
touch "/tmp/pm-test-media/Jane Doe with Drama % examplesite - 19-06-15 - 12345 - An Interesting Plot.mp4"

# Manual Add form
touch "/tmp/pm-test-media/Add Jane Doe And Mary Smith In My Scene At MyStudio.mp4"
```

### Windows (Command Prompt)

```cmd
mkdir C:\pm-test-media

type nul > "C:\pm-test-media\Jane Doe with Drama % examplesite - 12345.mp4"
```

### Windows (PowerShell)

```powershell
New-Item -ItemType Directory -Force -Path C:\pm-test-media

New-Item "C:\pm-test-media\Jane Doe with Drama % examplesite - 12345.mp4"
```

---

## 3. Write a plugin for your site

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

## 4. Test the filename parser in isolation (no API needed)

Before running the full pipeline, verify your filenames parse correctly:

### macOS / Linux

```bash
source .venv/bin/activate

python3 - <<'EOF'
from app.parser import parse

stems = [
    "Jane Doe with Drama % examplesite - 12345",
    "Jane Doe with Drama % examplesite - 19-06-15 - 12345 - An Interesting Plot",
    "Add Jane Doe And Mary Smith In My Scene At MyStudio With Drama, Comedy",
    "Jane Doe % ES - eager-hands",   # alias + direct URL slug
]

for stem in stems:
    result = parse(stem)
    if result:
        print(f"\nStem: {stem!r}")
        print(f"  form:          {result.form}")
        print(f"  actors:        {result.actors}")
        print(f"  genres:        {result.genres}")
        print(f"  site:          {result.site}")
        print(f"  match_subtype: {result.match_subtype}")
        print(f"  date:          {result.date}")
        print(f"  scene_id:      {result.scene_id}")
        print(f"  title:         {result.title}")
        print(f"  direct_url:    {result.direct_url}")
    else:
        print(f"\nStem: {stem!r}  →  UNMATCHED (returned None)")
EOF
```

### Windows (PowerShell)

```powershell
.venv\Scripts\Activate.ps1

python -c "
from app.parser import parse
stem = 'Jane Doe with Drama % examplesite - 12345'
r = parse(stem)
print('form:', r.form, 'site:', r.site, 'scene_id:', r.scene_id)
"
```

---

## 5. Run a full one-shot pass

Set environment variables and run with `--once`:

> **Note:** `--once` runs immediately and exits. It does **not** start the web
> dashboard. To use the dashboard locally, omit `--once` (see step 5b below).

> **Tip:** Set `PLUGIN_RATE_LIMIT_SECS=0` when testing locally to disable the
> per-plugin delay. The default 1-second wait between API calls is designed for
> production runs over large libraries — skip it during development.

### macOS / Linux

```bash
source .venv/bin/activate

PLEX_URL=http://your-plex-ip:32400 \
PLEX_TOKEN=your-plex-token \
LIBRARY_PATHS=/tmp/pm-test-media \
PLUGIN_DIR=./plugins \
REPORT_PATH=/tmp/pm-test-reports \
LOG_PATH=/tmp/pm-test-logs \
LOG_LEVEL=DEBUG \
EXAMPLESITE_API_KEY=your-api-key \
python3 -m app.main --once
```

Add `--force` to re-process files that already have a `.nfo` sidecar:

```bash
... python3 -m app.main --once --force
```

### Windows (Command Prompt)

```cmd
set PLEX_URL=http://your-plex-ip:32400
set PLEX_TOKEN=your-plex-token
set LIBRARY_PATHS=C:\pm-test-media
set PLUGIN_DIR=plugins
set REPORT_PATH=C:\pm-test-reports
set LOG_PATH=C:\pm-test-logs
set LOG_LEVEL=DEBUG
set EXAMPLESITE_API_KEY=your-api-key

python -m app.main --once
```

### Windows (PowerShell)

```powershell
$env:PLEX_URL       = "http://your-plex-ip:32400"
$env:PLEX_TOKEN     = "your-plex-token"
$env:LIBRARY_PATHS  = "C:\pm-test-media"
$env:PLUGIN_DIR     = "plugins"
$env:REPORT_PATH    = "C:\pm-test-reports"
$env:LOG_PATH       = "C:\pm-test-logs"
$env:LOG_LEVEL      = "DEBUG"
$env:EXAMPLESITE_API_KEY = "your-api-key"

python -m app.main --once
```

---

## 5b. Run with the web dashboard (optional)

Omit `--once` to start the scheduler and web server together (just like in Docker):

```bash
source .venv/bin/activate

PLEX_URL=http://your-plex-ip:32400 \
PLEX_TOKEN=your-plex-token \
LIBRARY_PATHS=/tmp/pm-test-media \
PLUGIN_DIR=./plugins \
REPORT_PATH=/tmp/pm-test-reports \
LOG_PATH=/tmp/pm-test-logs \
LOG_LEVEL=DEBUG \
EXAMPLESITE_API_KEY=your-api-key \
python3 -m app.main
```

Open `http://localhost:8765` in your browser. You'll see the dashboard with a
**Run Now** button — click it to trigger an immediate run without waiting for the
cron schedule. Press Ctrl+C to stop.

---

## 6. Check the outputs

### macOS / Linux

```bash
# List what was written next to the media file
ls /tmp/pm-test-media/

# Read the NFO sidecar
cat "/tmp/pm-test-media/Jane Doe with Drama % examplesite - 12345.nfo"

# Read the run summary report
cat /tmp/pm-test-reports/run_latest.txt

# Tail the log for detail
tail -50 /tmp/pm-test-logs/pm.log
```

### Windows (PowerShell)

```powershell
# List output files
Get-ChildItem C:\pm-test-media\

# Read the NFO
Get-Content "C:\pm-test-media\Jane Doe with Drama % examplesite - 12345.nfo"

# Read the run summary
Get-Content C:\pm-test-reports\run_latest.txt

# Tail the log
Get-Content C:\pm-test-logs\pm.log -Tail 50
```

---

## 7. Run the test suite

```bash
# macOS / Linux
source .venv/bin/activate
python3 -m pytest app/tests/ -v

# Windows
.venv\Scripts\activate.bat
python -m pytest app\tests\ -v
```

Run with coverage:

```bash
python3 -m pytest app/tests/ -v --cov=app --cov-report=term-missing
```

---

## 8. Skipping Plex during local testing

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
2. Run: python -m app.main --once --force
3. Check: run_latest.txt  →  see if the file was "updated" or "unmatched"
4. Check: the .nfo file   →  verify the fields look correct
5. Check: pm.log          →  see the full detail trace
6. Repeat
```

---

## Retrying failed files

After a run with errors, re-process only the failed files without re-running everything:

```bash
# macOS / Linux
source .venv/bin/activate
PLEX_URL=... PLEX_TOKEN=... LIBRARY_PATHS=... PLUGIN_DIR=./plugins \
REPORT_PATH=/tmp/pm-test-reports LOG_PATH=/tmp/pm-test-logs \
python3 -m app.main --retry-failed
```

This reads the most recent run report and re-processes every file with status `error`
or `scrape_error`. It sets `--force` automatically so existing sidecars are overwritten.

---

## Windows-specific notes

| Topic | Detail |
|---|---|
| Manual run trigger | `SIGUSR1` (trigger immediate run) and `SIGUSR2` (hot-reload plugins) are Unix signals and are not supported on Windows. Use `--once` locally, or the **Run Now** button / `docker exec` in a Docker/Unraid environment instead. |
| Diagnosing unmatched files | `--list-unmatched` works normally on Windows: `python -m app.main --list-unmatched` |
| Path separators | Use backslashes (`C:\pm-test-media`) for `LIBRARY_PATHS` on Windows when running locally. Inside Docker the paths are always Linux-style (`/media`). |
| Python command | Use `python` (not `python3`) on most Windows installs. |
| Line endings | The app writes NFO files in UTF-8. If you open them in Notepad and see no line breaks, use Notepad++ or VS Code instead. |
| RotatingFileHandler | Python's rotating log handler can occasionally fail to rotate on Windows if another process has the log file open (e.g., VS Code). This is a known Python limitation. If you see a rotation warning in logs, close any open log file viewers and it will recover on the next rotation. |
