# -----------------------------------------------------------------------------
# app/logging_setup.py
#
# Configures Python's logging system for the application.
#
# Two output destinations are set up:
#   1. A rotating file at <LOG_PATH>/<app_name>.log — survives container restarts;
#      older files are rotated out automatically once they reach 10 MB.
#   2. stdout — so `docker logs m3` shows live output in Unraid's UI.
#
# Retention cleanup:
#   cleanup_old_files() is called after each run to delete log and report
#   files older than the configured retention period, keeping disk usage
#   in check on long-running Unraid installs.
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per rotated file
_LOG_BACKUP_COUNT = 5               # keep m3.log + 5 rotated backups

# Tracks only handlers that setup_logging itself added, so we only remove our own
# handlers on re-call (not any handlers added by pytest, third-party libs, etc.)
_m3_handlers: list[logging.Handler] = []


def setup_logging(log_path: str, log_level: str, app_name: str = "m3") -> None:
    # Create the log directory if it doesn't exist yet.
    # If this fails (e.g. permission error) we print to stderr and fall back to
    # stdout-only logging so startup messages are never silently swallowed.
    try:
        os.makedirs(log_path, exist_ok=True)
    except OSError as exc:
        print(
            f"{app_name} WARNING: could not create log directory {log_path!r}: {exc}. "
            "Falling back to stdout-only logging.",
            file=sys.stderr,
        )
        log_path = None  # type: ignore[assignment]

    log_file = os.path.join(log_path, f"{app_name}.log") if log_path else None

    # Shared format: timestamp, level, logger name, message
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()

    # Guard against duplicate handlers if setup_logging is called more than once
    # (e.g. during tests). Remove only handlers that we previously added — leave
    # any handlers added by pytest, third-party libraries, or the caller intact.
    for h in _m3_handlers:
        root.removeHandler(h)
    _m3_handlers.clear()

    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Stream handler writes to stdout so `docker logs m3` captures it
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)
    _m3_handlers.append(stream_handler)

    # Rotating file handler: max 10 MB per file, keep 5 backups (m3.log, m3.log.1, ...)
    # Only added when a writable log directory is available.
    if log_file:
        try:
            # delay=True defers opening the file until the first log write,
            # which reduces Windows file-locking contention during rotation.
            file_handler = RotatingFileHandler(
                log_file, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUP_COUNT,
                delay=True, encoding="utf-8",
            )
            # Windows: RotatingFileHandler.doRollover() calls os.rename() which raises
            # PermissionError (WinError 32) when another process holds the log file open
            # (e.g. a tail -f in a terminal or Unraid's log viewer). Replace the default
            # rotator with a version that retries with exponential backoff, matching the
            # atomic_replace() pattern used elsewhere in the codebase.
            if sys.platform == "win32":
                def _win_rotator(source: str, dest: str) -> None:
                    for attempt in range(5):
                        try:
                            os.replace(source, dest)
                            return
                        except PermissionError:
                            if attempt == 4:
                                raise
                            time.sleep(0.05 * (2 ** attempt))
                file_handler.rotator = _win_rotator  # type: ignore[assignment]
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
            _m3_handlers.append(file_handler)
        except OSError as exc:
            print(
                f"{app_name} WARNING: could not open log file {log_file!r}: {exc}. "
                "Continuing with stdout-only logging.",
                file=sys.stderr,
            )


def cleanup_old_files(
    directory: str,
    retention_days: int,
    pattern_suffix: str = "",
    app_name: str = "m3",
) -> int:
    """
    Delete files in `directory` whose last-modified time is older than
    `retention_days` days. If `pattern_suffix` is given (e.g. ".log", ".json"),
    only files with that suffix are considered.

    For log files (.log suffix), the active log file "<app_name>.log" is always
    skipped even if its mtime predates the cutoff — only rotated backups
    (<app_name>.log.1, etc.) are eligible for deletion.

    Returns the number of files deleted.
    """
    if not os.path.isdir(directory):
        return 0

    active_log = f"{app_name}.log"

    # Any file older than this timestamp will be deleted
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0

    # os.scandir yields DirEntry objects that cache stat results, avoiding a
    # separate os.stat call per file compared to os.listdir + os.path.getmtime.
    with os.scandir(directory) as it:
        for entry in it:
            if not entry.is_file():
                continue
            fname = entry.name
            if pattern_suffix:
                # Python's RotatingFileHandler names backups "<app_name>.log.1", etc.
                # — they don't end with ".log", so we need a second check for rotated files.
                # We deliberately avoid plain `in` here: that would incorrectly match
                # unrelated files like "<app_name>.login_data" when filtering for ".log".
                is_rotated_log_backup = (
                    pattern_suffix == ".log"
                    and fname.startswith(f"{active_log}.")
                )
                if not fname.endswith(pattern_suffix) and not is_rotated_log_backup:
                    continue

            # Never delete the active log file — only rotated backups
            if fname == active_log:
                continue

            mtime = datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime < cutoff:
                try:
                    os.remove(entry.path)
                    removed += 1
                except PermissionError as exc:
                    # On Windows, a log viewer or tail process may hold the file
                    # open; skip and retry on the next cleanup pass.
                    logger.info(
                        "Could not remove %s (file in use, will retry next cleanup): %s",
                        entry.path, exc,
                    )
                except OSError as exc:
                    # Log but continue — a locked or unwritable file shouldn't
                    # abort cleanup of all other files
                    logger.warning("Could not delete old file %s: %s", entry.path, exc)

    return removed
