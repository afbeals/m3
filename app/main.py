# -----------------------------------------------------------------------------
# app/main.py
#
# Entrypoint for the pm metadata agent.
#
# Usage:
#   python -m app.main              # start the scheduler (normal container mode)
#   python -m app.main --once       # run once and exit (useful for testing)
#   python -m app.main --once --force  # run once, re-process all files
#
# On startup this module:
#   1. Parses CLI flags
#   2. Loads config from environment variables
#   3. Sets up logging (rotating file + stdout)
#   4. Discovers and loads plugins from the plugin directory
#   5. Connects to Plex (non-fatal if it fails — sidecars still get written)
#   6. Either runs once (--once) or starts the APScheduler cron loop
#
# The core run() function is the heart of each pass:
#   scan → route → fetch → write NFO → write to Plex → report
# -----------------------------------------------------------------------------

import argparse
import logging
import os
from datetime import datetime

from app.config import load_config
from app.logging_setup import setup_logging, cleanup_old_files
from app.plugins.loader import load_plugins
from app.reporter import RunReport, FileResult, write_report
from app.router import Router
from app.scanner import scan_library
from app.writers.nfo import write_nfo, write_images
from app.writers.plex import connect_plex, push_to_plex

logger = logging.getLogger(__name__)


def run(config, plex_server, router: Router) -> None:
    """
    Execute one full metadata pass over all configured library paths.

    For each media file:
      - Route it to a plugin via the filename parser
      - Call the plugin to fetch metadata from the external API
      - Write an NFO sidecar + poster/fanart images next to the file
      - Push the same metadata to Plex (if connected) with field locks
      - Record the outcome in the run report
    """
    report = RunReport(started_at=datetime.now().isoformat(timespec="seconds"))

    # Collect all video files that need processing (skips files with existing .nfo unless --force)
    media_files = scan_library(config.library_paths, force=config.force)

    for media in media_files:
        # Ask the router to parse the filename and find the right plugin
        parsed, plugin = router.dispatch(media.stem)

        # Case 1: filename couldn't be decoded at all
        if parsed is None:
            logger.info("Unmatched (could not parse): %s", media.path)
            report.record(FileResult(path=media.path, status="unmatched"))
            continue

        # Case 2: Manual Add form — no plugin needed, flag for human review
        if parsed.form == "add":
            msg = f"actors={parsed.actors} title={parsed.title!r} studio={parsed.studio!r}"
            logger.info("Manual Add form (no plugin): %s | %s", media.path, msg)
            report.record(FileResult(path=media.path, status="add_form", message=msg))
            continue

        # Case 3: parsed OK but no plugin is registered for this site
        if plugin is None:
            logger.info("Unmatched (no plugin for site %r): %s", parsed.site, media.path)
            report.record(FileResult(
                path=media.path,
                status="unmatched",
                message=f"no plugin registered for site {parsed.site!r}",
            ))
            continue

        # Case 4: call the plugin to fetch metadata from the external API
        try:
            result = plugin.fetch(parsed)
        except Exception as exc:
            logger.exception("Plugin error for %s", media.path)
            report.record(FileResult(path=media.path, status="error", message=str(exc)))
            continue

        # Plugin can return None if the lookup found nothing (e.g. scene not in database)
        if result is None:
            logger.info("Plugin returned no result for: %s", media.path)
            report.record(FileResult(
                path=media.path,
                status="unmatched",
                message="plugin returned no result",
            ))
            continue

        # Write the NFO sidecar and download poster/fanart images alongside the video file
        try:
            write_nfo(media, result)
            write_images(media, result)
        except Exception as exc:
            logger.exception("NFO write error for %s", media.path)
            report.record(FileResult(path=media.path, status="error", message=str(exc)))
            continue

        # Push the same metadata to Plex with field locks so Plex's built-in agent
        # won't overwrite our values on the next scheduled refresh.
        # This is non-fatal: if Plex is unreachable we still have the NFO sidecar.
        if plex_server is not None:
            try:
                push_to_plex(plex_server, media.path, result)
            except Exception as exc:
                logger.warning("Plex push failed for %s: %s", media.path, exc)

        report.record(FileResult(path=media.path, status="updated"))
        logger.info("Updated: %s", media.path)

    report.finished_at = datetime.now().isoformat(timespec="seconds")

    # Write JSON + plain-text report files and clean up old ones
    write_report(report, config.report_path, config.report_retention_days)

    # Delete old log files beyond the retention window
    cleanup_old_files(config.log_path, config.log_retention_days, pattern_suffix=".log")

    logger.info(
        "Run complete. updated=%d unmatched=%d add_form=%d errors=%d",
        report.updated, report.unmatched, report.add_form, report.errors,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="pm — Plex Metadata Agent")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one metadata pass then exit (instead of starting the scheduler)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process files that already have .nfo sidecars (ignore skip logic)",
    )
    args = parser.parse_args()

    # Load all settings from environment variables; fails fast if PLEX_URL/TOKEN missing
    config = load_config(force=args.force)

    # Set up logging before anything else so startup messages are captured
    setup_logging(config.log_path, config.log_level)

    logger.info("pm starting up")
    logger.info("Library paths: %s", config.library_paths)

    # Discover plugins from the mounted plugin directory
    registry = load_plugins(config.plugin_dir)
    if not registry:
        logger.warning("No plugins loaded. Files with site tokens will be unmatched.")

    # Build the router with the loaded plugin registry
    router = Router(registry)

    # Connect to Plex — non-fatal; if this fails we fall back to sidecar-only writes
    plex_server = connect_plex(config.plex_url, config.plex_token)
    if plex_server is None:
        logger.warning("Plex connection failed. Metadata will be written to sidecars only.")

    # Wrap run() so the scheduler and --once path call the same function
    def _run():
        run(config, plex_server, router)

    if args.once:
        # Run immediately and exit — useful for testing or docker exec one-shots
        _run()
        return

    # Default mode: start the blocking APScheduler cron loop
    from app.scheduler import start_scheduler
    start_scheduler(_run, config.run_schedule)


if __name__ == "__main__":
    main()
