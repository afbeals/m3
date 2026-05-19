# app/web/routes.py
#
# FastAPI route handlers for the pm web dashboard.
#
# Routes:
#   GET  /               — latest run summary + next scheduled time
#   GET  /runs           — paginated run history
#   GET  /runs/{ts}      — per-run file detail with status filter
#   GET  /unmatched      — cross-run unmatched file digest
#   GET  /plugins        — loaded plugin list
#   GET  /config         — active env var values (token masked)
#   GET  /api/status     — HTMX-polled run-in-progress badge
#   GET  /logs           — last N lines of pm.log
#   GET  /files          — per-file processing history across all runs
#   POST /trigger/run    — schedule an immediate full run
#   POST /trigger/file   — re-process a single file by path
#   GET  /healthz        — Docker HEALTHCHECK endpoint; ?check=plex for live Plex probe

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import logging
import os
import platform
import signal
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.utils import call_with_timeout
from app.web.history import list_runs, get_run, aggregate_unmatched, get_file_history
from app.writers.nfo import write_nfo, write_images
from app.writers.plex import connect_plex, push_to_plex

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-path lock set for /trigger/file coalescing.
# Prevents a user from spamming the ↺ button and spawning duplicate threads
# for the same file that would race on NFO writes and spam the source API.
# Entries are added on first access and never removed: the set of media files
# in a library is bounded, each Lock object is <100 bytes, so growth is
# negligible. Removal would require a separate GC pass and add complexity
# without meaningful benefit.
_file_trigger_locks: dict[str, threading.Lock] = {}
_file_trigger_locks_guard = threading.Lock()


def _get_file_lock(path: str) -> threading.Lock:
    with _file_trigger_locks_guard:
        if path not in _file_trigger_locks:
            _file_trigger_locks[path] = threading.Lock()
        return _file_trigger_locks[path]


# ---------------------------------------------------------------------------
# Health check — used by Docker HEALTHCHECK and Unraid
# ---------------------------------------------------------------------------

@router.get("/healthz")
def healthz(request: Request, verbose: bool = False, check: str = ""):
    """Basic health check used by Docker HEALTHCHECK.

    Add ?verbose=1 for an extended response including last run time and
    hours since the last run — useful for uptime monitors (e.g. Uptime Kuma)
    that can alert when runs stop happening.
    """
    from app import __version__

    # ?check=plex performs a live Plex reachability probe.
    # Returns {"plex": "ok"} or {"plex": "unreachable", "detail": "..."}.
    # Kept separate from the main healthz response so uptime monitors can
    # alert on Plex being down without flagging the pm process itself as unhealthy.
    if check == "plex":
        from app.writers.plex import connect_plex
        import concurrent.futures
        config = request.app.state.config
        # Run connect_plex in a thread with a hard 5-second deadline so a slow or
        # hung Plex server can't block the async event loop long enough for Docker's
        # HEALTHCHECK (--timeout=5s) to declare the container unhealthy.
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(connect_plex, config.plex_url, config.plex_token)
                server = future.result(timeout=5)
            if server is not None:
                return JSONResponse({"plex": "ok", "url": config.plex_url})
            else:
                return JSONResponse(
                    {"plex": "unreachable", "detail": "connection returned None"},
                    status_code=503,
                )
        except concurrent.futures.TimeoutError:
            return JSONResponse(
                {"plex": "unreachable", "detail": "connection timed out after 5s"},
                status_code=503,
            )
        except Exception as exc:
            return JSONResponse(
                {"plex": "unreachable", "detail": str(exc)},
                status_code=503,
            )

    if not verbose:
        return {"status": "ok", "version": __version__}

    runs = list_runs(request.app.state.config.report_path)
    last_run = runs[0].get("started_at") if runs else None
    hours_since: float | None = None
    if last_run:
        try:
            last_dt = datetime.fromisoformat(last_run)
            # Make timezone-aware if naive (reports use local time)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            hours_since = round(
                (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600, 1
            )
        except ValueError:
            pass

    return JSONResponse({
        "status": "ok",
        "version": __version__,
        "last_run": last_run,
        "hours_since_last_run": hours_since,
        "run_count": len(runs),
    })


# ---------------------------------------------------------------------------
# Run status badge (HTMX partial — polled every 2s)
# ---------------------------------------------------------------------------

@router.get("/api/status", response_class=HTMLResponse)
def run_status(request: Request):
    run_state = request.app.state.run_state
    if run_state is not None:
        snap = run_state.snapshot()
    else:
        snap = {"running": False, "elapsed_seconds": None}
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/status_badge.html",
        {"snap": snap},
    )


# ---------------------------------------------------------------------------
# Dashboard — latest run
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    runs = list_runs(request.app.state.config.report_path)
    latest = runs[0] if runs else None

    scheduler = request.app.state.scheduler
    next_run = None
    if scheduler is not None:
        job = scheduler.get_job("scheduled_run")
        if job is not None:
            next_run = job.next_run_time

    return request.app.state.templates.TemplateResponse(
        request,
        "latest.html",
        {"latest": latest, "next_run": next_run},
    )


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------

_RUNS_PAGE_SIZE = 25
_RUN_DETAIL_PAGE_SIZE = 200


@router.get("/runs", response_class=HTMLResponse)
def run_history(request: Request, page: int = 1):
    all_runs = list_runs(request.app.state.config.report_path)
    total = len(all_runs)
    page = max(1, page)
    start = (page - 1) * _RUNS_PAGE_SIZE
    end = start + _RUNS_PAGE_SIZE
    runs = all_runs[start:end]
    total_pages = max(1, (total + _RUNS_PAGE_SIZE - 1) // _RUNS_PAGE_SIZE)
    return request.app.state.templates.TemplateResponse(
        request,
        "runs.html",
        {
            "runs": runs,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@router.get("/runs/{filename}", response_class=HTMLResponse)
def run_detail(request: Request, filename: str, status: str = "", page: int = 1):
    run = get_run(request.app.state.config.report_path, filename)
    if run is None:
        return request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            {"message": f"Run report not found: {filename}"},
            status_code=404,
        )

    all_files = run.get("files", [])

    # Count per status for the filter tab bar
    status_counts: dict[str, int] = {}
    for f in all_files:
        s = f.get("status", "")
        status_counts[s] = status_counts.get(s, 0) + 1

    files = [f for f in all_files if f.get("status") == status] if status else all_files

    total_files = len(files)
    page = max(1, page)
    total_pages = max(1, (total_files + _RUN_DETAIL_PAGE_SIZE - 1) // _RUN_DETAIL_PAGE_SIZE)
    page = min(page, total_pages)
    files = files[(page - 1) * _RUN_DETAIL_PAGE_SIZE : page * _RUN_DETAIL_PAGE_SIZE]

    return request.app.state.templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "run": run,
            "files": files,
            "all_files_count": len(all_files),
            "status_filter": status,
            "status_counts": status_counts,
            "page": page,
            "total_pages": total_pages,
            "total_files": total_files,
        },
    )


# ---------------------------------------------------------------------------
# Unmatched digest
# ---------------------------------------------------------------------------

@router.get("/unmatched", response_class=HTMLResponse)
def unmatched_digest(request: Request, q: str = ""):
    entries = aggregate_unmatched(request.app.state.config.report_path)
    query = q.strip().lower()
    if query:
        entries = [e for e in entries if query in e["path"].lower()]
    return request.app.state.templates.TemplateResponse(
        request,
        "unmatched.html",
        {"entries": entries, "q": q},
    )


# ---------------------------------------------------------------------------
# Plugin list
# ---------------------------------------------------------------------------

@router.get("/plugins", response_class=HTMLResponse)
def plugin_list(request: Request):
    registry = request.app.state.plugin_registry
    plugins = []
    seen = set()
    for key, instance in registry.items():
        cls = type(instance)
        if id(instance) in seen:
            continue
        seen.add(id(instance))
        plugins.append({
            "site_id": cls.site_id,
            "aliases": cls.aliases,
            "class_name": cls.__name__,
            "source_file": inspect.getfile(cls),
        })

    return request.app.state.templates.TemplateResponse(
        request,
        "plugins.html",
        {"plugins": plugins},
    )


# ---------------------------------------------------------------------------
# Config page
# ---------------------------------------------------------------------------

@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request):
    cfg = request.app.state.config
    # Build per-path existence annotations as structured data so the template
    # can render missing mounts with a visible badge rather than plain dim text.
    lib_paths_annotated = [
        {"path": p, "exists": os.path.isdir(p)}
        for p in cfg.library_paths
    ]
    entries = [
        ("App name",               "APP_NAME",               cfg.app_name,      None),
        ("Plex URL",               "PLEX_URL",               cfg.plex_url,       None),
        ("Plex token",             "PLEX_TOKEN",             "***" if cfg.plex_token else "(not set)", None),
        ("Library paths",          "LIBRARY_PATHS",          None,               lib_paths_annotated),
        ("Plugin directory",       "PLUGIN_DIR",             cfg.plugin_dir,     None),
        ("Report path",            "REPORT_PATH",            cfg.report_path,    None),
        ("Log path",               "LOG_PATH",               cfg.log_path,       None),
        ("Run schedule",           "RUN_SCHEDULE",           cfg.run_schedule,   None),
        ("Log level",              "LOG_LEVEL",              cfg.log_level,      None),
        ("Log retention (days)",   "LOG_RETENTION_DAYS",     str(cfg.log_retention_days), None),
        ("Report retention (days)","REPORT_RETENTION_DAYS",  str(cfg.report_retention_days), None),
        ("Plugin rate limit (s)",  "PLUGIN_RATE_LIMIT_SECS", str(cfg.plugin_rate_limit_secs), None),
        ("Plugin fetch timeout (s)","PLUGIN_FETCH_TIMEOUT_SECS", str(cfg.plugin_fetch_timeout_secs), None),
        ("Exclude patterns",       "LIBRARY_EXCLUDE_PATTERNS",
         ", ".join(cfg.library_exclude_patterns) if cfg.library_exclude_patterns else "(none)", None),
        ("Notify URL",             "NOTIFY_URL",             cfg.notify_url or "(not set)", None),
        ("Notify min errors",      "NOTIFY_MIN_ERRORS",      str(cfg.notify_min_errors), None),
        ("Web enabled",            "WEB_ENABLED",            str(cfg.web_enabled), None),
        ("Web host",               "WEB_HOST",               cfg.web_host,       None),
        ("Web port",               "WEB_PORT",               str(cfg.web_port),  None),
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "config.html",
        {"entries": entries},
    )


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------

@router.post("/trigger/reload")
def trigger_reload(request: Request):
    """Reload plugins in-place by sending SIGUSR2 to the current process (Unix only).

    On Windows this is a no-op — the user must restart the container instead.
    Redirects to /plugins so the user sees the refreshed plugin list.
    """
    if platform.system() != "Windows":
        try:
            os.kill(os.getpid(), signal.SIGUSR2)
            logger.info("Plugin reload triggered via web UI")
        except Exception as exc:
            logger.warning("Could not send SIGUSR2 for plugin reload: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
    else:
        logger.info("Plugin reload requested on Windows — restart required instead")

    return RedirectResponse(url="/plugins", status_code=303)


@router.post("/trigger/run")
def trigger_run(request: Request):
    """Schedule an immediate run then redirect to the dashboard."""
    run_state = request.app.state.run_state
    if run_state is not None and run_state.snapshot()["running"]:
        # Don't queue a second run; redirect back with a flash message so the
        # user gets visible feedback instead of a silent no-op.
        return RedirectResponse(url="/?msg=already_running", status_code=303)
    scheduler = request.app.state.scheduler
    run_fn = request.app.state.run_fn
    try:
        scheduler.add_job(run_fn, id="manual_trigger", replace_existing=True)
        logger.info("Manual run triggered via web UI")
    except Exception as exc:
        logger.warning("Could not schedule manual run: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


def _htmx_error(request: Request, message: str, status_code: int = 400) -> HTMLResponse:
    """Return a visible inline error fragment for HTMX callers, or raise HTTPException
    for plain-form callers.

    HTMX ignores 4xx responses by default (no swap fires), so the user sees nothing.
    Returning 200 with an error badge lets the target cell show the problem inline.
    """
    if request.headers.get("HX-Request"):
        html = (
            f'<span style="color:var(--red); font-size:0.75rem;" title="HTTP {status_code}">'
            f'✗ {message}</span>'
        )
        return HTMLResponse(html, status_code=200)
    raise HTTPException(status_code=status_code, detail=message)


@router.post("/trigger/file")
async def trigger_file(request: Request):
    """Re-process a single file by path (form field: file_path)."""
    from app.scanner import MediaFile

    form = await request.form()
    file_path = (form.get("file_path") or "").strip()
    if not file_path:
        return _htmx_error(request, "file_path is required")
    if not os.path.isfile(file_path):
        return _htmx_error(request, f"File not found: {file_path}", status_code=404)

    # Security: only allow re-processing files that live inside a configured
    # library path. Prevents the form from being used to trigger metadata
    # writes on arbitrary system files outside the media directories.
    config = request.app.state.config
    real_path = Path(os.path.realpath(file_path))
    # is_relative_to() proves the resolved path is strictly inside the library root,
    # blocking both path-traversal attempts (/media/../etc/passwd) and requests
    # for files in sibling directories (/media2/other.mp4 when only /media is configured).
    # Path.is_relative_to() (Python 3.9+) is clearer and handles edge cases
    # (e.g. /media2 not being considered "inside" /media) better than commonpath.
    allowed = any(
        real_path.is_relative_to(os.path.realpath(lib))
        for lib in config.library_paths
    )
    if not allowed:
        return _htmx_error(
            request,
            f"File is not inside a configured library path: {file_path}",
        )

    # Coalesce concurrent requests for the same file path: if a thread is already
    # processing this file, return 409 rather than spawning a duplicate thread that
    # would race on NFO writes and spam the source API.
    file_lock = _get_file_lock(str(real_path))
    if not file_lock.acquire(blocking=False):
        return _htmx_error(request, f"Already processing: {file_path}", status_code=409)

    stem = os.path.splitext(os.path.basename(file_path))[0]
    dirpath = os.path.dirname(file_path)
    nfo_path = os.path.join(dirpath, f"{stem}.nfo")
    media = MediaFile(path=file_path, stem=stem, nfo_path=nfo_path)

    router_obj = request.app.state.plugin_router

    def _run_single():
        try:
            # Bypasses the normal run() path deliberately: no library scan, no run
            # report, no rate limiting. This is a single user-initiated re-process
            # triggered from the run-detail page — the full pipeline overhead is not
            # needed and would produce misleading report entries.
            plex_server = connect_plex(config.plex_url, config.plex_token)
            if plex_server is None:
                logger.warning("trigger_file: Plex unreachable — metadata will be written to sidecar only for %s", file_path)
            parsed, plugin = router_obj.dispatch(media.stem)
            if parsed is None or plugin is None:
                logger.warning("trigger_file: could not route %s", file_path)
                return
            try:
                timeout = config.plugin_fetch_timeout_secs
                result = call_with_timeout(
                    lambda: plugin.fetch(parsed),
                    timeout,
                    description="trigger_file plugin fetch",
                )
            except Exception as exc:
                logger.warning("trigger_file: plugin error for %s: %s", file_path, exc)
                return
            if result is None:
                logger.warning("trigger_file: plugin returned no result for %s", file_path)
                return
            try:
                write_nfo(media, result)
                write_images(media, result)
            except Exception as exc:
                logger.warning("trigger_file: write error for %s: %s", file_path, exc)
                return
            if plex_server is not None:
                try:
                    push_to_plex(plex_server, file_path, result)
                except Exception as exc:
                    logger.warning("trigger_file: Plex push error for %s: %s", file_path, exc)
            logger.info("trigger_file: reprocessed %s", file_path)
        finally:
            # Always release the per-path lock so subsequent requests can proceed.
            file_lock.release()

    # daemon=True so the thread doesn't keep the process alive if the container
    # is stopped mid-reprocess; the write is idempotent so an interrupted run is safe.
    thread = threading.Thread(target=_run_single, daemon=True, name="pm-trigger-file")
    thread.start()

    # HTMX inline retry (HX-Request header present): return a "queued" badge
    # that replaces just the action cell on the row, then fades out. Regular
    # form POSTs (no HTMX) fall back to the full-page redirect so the behaviour
    # is unchanged for non-JS environments.
    if request.headers.get("HX-Request"):
        slot_id = hashlib.md5(file_path.encode()).hexdigest()[:8]
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/queued_badge.html",
            {"slot_id": slot_id},
        )

    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# File history
# ---------------------------------------------------------------------------

@router.get("/files", response_class=HTMLResponse)
def file_history(request: Request, path: str = ""):
    """Show the processing history of a single file across all runs."""
    path = path.strip()
    history = []
    if path:
        history = get_file_history(request.app.state.config.report_path, path)
    return request.app.state.templates.TemplateResponse(
        request,
        "file_history.html",
        {"file_path": path, "history": history},
    )


# ---------------------------------------------------------------------------
# Log viewer
# ---------------------------------------------------------------------------

_LOG_TAIL_LINES = 200
_LOG_TAIL_MAX = 2000


@router.get("/logs", response_class=HTMLResponse)
def log_viewer(request: Request, tail: int = _LOG_TAIL_LINES):
    tail = max(1, min(tail, _LOG_TAIL_MAX))
    log_path = os.path.join(request.app.state.config.log_path, "pm.log")
    lines: list[str] = []
    error: str | None = None
    try:
        if not os.path.isfile(log_path):
            error = f"Log file not found: {log_path}"
        else:
            # deque(maxlen=N) keeps only the last N lines in memory regardless
            # of file size — avoids loading a multi-MB rotated log into RAM.
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail_buf: deque[str] = deque(fh, maxlen=tail)
            lines = [line.rstrip("\n") for line in tail_buf]
    except OSError as exc:
        error = f"Could not read log file: {exc}"
    return request.app.state.templates.TemplateResponse(
        request,
        "logs.html",
        {"lines": lines, "error": error, "tail": tail, "log_path": log_path},
    )
