# -----------------------------------------------------------------------------
# app/config.py
#
# Loads all runtime configuration from environment variables.
#
# Every setting has a sensible default except PLEX_URL and PLEX_TOKEN, which
# are required — the app will refuse to start if either is missing.
#
# Environment variables (set these in your Unraid Docker template):
#   PLEX_URL              URL of your Plex server, e.g. http://192.168.1.x:32400
#   PLEX_TOKEN            Your Plex authentication token (see UNRAID_SETUP.md)
#   LIBRARY_PATHS         Comma-separated container paths to scan, e.g. /media/Movies,/media/TV
#   PLUGIN_DIR            Where drop-in plugin .py files live (default: ./plugins)
#   REPORT_PATH           Where run reports are written (default: ./reports)
#   LOG_PATH              Where rotating log files are written (default: ./logs)
#   RUN_SCHEDULE          Cron expression for nightly runs (default: "0 3 * * *" = 3am)
#   LOG_LEVEL             Logging verbosity: DEBUG, INFO, WARNING, ERROR (default: INFO)
#   LOG_RETENTION_DAYS    Delete log files older than N days (default: 30)
#   REPORT_RETENTION_DAYS Delete report files older than N days (default: 90)
#   WEB_ENABLED           Enable the web dashboard (default: true)
#   WEB_PORT              Port the web dashboard listens on (default: 8765)
#   WEB_HOST              Host the web dashboard binds to (default: 0.0.0.0)
#   APP_NAME              Display name in the dashboard header and page title (default: m3)
#   PLUGIN_RATE_LIMIT_SECS    Seconds between plugin fetch() calls (default: 1.0)
#   PLUGIN_FETCH_TIMEOUT_SECS Max seconds one plugin.fetch() may block (default: 60)
#   NOTIFY_URL            Optional webhook URL; m3 POSTs a JSON summary after each run (default: "")
#                         Works with Apprise, Gotify, Pushover relay, or any HTTP endpoint
#   LIBRARY_EXCLUDE_PATTERNS  Comma-separated glob patterns to exclude from scanning (default: "")
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Config:
    # Connection details for your Plex Media Server
    plex_url: str
    # plex_token is excluded from __repr__ so it never appears in logs or tracebacks
    plex_token: str

    # Directories inside the container that will be scanned for media files
    library_paths: list[str]

    # Directory containing drop-in plugin .py files
    plugin_dir: str

    # Directories where reports and logs are written (should be mounted volumes)
    report_path: str
    log_path: str

    # APScheduler cron expression: "minute hour day month weekday"
    run_schedule: str

    # Python logging level string (e.g. "INFO", "DEBUG")
    log_level: str

    # How long to keep old log and report files before deleting them
    log_retention_days: int
    report_retention_days: int

    # Web dashboard settings
    web_enabled: bool = True
    web_port: int = 8765
    web_host: str = "0.0.0.0"

    # Display name shown in the dashboard header, page title, and footer.
    # Override via APP_NAME env var to rename the app without editing code.
    app_name: str = "m3"

    # Minimum seconds to wait between plugin fetch() calls during a run.
    # Prevents hammering a site with hundreds of rapid-fire requests on large libraries.
    # Set to 0 to disable throttling (not recommended for production).
    plugin_rate_limit_secs: float = 1.0

    # Maximum seconds a single plugin.fetch() call may take before the run marks it
    # as an error and moves on. Prevents a hung plugin from blocking the entire run
    # indefinitely (scheduler max_instances=1 means a hung run blocks all future runs).
    # Set to 0 to disable the timeout entirely (not recommended; a hung plugin will
    # block the run forever since max_instances=1 prevents concurrent runs).
    plugin_fetch_timeout_secs: float = 60.0

    # Optional webhook URL. When set, m3 POSTs a JSON run summary to this URL
    # after every completed (non-dry-run) run. Leave empty to disable. Works with
    # any HTTP endpoint that accepts JSON (Apprise, Gotify, Pushover relay, etc.).
    notify_url: str = ""

    # Minimum combined error count (errors + scrape_errors + image_errors) required
    # before the webhook fires. Webhook fires when total error count >= this value
    # (default: 1). Set to 0 to always fire regardless of errors.
    notify_min_errors: int = 1

    # When True, re-process files that already have .nfo sidecars.
    # Set via --force CLI flag, not an env var (running with --force every
    # scheduled run would defeat the purpose of the skip logic).
    force: bool = False

    # Comma-separated glob patterns for files/directories to skip during scanning.
    # Matched against the full absolute path. Example: "*.part,/media/incoming/**"
    # Useful for excluding temp files, hidden directories, or work-in-progress media.
    library_exclude_patterns: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        import dataclasses
        parts = []
        for f in dataclasses.fields(self):
            val = getattr(self, f.name)
            if f.name == "plex_token":
                val = "***"
            parts.append(f"{f.name}={val!r}")
        return f"Config({', '.join(parts)})"


def load_config(force: bool = False, plex_required: bool = True) -> Config:
    # Inner helper: raise if a required env var is missing or empty
    def require(key: str) -> str:
        val = os.environ.get(key, "").strip()
        if not val:
            raise ValueError(f"Required environment variable {key!r} is not set")
        return val

    # Inner helper: like require() but returns "" when plex_required=False
    def require_or_empty(key: str) -> str:
        return require(key) if plex_required else os.environ.get(key, "").strip()

    # Inner helper: return env var value or a default if not set
    def optional(key: str, default: str) -> str:
        return os.environ.get(key, default).strip()

    # Inner helper: parse an integer env var with a clear error on bad values
    def optional_int(key: str, default: int, min_val: int | None = None) -> int:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            val = int(raw)
        except ValueError:
            raise ValueError(
                f"Environment variable {key!r} must be an integer, got {raw!r}"
            ) from None
        if min_val is not None and val < min_val:
            raise ValueError(
                f"Environment variable {key!r} must be >= {min_val}, got {val}"
            )
        return val

    # Inner helper: parse a float env var with bounds check
    def optional_float(key: str, default: float, min_val: float = 0.0) -> float:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            val = float(raw)
        except ValueError:
            raise ValueError(
                f"Environment variable {key!r} must be a number, got {raw!r}"
            ) from None
        if val < min_val:
            raise ValueError(
                f"Environment variable {key!r} must be >= {min_val}, got {val}"
            )
        return val

    # LIBRARY_PATHS is comma-separated, e.g. "/media/Movies,/media/TV"
    # Split and strip each path, dropping empty entries
    raw_paths_env = os.environ.get("LIBRARY_PATHS")
    raw_paths = raw_paths_env.strip() if raw_paths_env else ""
    if not raw_paths:
        raw_paths = "./media"
        logger.warning(
            "LIBRARY_PATHS not set; defaulting to %r — set LIBRARY_PATHS to your actual media directory",
            raw_paths,
        )
    library_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]
    if not library_paths:
        raise ValueError(
            "LIBRARY_PATHS produced an empty list after parsing — "
            "check that your comma-separated paths are valid."
        )

    raw_exclude = optional("LIBRARY_EXCLUDE_PATTERNS", "")
    library_exclude_patterns = [p.strip() for p in raw_exclude.split(",") if p.strip()]
    # Warn if the user appears to have used spaces instead of commas as separators.
    # A single-element result containing a space (and no glob metacharacters) is
    # the telltale sign of e.g. LIBRARY_EXCLUDE_PATTERNS="*.part *.tmp" instead of
    # the correct "*.part,*.tmp".
    if (
        raw_exclude
        and len(library_exclude_patterns) == 1
        and " " in library_exclude_patterns[0]
    ):
        logger.warning(
            "LIBRARY_EXCLUDE_PATTERNS=%r appears to use spaces as separators. "
            "Use commas to separate multiple patterns. "
            "Current value is treated as a single pattern.",
            raw_exclude,
        )

    web_enabled_raw = optional("WEB_ENABLED", "true").lower()
    web_enabled = web_enabled_raw in ("true", "1", "yes", "on")
    _KNOWN_FALSE = {"false", "0", "no", "off", ""}
    if web_enabled_raw and web_enabled_raw not in {"true", "1", "yes", "on"} | _KNOWN_FALSE:
        logger.warning(
            "WEB_ENABLED=%r is not a recognised boolean value; treating as False. "
            "Use 'true', '1', 'yes', or 'on' to enable.",
            web_enabled_raw,
        )

    plex_url = require_or_empty("PLEX_URL")
    if plex_required:
        from urllib.parse import urlparse as _urlparse
        parsed_url = _urlparse(plex_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise ValueError(f"PLEX_URL must be a valid http/https URL, got: {plex_url!r}")

    run_schedule = optional("RUN_SCHEDULE", "0 3 * * *")
    try:
        from apscheduler.triggers.cron import CronTrigger as _CronTrigger
        _CronTrigger.from_crontab(run_schedule)
    except Exception as exc:
        raise ValueError(
            f"RUN_SCHEDULE {run_schedule!r} is not a valid cron expression: {exc}"
        ) from exc

    log_level = optional("LOG_LEVEL", "INFO").upper()
    _VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in _VALID_LEVELS:
        raise ValueError(
            f"LOG_LEVEL {log_level!r} is not valid; must be one of {sorted(_VALID_LEVELS)}"
        )

    app_name = optional("APP_NAME", "m3")
    if not app_name:
        raise ValueError("APP_NAME must not be empty")

    _plugin_fetch_timeout_secs = optional_float("PLUGIN_FETCH_TIMEOUT_SECS", 60.0, min_val=0.0)
    if _plugin_fetch_timeout_secs == 0.0:
        logger.warning(
            "PLUGIN_FETCH_TIMEOUT_SECS=0 disables the per-plugin fetch timeout; "
            "a hung plugin will block the scheduler indefinitely."
        )

    return Config(
        plex_url=plex_url,
        plex_token=require_or_empty("PLEX_TOKEN"),
        library_paths=library_paths,
        plugin_dir=optional("PLUGIN_DIR", "./plugins"),
        report_path=optional("REPORT_PATH", "./reports"),
        log_path=optional("LOG_PATH", "./logs"),
        run_schedule=run_schedule,
        log_level=log_level,
        log_retention_days=optional_int("LOG_RETENTION_DAYS", 30, min_val=1),
        report_retention_days=optional_int("REPORT_RETENTION_DAYS", 90, min_val=1),
        web_enabled=web_enabled,
        web_port=optional_int("WEB_PORT", 8765, min_val=1),
        web_host=optional("WEB_HOST", "0.0.0.0"),
        app_name=app_name,
        plugin_rate_limit_secs=optional_float("PLUGIN_RATE_LIMIT_SECS", 1.0, min_val=0.0),
        plugin_fetch_timeout_secs=_plugin_fetch_timeout_secs,
        notify_url=optional("NOTIFY_URL", ""),
        notify_min_errors=optional_int("NOTIFY_MIN_ERRORS", 1, min_val=0),
        force=force,
        library_exclude_patterns=library_exclude_patterns,
    )
