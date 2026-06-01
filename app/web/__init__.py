# app/web/__init__.py
#
# FastAPI application factory for the m3 web dashboard.

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI

logger = logging.getLogger(__name__)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from app.web.routes import router


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none';"
        )
        return response


def create_app(config, plugin_registry: dict, scheduler, run_fn, run_state=None,
               dry_run_fn=None) -> FastAPI:
    """
    Build and return the FastAPI application.

    config          — the loaded Config instance (for report_path, plugin_dir, etc.)
    plugin_registry — {site_id: plugin_instance} dict from load_plugins()
    scheduler       — the APScheduler BlockingScheduler instance (for "Run now")
    run_fn          — the run() closure used by the scheduler (same one reused here)
    run_state       — optional RunState for the in-progress indicator
    dry_run_fn      — optional dry-run closure (runs with dry_run_strict=True)
    """
    app = FastAPI(title=f"{config.app_name} dashboard", docs_url=None, redoc_url=None)

    # Shared state accessible in all route handlers via request.app.state
    from app.router import Router
    app.state.config = config
    app.state.plugin_registry = plugin_registry
    app.state.plugin_router = Router(plugin_registry)
    app.state.scheduler = scheduler
    app.state.run_fn = run_fn
    app.state.dry_run_fn = dry_run_fn
    app.state.run_state = run_state

    # Static files (CSS)
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Jinja2 templates — autoescaping enabled for all .html and .xml files
    templates_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    templates = Jinja2Templates(env=env)
    from app import __version__
    templates.env.globals["app_name"] = config.app_name
    templates.env.globals["version"] = __version__

    # Defined inside create_app (rather than module-level) so it stays with the
    # other Jinja2 globals that are registered here. It has no external dependencies,
    # so either location would work; keeping them together makes the template API
    # easier to find.
    def _fmt_duration(seconds: int | float | None) -> str:
        if seconds is None:
            return "—"
        seconds = int(seconds)
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
            now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
            raw_secs = (now - dt).total_seconds()
            if raw_secs < 0:
                if raw_secs < -5:  # only log if more than 5 seconds ahead — minor skew is normal
                    logger.debug(
                        "_time_ago: future timestamp %r (%.1fs ahead); "
                        "possible clock skew or timezone mismatch",
                        iso_str, -raw_secs,
                    )
                return ""
            secs = int(raw_secs)
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

    # Custom Jinja2 filters
    templates.env.filters["basename"] = os.path.basename

    app.state.templates = templates

    app.add_middleware(_SecurityHeadersMiddleware)
    app.include_router(router)
    return app
