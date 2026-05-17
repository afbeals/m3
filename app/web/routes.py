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
#   POST /trigger/run    — schedule an immediate full run
#   POST /trigger/file   — re-process a single file by path
#   GET  /healthz        — Docker HEALTHCHECK endpoint

from __future__ import annotations

import dataclasses
import inspect
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.web.history import list_runs, get_run, aggregate_unmatched
from app.writers.nfo import write_nfo, write_images
from app.writers.plex import connect_plex, push_to_plex

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health check — used by Docker HEALTHCHECK and Unraid
# ---------------------------------------------------------------------------

@router.get("/healthz")
async def healthz(request: Request, verbose: bool = False):
    """Basic health check used by Docker HEALTHCHECK.

    Add ?verbose=1 for an extended response including last run time and
    hours since the last run — useful for uptime monitors (e.g. Uptime Kuma)
    that can alert when runs stop happening.
    """
    if not verbose:
        return {"status": "ok"}

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
        "last_run": last_run,
        "hours_since_last_run": hours_since,
        "run_count": len(runs),
    })


# ---------------------------------------------------------------------------
# Run status badge (HTMX partial — polled every 2s)
# ---------------------------------------------------------------------------

@router.get("/api/status", response_class=HTMLResponse)
async def run_status(request: Request):
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
async def dashboard(request: Request):
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


@router.get("/runs", response_class=HTMLResponse)
async def run_history(request: Request, page: int = 1):
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
async def run_detail(request: Request, filename: str, status: str = ""):
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

    return request.app.state.templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run, "files": files, "status_filter": status, "status_counts": status_counts},
    )


# ---------------------------------------------------------------------------
# Unmatched digest
# ---------------------------------------------------------------------------

@router.get("/unmatched", response_class=HTMLResponse)
async def unmatched_digest(request: Request, q: str = ""):
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
async def plugin_list(request: Request):
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
async def config_page(request: Request):
    cfg = request.app.state.config
    entries = [
        ("App name",               "APP_NAME",               cfg.app_name),
        ("Plex URL",               "PLEX_URL",               cfg.plex_url),
        ("Plex token",             "PLEX_TOKEN",             "***" if cfg.plex_token else "(not set)"),
        ("Library paths",          "LIBRARY_PATHS",          ", ".join(cfg.library_paths)),
        ("Plugin directory",       "PLUGIN_DIR",             cfg.plugin_dir),
        ("Report path",            "REPORT_PATH",            cfg.report_path),
        ("Log path",               "LOG_PATH",               cfg.log_path),
        ("Run schedule",           "RUN_SCHEDULE",           cfg.run_schedule),
        ("Log level",              "LOG_LEVEL",              cfg.log_level),
        ("Log retention (days)",   "LOG_RETENTION_DAYS",     str(cfg.log_retention_days)),
        ("Report retention (days)","REPORT_RETENTION_DAYS",  str(cfg.report_retention_days)),
        ("Plugin rate limit (s)",  "PLUGIN_RATE_LIMIT_SECS", str(cfg.plugin_rate_limit_secs)),
        ("Notify URL",             "NOTIFY_URL",             cfg.notify_url or "(not set)"),
        ("Web enabled",            "WEB_ENABLED",            str(cfg.web_enabled)),
        ("Web host",               "WEB_HOST",               cfg.web_host),
        ("Web port",               "WEB_PORT",               str(cfg.web_port)),
    ]
    return request.app.state.templates.TemplateResponse(
        request,
        "config.html",
        {"entries": entries},
    )


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------

@router.post("/trigger/run")
async def trigger_run(request: Request):
    """Schedule an immediate run then redirect to the dashboard."""
    scheduler = request.app.state.scheduler
    run_fn = request.app.state.run_fn
    try:
        scheduler.add_job(run_fn, id="manual_trigger", replace_existing=True)
        logger.info("Manual run triggered via web UI")
    except Exception as exc:
        logger.warning("Could not schedule manual run: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return RedirectResponse(url="/", status_code=303)


@router.post("/trigger/file")
async def trigger_file(request: Request):
    """Re-process a single file by path (form field: file_path)."""
    from app.scanner import MediaFile
    from app.router import Router

    form = await request.form()
    file_path = (form.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path is required")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    stem = os.path.splitext(os.path.basename(file_path))[0]
    dirpath = os.path.dirname(file_path)
    nfo_path = os.path.join(dirpath, f"{stem}.nfo")
    media = MediaFile(path=file_path, stem=stem, nfo_path=nfo_path)

    config = request.app.state.config
    registry = request.app.state.plugin_registry
    router_obj = Router(registry)

    def _run_single():
        # Bypasses the normal run() path deliberately: no library scan, no run
        # report, no rate limiting. This is a single user-initiated re-process
        # triggered from the run-detail page — the full pipeline overhead is not
        # needed and would produce misleading report entries.
        plex_server = connect_plex(config.plex_url, config.plex_token)
        parsed, plugin = router_obj.dispatch(media.stem)
        if parsed is None or plugin is None:
            logger.warning("trigger_file: could not route %s", file_path)
            return
        try:
            result = plugin.fetch(parsed)
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

    # daemon=True so the thread doesn't keep the process alive if the container
    # is stopped mid-reprocess; the write is idempotent so an interrupted run is safe.
    thread = threading.Thread(target=_run_single, daemon=True, name="pm-trigger-file")
    thread.start()

    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Log viewer
# ---------------------------------------------------------------------------

_LOG_TAIL_LINES = 200


@router.get("/logs", response_class=HTMLResponse)
async def log_viewer(request: Request):
    log_path = os.path.join(request.app.state.config.log_path, "pm.log")
    lines: list[str] = []
    error: str | None = None
    if not os.path.isfile(log_path):
        error = f"Log file not found: {log_path}"
    else:
        try:
            # deque(maxlen=N) keeps only the last N lines in memory regardless
            # of file size — avoids loading a multi-MB rotated log into RAM.
            with open(log_path) as fh:
                tail: deque[str] = deque(fh, maxlen=_LOG_TAIL_LINES)
            lines = [l.rstrip("\n") for l in tail]
        except OSError as exc:
            error = f"Could not read log file: {exc}"
    return request.app.state.templates.TemplateResponse(
        request,
        "logs.html",
        {"lines": lines, "error": error, "tail": _LOG_TAIL_LINES, "log_path": log_path},
    )
