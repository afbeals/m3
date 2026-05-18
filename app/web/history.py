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
# Invalidated whenever (newest_mtime, file_count) changes — i.e. a new run completed
# OR an old report was deleted. max_runs is part of the outer key so a call with a
# different limit doesn't return a stale narrower/wider set from a prior call.
_unmatched_cache: dict[tuple, tuple] = {}  # key → ((newest_mtime, file_count), result)


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
            data.pop("files", None)  # strip per-file list to keep memory small
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

    # Determine both the mtime of the newest report file and the total count of
    # report files, using both as the cache invalidation key. Using only the newest
    # mtime would miss deletions: if an old report is deleted (e.g. by the retention
    # sweep or manually) without a new run completing, the cache would return stale
    # aggregates that still include the deleted file's unmatched entries.
    newest_mtime: float | None = None
    file_count: int = 0
    if os.path.isdir(report_path):
        try:
            report_files = sorted(
                (f for f in os.listdir(report_path) if f.startswith("run_") and f.endswith(".json")),
                reverse=True,
            )
            file_count = len(report_files)
            if report_files:
                newest_mtime = os.path.getmtime(os.path.join(report_path, report_files[0]))
        except OSError:
            pass

    invalidation_key = (newest_mtime, file_count)
    cached = _unmatched_cache.get(cache_key)
    if cached is not None and cached[0] == invalidation_key:
        return cached[1]

    summaries = list_runs(report_path)[:max_runs]
    # Need the full file list — re-read only the runs that have unmatched files.
    # Skipping runs whose summary counter is 0 avoids opening JSON files unnecessarily.
    seen: dict[str, dict] = {}  # path -> aggregated entry

    for summary in summaries:
        # Skip runs that explicitly report 0 unmatched files — saves opening the full JSON.
        # Fall through when the key is absent (old-format reports that predate the counter).
        if "unmatched" in summary and summary["unmatched"] == 0:
            continue
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
    _unmatched_cache[cache_key] = (invalidation_key, result)
    return result
