# app/web/history.py
#
# Reads run report JSON files from REPORT_PATH and returns them as dicts
# suitable for use in Jinja2 templates.

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


_MAX_RUNS = 100  # hard cap: prevents unbounded memory use on large history directories

# Cache for aggregate_unmatched: keyed by (report_path, max_runs).
# Invalidated whenever the newest report file's mtime changes (i.e. a new run completed).
_unmatched_cache: dict[tuple, tuple] = {}  # key → (mtime, result)


def list_runs(report_path: str) -> list[dict]:
    """
    Return a list of run summaries from run_*.json files in report_path,
    newest first, capped at _MAX_RUNS entries.

    Each entry is the top-level dict from the JSON file plus a "filename"
    key for linking to the detail view. The per-file "files" array is
    stripped to keep memory usage small — use get_run() for full detail.
    """
    if not os.path.isdir(report_path):
        return []

    runs = []
    filenames = sorted(
        (f for f in os.listdir(report_path) if f.startswith("run_") and f.endswith(".json")),
        reverse=True,
    )
    for fname in filenames[:_MAX_RUNS]:
        fpath = os.path.join(report_path, fname)
        try:
            with open(fpath) as fh:
                data = json.load(fh)
            data["filename"] = fname
            data.pop("files", None)
            runs.append(data)
        except Exception as exc:
            logger.warning("Could not read report file %s: %s", fpath, exc)

    return runs


def get_run(report_path: str, filename: str) -> dict | None:
    """
    Return the full run dict (including per-file results) for a single report
    file identified by its filename (e.g. "run_20250515_030001.json").
    Returns None if the file doesn't exist or can't be read.
    """
    # Sanitise: only allow bare filenames, no path traversal
    if "/" in filename or "\\" in filename or not filename.endswith(".json"):
        return None

    fpath = os.path.join(report_path, filename)
    try:
        with open(fpath) as fh:
            data = json.load(fh)
        data["filename"] = filename
        return data
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Could not read report file %s: %s", fpath, exc)
        return None


def aggregate_unmatched(report_path: str, *, max_runs: int = 30) -> list[dict]:
    """
    Return a deduplicated list of unmatched file paths seen across the most
    recent max_runs reports, sorted by occurrence count (desc) then last_seen (desc).

    Each entry: {"path": str, "count": int, "last_seen": str | None, "last_message": str}

    Results are cached in memory and invalidated when the newest report file's
    mtime changes (indicating a new run completed), so repeated page loads on an
    idle system don't re-open up to max_runs JSON files each time.
    """
    cache_key = (report_path, max_runs)

    # Determine the mtime of the newest report file to use as a cache key
    newest_mtime: float | None = None
    if os.path.isdir(report_path):
        try:
            newest = next(
                (f for f in sorted(os.listdir(report_path), reverse=True)
                 if f.startswith("run_") and f.endswith(".json")),
                None,
            )
            if newest:
                newest_mtime = os.path.getmtime(os.path.join(report_path, newest))
        except OSError:
            pass

    cached = _unmatched_cache.get(cache_key)
    if cached is not None and cached[0] == newest_mtime:
        return cached[1]

    summaries = list_runs(report_path)[:max_runs]
    # Need the full file list — re-read only the runs that have unmatched files.
    seen: dict[str, dict] = {}  # path -> aggregated entry

    for summary in summaries:
        filename = summary.get("filename", "")
        if not filename:
            continue
        run = get_run(report_path, filename)
        if run is None:
            continue
        for f in run.get("files", []):
            if f.get("status") != "unmatched":
                continue
            path = f.get("path", "")
            if not path:
                continue
            if path not in seen:
                seen[path] = {
                    "path": path,
                    "count": 0,
                    "last_seen": None,
                    "last_message": f.get("message", ""),
                }
            entry = seen[path]
            entry["count"] += 1
            run_started = run.get("started_at")
            if run_started and (entry["last_seen"] is None or run_started > entry["last_seen"]):
                entry["last_seen"] = run_started
                entry["last_message"] = f.get("message", "")

    result = sorted(seen.values(), key=lambda e: (-e["count"], e["last_seen"] or ""))
    _unmatched_cache[cache_key] = (newest_mtime, result)
    return result
