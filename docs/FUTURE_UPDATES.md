# Future Updates

Planned improvements and enhancements for m3. Items are grouped by theme; each entry covers **why** the improvement matters, **when** it makes sense to tackle it, and **what** the implementation would look like.

---

## SQLite run history database

> **Status: Not yet implemented**

**Why:** The current run history is stored as one JSON file per run in `/config/reports/`. This works fine for browsing recent runs, but has scaling limits:

- The `/files?path=…` history view must read every JSON report on disk to build one file's history — gets noticeably slow once you have hundreds of runs
- Aggregate queries ("show me all files that errored more than 3 times", "which sites have the most scrape errors this month") are impossible without writing a script
- Tracking deleted files — knowing which media files have been processed but no longer exist — requires persistent state that JSON files on disk cannot provide

**When:** Once the report directory starts accumulating enough files that the file history page feels slow (typically 200–500 runs, depending on library size), or when the deleted-file tracking feature below is needed.

**What:** Add a SQLite database at `/config/m3.db` (alongside the existing reports directory). Write to it alongside the existing JSON reports — do not replace the JSON files, which serve as human-readable backups. The schema would be two tables:

```sql
-- One row per completed run
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,        -- matches the existing JSON report filename
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_seconds INTEGER,
    updated INTEGER, renamed INTEGER, skipped INTEGER,
    unmatched INTEGER, add_form INTEGER, scrape_errors INTEGER, errors INTEGER,
    total_scanned INTEGER
);

-- One row per file result within a run
CREATE TABLE file_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    status TEXT NOT NULL,   -- updated / skipped / unmatched / error / scrape_error / renamed / add_form
    message TEXT
);

CREATE INDEX idx_file_results_path ON file_results(path);
CREATE INDEX idx_file_results_status ON file_results(status);
```

The `web/history.py` functions (`list_runs`, `get_run`, `get_file_history`, `aggregate_unmatched`) would query the database instead of scanning JSON files. The JSON files would be kept as a fallback and for human-readable inspection.

**Effort:** Medium. The schema is small and the query patterns are straightforward. The main work is migrating `history.py` and ensuring the write path in `reporter.py` stays atomic.

---

## Deleted file tracking

> **Status: Not yet implemented**

**Why:** When you delete a media file and its sidecars, Plex removes it from its library after a scan + "Empty Trash". But m3 has no awareness of deletions — it never records that a file was removed, and the historical run report entries for that file remain in the dashboard indefinitely (until they age out after `REPORT_RETENTION_DAYS`).

Additionally, orphaned sidecars (`.nfo` + images left on disk after the video is deleted) accumulate silently and take up space.

**When:** After the SQLite database is in place — the clean implementation stores a `deleted_at` timestamp per file in the database rather than scanning the filesystem on every run.

**What:**

1. A `--clean-orphans` CLI flag (and a web dashboard button) that scans library paths for `.nfo` files with no matching video file and deletes them (with a dry-run preview mode)
2. Optionally: a `deleted_at` column on the `file_results` table (or a separate `known_files` table) so the dashboard can distinguish "file not seen since run X" from "file is actively being processed"
3. Dashboard badge on the `/unmatched` page showing orphan count

**Effort:** Medium. The filesystem scan is straightforward; the tricky part is the definition of "orphan" — a `.nfo` with no video in the same directory is always an orphan, but there are edge cases (`.nfo` files written by other tools, multi-disc sets with a shared sidecar). A dry-run preview that lists what would be deleted is essential before any automatic deletion.

---

## Web dashboard authentication

> **Status: Not yet implemented**

**Why:** The web dashboard currently has no authentication. Anyone on the same network who can reach the port can:
- Trigger a full library run (computationally expensive, hammers source APIs)
- Re-process individual files
- View your library paths, plugin list, and configuration (including which sites you have plugins for)

For a LAN-only homelab this is usually acceptable, but if `WEB_HOST=0.0.0.0` and the host is exposed to the internet (or to untrusted LAN segments), it's a real risk.

**When:** If you ever expose the dashboard port outside your home network, or if you want to add the WebUI link in Unraid and share access with other household users without full trust.

**What:** Add an optional HTTP basic auth layer via a `WEB_USERNAME` + `WEB_PASSWORD` environment variable pair. When set, FastAPI middleware rejects unauthenticated requests with a `401 WWW-Authenticate: Basic` response. When not set (the default), the dashboard is open as it is today — no behaviour change for existing users.

Simple FastAPI middleware example:
```python
import secrets
from fastapi import Request
from fastapi.responses import Response

class BasicAuthMiddleware:
    def __init__(self, app, username: str, password: str):
        self._app = app
        self._credentials = (username, password)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            request = Request(scope, receive)
            auth = request.headers.get("Authorization", "")
            # ... validate against self._credentials using secrets.compare_digest
```

**Effort:** Low. The middleware is ~30 lines. The main care needed is using `secrets.compare_digest` (timing-safe comparison) and making the `/healthz` endpoint exempt from auth so Docker health checks keep working.

---

## Scheduled orphan sidecar cleanup

> **Status: Not yet implemented**

**Why:** Over time, deleted or moved media files leave orphan `.nfo`, `-poster.jpg`, and `-fanart.jpg` files on disk. These accumulate silently, waste disk space, and can confuse Plex's scanner.

**When:** After the `--clean-orphans` CLI flag (above) is proven stable.

**What:** Add an optional cleanup pass at the end of each scheduled run (gated by a `CLEANUP_ORPHANS=true` env var, default off) that removes sidecars with no matching video file. Always logs what it removes. Never deletes `.nfo` files written by other tools (Kodi, Jellyfin) — only deletes files whose stem matches a known m3-written pattern (checks for the `<uniqueid type="m3">` element in the NFO before deleting).

**Effort:** Low once `--clean-orphans` exists. Mainly a matter of wiring the env var and calling the same logic from the scheduler path.
