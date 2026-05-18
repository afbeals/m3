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
#   PLUGIN_DIR            Where drop-in plugin .py files live (default: /plugins)
#   REPORT_PATH           Where run reports are written (default: /config/reports)
#   LOG_PATH              Where rotating log files are written (default: /config/logs)
#   RUN_SCHEDULE          Cron expression for nightly runs (default: "0 3 * * *" = 3am)
#   LOG_LEVEL             Logging verbosity: DEBUG, INFO, WARNING, ERROR (default: INFO)
#   LOG_RETENTION_DAYS    Delete log files older than N days (default: 30)
#   REPORT_RETENTION_DAYS Delete report files older than N days (default: 90)
#   WEB_ENABLED           Enable the web dashboard (default: true)
#   WEB_PORT              Port the web dashboard listens on (default: 8765)
#   WEB_HOST              Host the web dashboard binds to (default: 0.0.0.0)
#   APP_NAME              Display name in the dashboard header and page title (default: pm)
#   PLUGIN_RATE_LIMIT_SECS    Seconds between plugin fetch() calls (default: 1.0)
#   PLUGIN_FETCH_TIMEOUT_SECS Max seconds one plugin.fetch() may block (default: 60)
#   NOTIFY_URL            Optional webhook URL; pm POSTs a JSON summary after each run (default: "")
#                         Works with Apprise, Gotify, Pushover relay, or any HTTP endpoint
#   LIBRARY_EXCLUDE_PATTERNS  Comma-separated glob patterns to exclude from scanning (default: "")
# -----------------------------------------------------------------------------

from __future__ import annotations

import os
from dataclasses import dataclass, field


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
    app_name: str = "pm"

    # Minimum seconds to wait between plugin fetch() calls during a run.
    # Prevents hammering a site with hundreds of rapid-fire requests on large libraries.
    # Set to 0 to disable throttling (not recommended for production).
    plugin_rate_limit_secs: float = 1.0

    # Maximum seconds a single plugin.fetch() call may take before the run marks it
    # as an error and moves on. Prevents a hung plugin from blocking the entire run
    # indefinitely (scheduler max_instances=1 means a hung run blocks all future runs).
    plugin_fetch_timeout_secs: float = 60.0

    # Optional webhook URL. When set, pm POSTs a JSON run summary to this URL
    # after every successful run. Leave empty to disable. Works with any HTTP
    # endpoint that accepts JSON (Apprise, Gotify, Pushover relay, custom scripts).
    notify_url: str = ""

    # When True, re-process files that already have .nfo sidecars.
    # Set via --force CLI flag, not an env var (running with --force every
    # scheduled run would defeat the purpose of the skip logic).
    force: bool = False

    # Comma-separated glob patterns for files/directories to skip during scanning.
    # Matched against the full absolute path. Example: "*.part,/media/incoming/**"
    # Useful for excluding temp files, hidden directories, or work-in-progress media.
    library_exclude_patterns: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        # Redact plex_token so it never appears in logs or debug output
        return (
            f"Config(plex_url={self.plex_url!r}, plex_token='***', "
            f"library_paths={self.library_paths!r}, plugin_dir={self.plugin_dir!r}, "
            f"report_path={self.report_path!r}, log_path={self.log_path!r}, "
            f"run_schedule={self.run_schedule!r}, log_level={self.log_level!r}, "
            f"log_retention_days={self.log_retention_days!r}, "
            f"report_retention_days={self.report_retention_days!r}, "
            f"web_enabled={self.web_enabled!r}, web_port={self.web_port!r}, "
            f"app_name={self.app_name!r}, "
            f"plugin_rate_limit_secs={self.plugin_rate_limit_secs!r}, "
            f"plugin_fetch_timeout_secs={self.plugin_fetch_timeout_secs!r}, "
            f"notify_url={self.notify_url!r}, "
            f"library_exclude_patterns={self.library_exclude_patterns!r}, "
            f"force={self.force!r})"
        )


def load_config(force: bool = False) -> Config:
    # Inner helper: raise if a required env var is missing or empty
    def require(key: str) -> str:
        val = os.environ.get(key, "").strip()
        if not val:
            raise ValueError(f"Required environment variable {key!r} is not set")
        return val

    # Inner helper: return env var value or a default if not set
    def optional(key: str, default: str) -> str:
        return os.environ.get(key, default).strip()

    # Inner helper: parse an integer env var with a clear error on bad values
    def optional_int(key: str, default: int) -> int:
        raw = os.environ.get(key, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(
                f"Environment variable {key!r} must be an integer, got {raw!r}"
            ) from None

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
    raw_paths = optional("LIBRARY_PATHS", "/media")
    library_paths = [p.strip() for p in raw_paths.split(",") if p.strip()]

    raw_exclude = optional("LIBRARY_EXCLUDE_PATTERNS", "")
    library_exclude_patterns = [p.strip() for p in raw_exclude.split(",") if p.strip()]

    web_enabled_raw = optional("WEB_ENABLED", "true").lower()
    web_enabled = web_enabled_raw not in ("false", "0", "no", "off")

    return Config(
        plex_url=require("PLEX_URL"),
        plex_token=require("PLEX_TOKEN"),
        library_paths=library_paths,
        plugin_dir=optional("PLUGIN_DIR", "/plugins"),
        report_path=optional("REPORT_PATH", "/config/reports"),
        log_path=optional("LOG_PATH", "/config/logs"),
        run_schedule=optional("RUN_SCHEDULE", "0 3 * * *"),
        log_level=optional("LOG_LEVEL", "INFO").upper(),
        log_retention_days=optional_int("LOG_RETENTION_DAYS", 30),
        report_retention_days=optional_int("REPORT_RETENTION_DAYS", 90),
        web_enabled=web_enabled,
        web_port=optional_int("WEB_PORT", 8765),
        web_host=optional("WEB_HOST", "0.0.0.0"),
        app_name=optional("APP_NAME", "pm"),
        plugin_rate_limit_secs=optional_float("PLUGIN_RATE_LIMIT_SECS", 1.0, min_val=0.0),
        plugin_fetch_timeout_secs=optional_float("PLUGIN_FETCH_TIMEOUT_SECS", 60.0, min_val=1.0),
        notify_url=optional("NOTIFY_URL", ""),
        force=force,
        library_exclude_patterns=library_exclude_patterns,
    )
