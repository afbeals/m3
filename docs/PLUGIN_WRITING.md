# Writing a pm Plugin

This guide walks through writing a new metadata plugin from scratch — both the
common JSON API case and the HTML scraping case.

---

## Overview

A plugin is a single `.py` file dropped into the `plugins/` directory (mounted
at `/plugins` in the container). pm discovers and loads it automatically on
startup — no core code changes needed.

Each plugin:
1. Subclasses `MetadataPlugin`
2. Declares a `site_id` that matches the site token in filenames
3. Implements `fetch()` to call its API and return a `MetadataResult`

---

## Quickstart (5-minute version)

```bash
# Copy the reference implementation
cp plugins/example_plugin.py plugins/mysite.py
```

Edit `plugins/mysite.py`:
1. Change `site_id = "mysite"` to your token
2. Set `BASE_URL` to your API's base URL
3. Set the env var name in `_api_get()` (e.g. `MYSITE_API_KEY`)
4. Update `_to_result()` to map your API's response fields

Restart the container:
```bash
docker restart pm
```

Your plugin is live. Test it with a one-shot run:
```bash
docker exec pm python -m app.main --once --force
```

---

## Full Step-by-Step Guide

### Step 1 — Decide your `site_id`

`site_id` is the token users put in their filenames after the `%` separator:

```
Jane Doe with Drama % mysite - 12345.mp4
                       ^^^^^^
```

It must be lowercase, no spaces, URL-friendly. You can also add short aliases:

```python
class MyPlugin(MetadataPlugin):
    site_id = "mysite"
    aliases = ["MS"]   # % MS - 12345 also routes here
```

### Step 2 — Understand ParsedFilename

`fetch()` receives a `ParsedFilename` dataclass. The fields most relevant to
plugins:

| Field | Type | When populated | Description |
|---|---|---|---|
| `match_subtype` | `"exact"` / `"enhanced"` / `"limited"` | always | Which search strategy to use |
| `scene_id` | `str \| None` | exact, enhanced | Numeric or slug scene ID |
| `direct_url` | `str \| None` | exact | URL path slug (e.g. `eager-hands`) |
| `title` | `str \| None` | enhanced, limited | Text title from filename |
| `actors` | `list[str]` | always | Actors from left side of filename |
| `genres` | `list[str]` | always | Genres from "with ..." clause |
| `date` | `str \| None` | enhanced | Release date (YYYY-MM-DD) |
| `studio_id` | `str \| None` | enhanced | Studio numeric ID (multi-studio sites) |
| `actress_id` | `str \| None` | enhanced | Actress page ID |
| `raw_match_payload` | `str \| None` | always | Raw text after `%` for custom parsing |

**Match subtypes:**

- **exact** — filename contains a scene ID or URL slug; call the detail API endpoint directly
- **enhanced** — filename has a date, scene ID, title, and/or actors; use all of them to search
- **limited** — filename has a title and/or actors but no date or ID; use what's available
- **add** — Manual Add form; not routed to plugins (handled by the add writer)

### Step 3 — Implement fetch()

Route by subtype, then call the appropriate helper:

```python
def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
    subtype = parsed.match_subtype
    if subtype == "exact":
        return self._fetch_by_id(parsed)
    elif subtype == "enhanced":
        return self._fetch_enhanced(parsed)
    elif subtype == "limited":
        return self._fetch_limited(parsed)
    return None
```

Return `None` if the API returns no usable record. Do **not** raise — returning
`None` is the contract for "not found". Only raise (or let exceptions propagate)
for actual errors (network failure, unexpected API response shape), which will be
recorded as `status="error"` in the run report.

### Step 4 — Build MetadataResult

`MetadataResult` is a dataclass. `title` is the only required field; all others
are optional:

```python
from app.plugins.base import MetadataResult

return MetadataResult(
    title=data.get("title") or "Unknown Title",   # required, must be non-empty
    summary=data.get("description"),
    rating=data.get("rating"),         # float, 0.0–10.0
    year=data.get("year"),             # int
    content_rating=data.get("rating_code"),       # e.g. "NR", "R"
    genres=data.get("genres", []),
    tags=data.get("tags", []),
    labels=data.get("labels", []),
    actors=data.get("performers", []),
    poster_url=data.get("poster_url"),
    fanart_url=data.get("fanart_url"),
    source_url=data.get("url"),
    source_id=str(data["id"]) if data.get("id") is not None else None,
)
```

`__post_init__` validates the result — a missing or blank `title` raises
`ValueError` immediately so you know your plugin has a mapping problem.

### Step 5 — Reading API keys

Always read API keys inside the method that needs them — **not** at module level:

```python
# CORRECT: read at call time so key rotation takes effect without restart
def _api_get(self, path: str):
    api_key = os.environ.get("MYSITE_API_KEY", "")
    if not api_key:
        logger.error("[mysite] MYSITE_API_KEY is not set")
        return None
    ...

# WRONG: captured once at import time, stale after key rotation
API_KEY = os.environ.get("MYSITE_API_KEY", "")
```

---

## JSON API Plugin (full example)

See `plugins/example_plugin.py` — it is fully commented and covers all three
match subtypes. The structure is:

```
ExampleSitePlugin
├── fetch()            — dispatch by match_subtype
├── _fetch_by_id()     — exact match: GET /scenes/{id}
├── _fetch_enhanced()  — search with title+actor+date+scene_id params
├── _fetch_limited()   — search with title and/or actor only
├── _api_get()         — shared httpx GET wrapper; reads API key from env
└── _to_result()       — maps API response dict → MetadataResult
```

---

## HTML Scraping Plugin (full example)

Use this when the site has no JSON API — you scrape the HTML directly.

See `plugins/example_html_plugin.py` for a complete reference. Key differences
from the JSON API plugin:

### Import the scraping helpers

```python
from app.scrape import fetch_html, ScrapeError, SelectorMissingError
from bs4 import BeautifulSoup
```

### Call fetch_html

`fetch_html(url)` handles retries, browser-like User-Agent headers, and content
validation. It raises `ScrapeError` on network or HTTP errors — let these
propagate so pm records them as `status="scrape_error"` (distinct from
`status="error"`, which means your plugin code crashed).

```python
html = fetch_html(f"https://mysite.com/scenes/{scene_id}")
soup = BeautifulSoup(html, "html.parser")
```

### Use SelectorMissingError for broken markup

When a CSS selector that should always be present returns `None`, raise
`SelectorMissingError` — **not** a generic exception:

```python
title_el = soup.select_one("h1.scene-title")
if not title_el:
    raise SelectorMissingError("title not found at h1.scene-title")
```

This records `status="scrape_error"` with the broken selector in the run
report, so you can immediately see "the site changed its markup" rather than
hunting for a generic crash.

If a selector is **optional** (e.g. fanart isn't always present), don't raise —
just skip:

```python
fanart_el = soup.select_one("img.fanart")
fanart_url = fanart_el["src"] if fanart_el else None
```

---

## Testing Your Plugin

### 0. Check routing (fastest — no API call)

Before writing a single line of plugin code, verify your filenames route to your plugin:

```bash
# List every file in your library that currently has no plugin — shows you exactly
# what you need to cover before your plugin can process anything.
docker exec pm python -m app.main --list-unmatched

# Or locally after activating the venv:
PLEX_URL=x PLEX_TOKEN=x LIBRARY_PATHS=/tmp/pm-test-media PLUGIN_DIR=./plugins \
REPORT_PATH=/tmp NOTIFY_URL= LOG_PATH=/tmp \
python3 -m app.main --list-unmatched
```

Once your plugin is in place, run `--list-unmatched` again — your files should
disappear from the list (they'll now be routed to your plugin).

### 1. Test parsing first (no API needed)

```bash
source .venv/bin/activate
python3 - <<'EOF'
from app.parser import parse

stems = [
    "Jane Doe with Drama % mysite - 12345",
    "Jane Doe with Drama % mysite - 2024-01-15 - 12345 - My Scene",
]
for stem in stems:
    r = parse(stem)
    print(f"subtype={r.match_subtype} site={r.site} scene_id={r.scene_id} title={r.title!r}")
EOF
```

### 2. Test the plugin directly (no full run needed)

```bash
python3 - <<'EOF'
import os
os.environ["MYSITE_API_KEY"] = "your-real-key"

from app.parser import parse
from plugins.mysite import MySitePlugin   # adjust to your actual class name

plugin = MySitePlugin()
parsed = parse("Jane Doe with Drama % mysite - 12345")
result = plugin.fetch(parsed)

if result:
    print("title:", result.title)
    print("actors:", result.actors)
    print("poster_url:", result.poster_url)
else:
    print("result: None (not found)")
EOF
```

### 3. Run a full one-shot pass

```bash
PLEX_URL=... PLEX_TOKEN=... LIBRARY_PATHS=/path/to/media \
PLUGIN_DIR=./plugins REPORT_PATH=/tmp/pm-reports LOG_PATH=/tmp/pm-logs \
MYSITE_API_KEY=your-key \
python3 -m app.main --once --force
```

Check the output:
```bash
cat /tmp/pm-reports/run_latest.txt
```

Look for your file under `updated` (success) or `error`/`scrape_error` (check
the message column).

### 4. Unit tests

Write a test file in `app/tests/test_plugin_mysite.py`. Mock the HTTP calls
with `httpx`'s built-in `MockTransport`, or use `unittest.mock.patch` to mock
`_api_get`:

```python
from unittest.mock import patch
from app.parser import parse
from plugins.mysite import MySitePlugin

def test_fetch_by_id_returns_title():
    plugin = MySitePlugin()
    parsed = parse("Jane Doe with Drama % mysite - 12345")
    fake_data = {"id": 12345, "title": "My Scene", "performers": ["Jane Doe"]}

    with patch.object(plugin, "_api_get", return_value=fake_data):
        result = plugin.fetch(parsed)

    assert result is not None
    assert result.title == "My Scene"
    assert "Jane Doe" in result.actors
```

Run it:
```bash
python3 -m pytest app/tests/test_plugin_mysite.py -v
```

---

## Troubleshooting Plugins

| Symptom | Likely cause | Fix |
|---|---|---|
| Plugin not loaded at startup | File not in plugin dir, or has a Python syntax error | Check `docker logs pm` for an import error traceback |
| Files route to "unmatched" | `site_id` doesn't match the filename token | Verify `site_id` exactly matches what's after `%` in filenames |
| `fetch()` called but returns None | API returned empty results | Add logging in `_fetch_*` methods; run with `LOG_LEVEL=DEBUG` |
| `status=error` in run report | Unhandled exception in plugin | Check `docker logs pm` for the full traceback |
| `status=scrape_error` in run report | `ScrapeError` or `SelectorMissingError` raised | HTTP error from the site, or a CSS selector broke after a site redesign |
| API key not found | Key not set as env var on the container | Add the key as an environment variable in the Docker/Unraid config |

---

## Plugin Checklist

Before deploying a plugin:

- [ ] `site_id` is lowercase and matches the token you'll use in filenames
- [ ] API key is read inside the method, not at module level
- [ ] All three subtypes handled (`exact`, `enhanced`, `limited`) or explicitly skipped with a log warning
- [ ] `fetch()` returns `None` for "not found" rather than raising
- [ ] HTML selectors use `SelectorMissingError` for required elements
- [ ] `_to_result()` uses `or "Unknown Title"` so `title` is never empty
- [ ] Plugin tested locally with `--once --force` before deploying
