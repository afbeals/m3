# app/web/routes.py
#
# FastAPI route handlers for the pm web dashboard.

from __future__ import annotations

import logging
import inspect
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.web.history import list_runs, get_run

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health check — used by Docker HEALTHCHECK and Unraid
# ---------------------------------------------------------------------------

@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Dashboard — latest run
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    runs = list_runs(request.app.state.config.report_path)
    latest = runs[0] if runs else None
    return request.app.state.templates.TemplateResponse(
        request,
        "latest.html",
        {"latest": latest},
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
    # Filter by status if requested
    if status:
        files = [f for f in files if f.get("status") == status]

    return request.app.state.templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run, "files": files, "status_filter": status},
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

    # Patch scan_library for this single-file run by wrapping run_fn
    # in a closure that substitutes a fixed file list.
    from unittest.mock import patch
    with patch("app.main.scan_library", return_value=([media], 0)):
        # Force re-process even if sidecar exists
        original_force = config.force
        config.force = True
        try:
            run_fn()
        finally:
            config.force = original_force

    return RedirectResponse(url="/", status_code=303)
