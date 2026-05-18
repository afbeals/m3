# -----------------------------------------------------------------------------
# app/logging_setup.py
#
# Configures Python's logging system for the application.
#
# Two output destinations are set up:
#   1. A rotating file at <LOG_PATH>/pm.log — survives container restarts;
#      older files are rotated out automatically once they reach 10 MB.
#   2. stdout — so `docker logs pm` shows live output in Unraid's UI.
#
# Retention cleanup:
#   cleanup_old_files() is called after each run to delete log and report
#   files older than the configured retention period, keeping disk usage
#   in check on long-running Unraid installs.
# -----------------------------------------------------------------------------

import logging
import os
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)


def setup_logging(log_path: str, log_level: str) -> None:
    # Create the log directory if it doesn't exist yet.
    # If this fails (e.g. permission error) we print to stderr and fall back to
    # stdout-only logging so startup messages are never silently swallowed.
    try:
        os.makedirs(log_path, exist_ok=True)
    except OSError as exc:
        import sys
        print(
            f"pm WARNING: could not create log directory {log_path!r}: {exc}. "
            "Falling back to stdout-only logging.",
            file=sys.stderr,
        )
        log_path = None  # type: ignore[assignment]

    log_file = os.path.join(log_path, "pm.log") if log_path else None

    # Shared format: timestamp, level, logger name, message
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()

    # Guard against duplicate handlers if setup_logging is called more than once
    # (e.g. during tests). Remove any previously added handlers first.
    if root.handlers:
        root.handlers.clear()

    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Stream handler writes to stdout so `docker logs pm` captures it
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)

    # Rotating file handler: max 10 MB per file, keep 5 backups (pm.log, pm.log.1, ...)
    # Only added when a writable log directory is available.
    if log_file:
        try:
            file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            import sys
            print(
                f"pm WARNING: could not open log file {log_file!r}: {exc}. "
                "Continuing with stdout-only logging.",
                file=sys.stderr,
            )


def cleanup_old_files(directory: str, retention_days: int, pattern_suffix: str = "") -> int:
    """
    Delete files in `directory` whose last-modified time is older than
    `retention_days` days. If `pattern_suffix` is given (e.g. ".log", ".json"),
    only files with that suffix are considered.

    For log files (.log suffix), the active log file "pm.log" is always skipped
    even if its mtime predates the cutoff — only rotated backups (pm.log.1, etc.)
    are eligible for deletion.

    Returns the number of files deleted.
    """
    if not os.path.isdir(directory):
        return 0

    # Any file older than this timestamp will be deleted
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0

    for fname in os.listdir(directory):
        if pattern_suffix:
            # Python's RotatingFileHandler names backups "pm.log.1", "pm.log.2", etc.
            # — they don't end with ".log", so we need a second check for rotated files.
            # We deliberately avoid plain `in` here: that would incorrectly match
            # unrelated files like "pm.login_data" when filtering for ".log".
            is_rotated_log_backup = (
                pattern_suffix == ".log"
                and fname.startswith("pm.log.")
            )
            if not fname.endswith(pattern_suffix) and not is_rotated_log_backup:
                continue

        # Never delete the active log file — only rotated backups (pm.log.1, pm.log.2, ...)
        if fname == "pm.log":
            continue

        fpath = os.path.join(directory, fname)
        if os.path.isfile(fpath):
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            if mtime < cutoff:
                try:
                    os.remove(fpath)
                    removed += 1
                except OSError as exc:
                    # Log but continue — a locked or unwritable file shouldn't
                    # abort cleanup of all other files
                    logger.warning("Could not delete old file %s: %s", fpath, exc)

    return removed
