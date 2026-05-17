# app/web/__init__.py
#
# FastAPI application factory for the pm web dashboard.

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.web.routes import router


def create_app(config, plugin_registry: dict, scheduler, run_fn) -> FastAPI:
    """
    Build and return the FastAPI application.

    config          — the loaded Config instance (for report_path, plugin_dir, etc.)
    plugin_registry — {site_id: plugin_instance} dict from load_plugins()
    scheduler       — the APScheduler BlockingScheduler instance (for "Run now")
    run_fn          — the run() closure used by the scheduler (same one reused here)
    """
    app = FastAPI(title="pm dashboard", docs_url=None, redoc_url=None)

    # Shared state accessible in all route handlers via request.app.state
    app.state.config = config
    app.state.plugin_registry = plugin_registry
    app.state.scheduler = scheduler
    app.state.run_fn = run_fn

    # Static files (CSS)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Jinja2 templates
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    app.state.templates = Jinja2Templates(directory=templates_dir)

    app.include_router(router)
    return app
