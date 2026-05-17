# app/web/__init__.py
#
# FastAPI application factory for the pm web dashboard.

from __future__ import annotations

import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.web.routes import router


def create_app(config, plugin_registry: dict, scheduler, run_fn, run_state=None) -> FastAPI:
    """
    Build and return the FastAPI application.

    config          — the loaded Config instance (for report_path, plugin_dir, etc.)
    plugin_registry — {site_id: plugin_instance} dict from load_plugins()
    scheduler       — the APScheduler BlockingScheduler instance (for "Run now")
    run_fn          — the run() closure used by the scheduler (same one reused here)
    run_state       — optional RunState for the in-progress indicator
    """
    app = FastAPI(title=f"{config.app_name} dashboard", docs_url=None, redoc_url=None)

    # Shared state accessible in all route handlers via request.app.state
    app.state.config = config
    app.state.plugin_registry = plugin_registry
    app.state.scheduler = scheduler
    app.state.run_fn = run_fn
    app.state.run_state = run_state

    # Static files (CSS)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Jinja2 templates
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    templates = Jinja2Templates(directory=templates_dir)
    templates.env.globals["app_name"] = config.app_name

    # Defined inside create_app (rather than module-level) so it stays with the
    # other Jinja2 globals that are registered here. It has no external dependencies,
    # so either location would work; keeping them together makes the template API
    # easier to find.
    def _fmt_duration(seconds: int | None) -> str:
        if seconds is None:
            return "—"
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m {seconds % 60}s"

    templates.env.globals["fmt_duration"] = _fmt_duration

    def _time_ago(iso_str: str | None) -> str:
        """Return a human-friendly relative time string like '2h ago' or '3d ago'."""
        if not iso_str:
            return ""
        try:
            dt = datetime.fromisoformat(iso_str)
            # RunReport.started_at uses datetime.now() (naive local time).
            # Compare against local time so the elapsed calculation is correct
            # on servers not running in UTC. If the datetime is tz-aware,
            # fall back to a UTC comparison.
            if dt.tzinfo is None:
                secs = int((datetime.now() - dt).total_seconds())
            else:
                secs = int((datetime.now(timezone.utc) - dt).total_seconds())
            if secs < 0:
                return ""
            if secs < 60:
                return f"{secs}s ago"
            if secs < 3600:
                return f"{secs // 60}m ago"
            if secs < 86400:
                return f"{secs // 3600}h ago"
            return f"{secs // 86400}d ago"
        except ValueError:
            return ""

    templates.env.globals["time_ago"] = _time_ago
    app.state.templates = templates

    app.include_router(router)
    return app
