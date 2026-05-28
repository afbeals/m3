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

# Cache for list_runs: keyed by report_path.
# Invalidated when (newest_mtime, file_count) changes.
_list_runs_cache: dict[str, tuple] = {}  # report_path → ((newest_mtime, file_count), result)

# Cache for aggregate_unmatched: keyed by (report_path, max_runs).
# Invalidated whenever (newest_mtime, file_count) changes — i.e. a new run completed
# OR an old report was deleted. max_runs is part of the outer key so a call with a
# different limit doesn't return a stale narrower/wider set from a prior call.
_unmatched_cache: dict[tuple, tuple] = {}  # key → ((newest_mtime, file_count), result)


def _run_invalidation_key(report_path: str) -> tuple[float | None, int]:
    """Return (newest_mtime, file_count) for run_*.json files in report_path."""
    newest_mtime: float | None = None
    file_count = 0
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
    return (newest_mtime, file_count)


def list_runs(report_path: str) -> list[dict]:
    """
    Return a list of run summaries from run_*.json files in report_path,
    newest first, capped at _MAX_RUNS entries.

    Each entry is the top-level dict from the JSON file plus a "filename"
    key for linking to the detail view. The per-file "files" array is
    stripped to keep memory usage small — use get_run() for full detail.

    Results are cached and invalidated when (newest_mtime, file_count) changes
    so repeated dashboard loads on an idle system don't re-parse all JSON files.
    """
    if not os.path.isdir(report_path):
        return []

    invalidation_key = _run_invalidation_key(report_path)
    cached = _list_runs_cache.get(report_path)
    if cached is not None and cached[0] == invalidation_key:
        return cached[1]

    runs = []
    filenames = sorted(
        (f for f in os.listdir(report_path) if f.startswith("run_") and f.endswith(".json")),
        reverse=True,
    )
    for fname in filenames[:_MAX_RUNS]:
        fpath = os.path.join(report_path, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                data = json.load(fh)
            data["filename"] = fname
            data.pop("files", None)  # strip per-file list to keep memory small
            runs.append(data)
        except Exception as exc:
            logger.warning("Could not read report file %s: %s", fpath, exc)

    _list_runs_cache[report_path] = (invalidation_key, runs)
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
        with open(fpath, encoding="utf-8") as fh:
            data = json.load(fh)
        data["filename"] = filename
        return data
    except FileNotFoundError:
        # Normal when the retention sweep deletes a file between list_runs and get_run
        logger.debug("Report file no longer present: %s", fpath)
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
    invalidation_key = _run_invalidation_key(report_path)
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


def get_file_history(report_path: str, file_path: str, *, max_runs: int = _MAX_RUNS) -> list[dict]:
    """
    Return a list of run entries where file_path appeared, newest first.

    Each entry: {"run_filename": str, "started_at": str, "status": str, "message": str,
                 "trigger": bool}

    Scans both regular run reports (run_*.json) and manual trigger records
    (trigger_*.json written by /trigger/file). Used by the /files page to show
    a single file's full processing history — including manual retriggering — across runs.
    """
    history = []

    # Use list_runs() for run summaries (cache hit on repeated calls).
    # list_runs() strips "files", so we re-read via get_run() only for runs
    # that actually contain this file path. We use the summaries only to get filenames.
    summaries = list_runs(report_path)
    for summary in summaries[:max_runs]:
        fname = summary.get("filename", "")
        if not fname:
            continue
        run = get_run(report_path, fname)
        if run is None:
            continue
        for f in run.get("files", []):
            if f.get("path") == file_path:
                history.append({
                    "run_filename": fname,
                    "started_at": run.get("started_at", ""),
                    "status": f.get("status", ""),
                    "message": f.get("message", ""),
                    "trigger": False,
                })
                break  # only one entry per run

    # Trigger records are not cached (infrequent and small); read directory directly.
    try:
        raw = os.listdir(report_path) if os.path.isdir(report_path) else []
    except OSError:
        logger.warning("Could not list report directory: %s", report_path)
        raw = []

    trigger_files = sorted(
        (f for f in raw if f.startswith("trigger_") and f.endswith(".json")),
        reverse=True,
    )

    for fname in trigger_files[:max_runs]:
        fpath = os.path.join(report_path, fname)
        try:
            with open(fpath, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.warning("Could not read trigger record %s: %s", fpath, exc)
            continue
        for f in data.get("files", []):
            if f.get("path") == file_path:
                history.append({
                    "run_filename": fname,
                    "started_at": data.get("started_at", ""),
                    "status": f.get("status", ""),
                    "message": f.get("message", ""),
                    "trigger": True,
                })
                break

    history.sort(key=lambda e: e.get("started_at", ""), reverse=True)
    return history
