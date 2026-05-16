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


def setup_logging(log_path: str, log_level: str) -> None:
    # Create the log directory if it doesn't exist yet
    os.makedirs(log_path, exist_ok=True)
    log_file = os.path.join(log_path, "pm.log")

    # Shared format: timestamp, level, logger name, message
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # Rotating file handler: max 10 MB per file, keep 5 backups (pm.log, pm.log.1, ...)
    file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(fmt)

    # Stream handler writes to stdout so `docker logs pm` captures it
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    # Apply both handlers to the root logger so all modules inherit them
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def cleanup_old_files(directory: str, retention_days: int, pattern_suffix: str = "") -> int:
    """
    Delete files in `directory` whose last-modified time is older than
    `retention_days` days. If `pattern_suffix` is given (e.g. ".log", ".json"),
    only files with that suffix are considered.

    Returns the number of files deleted.
    """
    if not os.path.isdir(directory):
        return 0

    # Any file older than this timestamp will be deleted
    cutoff = datetime.now() - timedelta(days=retention_days)
    removed = 0

    for fname in os.listdir(directory):
        # Skip files that don't match the requested suffix filter
        if pattern_suffix and not fname.endswith(pattern_suffix):
            continue

        fpath = os.path.join(directory, fname)
        if os.path.isfile(fpath):
            mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
            if mtime < cutoff:
                os.remove(fpath)
                removed += 1

    return removed
