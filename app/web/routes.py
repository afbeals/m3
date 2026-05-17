# app/web/routes.py
#
# FastAPI route handlers for the pm web dashboard.

from __future__ import annotations

import logging
import inspect
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.web.history import list_runs, get_run, aggregate_unmatched

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health check — used by Docker HEALTHCHECK and Unraid
# ---------------------------------------------------------------------------

@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


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

@router.get("/runs", response_class=HTMLResponse)
async def run_history(request: Request):
    runs = list_runs(request.app.state.config.report_path)
    return request.app.state.templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": runs},
    )


@router.get("/runs/{filename}", response_class=HTMLResponse)
async def run_detail(request: Request, filename: str, status: str = ""):
    run = get_run(request.app.state.config.report_path, filename)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    files = run.get("files", [])
    if status:
        files = [f for f in files if f.get("status") == status]

    return request.app.state.templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run, "files": files, "status_filter": status},
    )


# ---------------------------------------------------------------------------
# Unmatched digest
# ---------------------------------------------------------------------------

@router.get("/unmatched", response_class=HTMLResponse)
async def unmatched_digest(request: Request):
    entries = aggregate_unmatched(request.app.state.config.report_path)
    return request.app.state.templates.TemplateResponse(
        request,
        "unmatched.html",
        {"entries": entries},
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
    form = await request.form()
    file_path = (form.get("file_path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="file_path is required")
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    from app.scanner import MediaFile
    import os as _os

    stem = _os.path.splitext(_os.path.basename(file_path))[0]
    dirpath = _os.path.dirname(file_path)
    nfo_path = _os.path.join(dirpath, f"{stem}.nfo")
    media = MediaFile(path=file_path, stem=stem, nfo_path=nfo_path)

    run_fn = request.app.state.run_fn
    config = request.app.state.config

    from unittest.mock import patch
    with patch("app.main.scan_library", return_value=([media], 0)):
        original_force = config.force
        config.force = True
        try:
            run_fn()
        finally:
            config.force = original_force

    return RedirectResponse(url="/", status_code=303)
