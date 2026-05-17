# -----------------------------------------------------------------------------
# app/main.py
#
# Entrypoint for the pm metadata agent.
#
# Usage:
#   python -m app.main                        # start the scheduler (normal container mode)
#   python -m app.main --once                 # run once and exit (useful for testing)
#   python -m app.main --once --force         # run once, re-process all files
#   python -m app.main --once --dry-run       # parse + route but skip all writes
#
# On startup this module:
#   1. Parses CLI flags
#   2. Loads config from environment variables
#   3. Sets up logging (rotating file + stdout)
#   4. Discovers and loads plugins from the plugin directory
#   5. Either runs once (--once) or starts the APScheduler cron loop
#      (Plex connection is established per-run inside run(), not at startup)
#
# The core run() function is the heart of each pass:
#   scan → route → fetch → write NFO → write to Plex → report
# -----------------------------------------------------------------------------

import argparse
import logging
import os
import time
from datetime import datetime

from app.config import load_config
from app.logging_setup import setup_logging, cleanup_old_files
from app.plugins.loader import load_plugins
from app.reporter import RunReport, FileResult, write_report
from app.router import Router
from app.runstate import RunState
from app.scanner import scan_library
from app.scrape import ScrapeError
from app.writers.nfo import write_nfo, write_images
from app.writers.plex import connect_plex, push_to_plex

logger = logging.getLogger(__name__)


def run(config, router: Router, dry_run: bool = False) -> None:
    """
    Execute one full metadata pass over all configured library paths.

    For each media file:
      - Route it to a plugin via the filename parser
      - Call the plugin to fetch metadata from the external API
      - Write an NFO sidecar + poster/fanart images next to the file
      - Push the same metadata to Plex (if connected) with field locks
      - Record the outcome in the run report

    When dry_run=True, all writes are skipped; routing and plugin fetches still run.

    Plex is reconnected on every run so that long-running schedulers don't use
    a stale connection after a Plex server restart or token expiry.
    """
    if dry_run:
        logger.info("DRY RUN mode — no files or Plex records will be written")

    # Reconnect to Plex at the start of every run (not once at startup) so that
    # a Plex restart between scheduled runs doesn't leave us with a dead connection.
    plex_server = connect_plex(config.plex_url, config.plex_token)
    if plex_server is None:
        logger.warning("Plex connection failed. Metadata will be written to sidecars only.")

    report = RunReport(started_at=datetime.now().isoformat(timespec="seconds"))

    # Collect all video files that need processing (skips files with existing .nfo unless --force)
    media_files, skipped_count = scan_library(config.library_paths, force=config.force)

    # Record skipped files in the report so the summary includes an accurate count
    for _ in range(skipped_count):
        report.record(FileResult(path="", status="skipped"))

    if not media_files and skipped_count == 0:
        logger.warning(
            "No media files found to process. Check that LIBRARY_PATHS is correct "
            "and that the directories are mounted and contain video files."
        )

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
        except ScrapeError as exc:
            # ScrapeError means the site changed its markup or the record is gone —
            # a distinct status so the user can distinguish "my plugin is broken"
            # from "the site changed" in the run report.
            logger.warning("Scrape error for %s: %s", media.path, exc)
            report.record(FileResult(path=media.path, status="scrape_error", message=str(exc)))
            continue
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

        # Throttle after a successful fetch to avoid hammering the source site.
        # Placed here (after fetch, after the None-result guard) so that:
        #   - the first file in a run is not delayed
        #   - failed fetches (exception or None result) don't count toward the window
        if config.plugin_rate_limit_secs > 0:
            time.sleep(config.plugin_rate_limit_secs)

        # Write the NFO sidecar and download poster/fanart images alongside the video file
        if dry_run:
            logger.info("[DRY RUN] Would write NFO + images for: %s", media.path)
        else:
            try:
                write_nfo(media, result)
            except Exception as exc:
                logger.exception("NFO write error for %s", media.path)
                report.record(FileResult(path=media.path, status="error", message=str(exc)))
                continue

            try:
                write_images(media, result)
            except Exception as exc:
                logger.exception("Image write error for %s", media.path)
                report.record(FileResult(path=media.path, status="error", message=str(exc)))
                continue

        # Push the same metadata to Plex with field locks so Plex's built-in agent
        # won't overwrite our values on the next scheduled refresh.
        # This is non-fatal: if Plex is unreachable we still have the NFO sidecar.
        if not dry_run and plex_server is not None:
            try:
                ok = push_to_plex(plex_server, media.path, result)
                if not ok:
                    logger.warning("Plex push returned failure for %s", media.path)
            except Exception as exc:
                logger.warning("Plex push failed for %s: %s", media.path, exc)
        elif dry_run and plex_server is not None:
            logger.info("[DRY RUN] Would push to Plex for: %s", media.path)

        report.record(FileResult(path=media.path, status="updated"))
        logger.info("Updated: %s", media.path)

    report.finished_at = datetime.now().isoformat(timespec="seconds")

    # Dry runs don't write report files — they'd pollute history with zero-action entries.
    # The summary is logged to stdout instead so the user still sees what would have run.
    if dry_run:
        logger.info(
            "[DRY RUN] Skipping report write. "
            "updated=%d skipped=%d unmatched=%d scrape_errors=%d errors=%d",
            report.updated, report.skipped, report.unmatched,
            report.scrape_errors, report.errors,
        )
    else:
        # Write JSON + plain-text report files and clean up old ones
        write_report(report, config.report_path, config.report_retention_days)

    # Delete old log files beyond the retention window (runs regardless of dry_run)
    cleanup_old_files(config.log_path, config.log_retention_days, pattern_suffix=".log")

    logger.info(
        "Run complete. updated=%d skipped=%d unmatched=%d add_form=%d scrape_errors=%d errors=%d",
        report.updated, report.skipped, report.unmatched, report.add_form,
        report.scrape_errors, report.errors,
    )


def _validate_paths(config) -> None:
    """
    Check that critical filesystem paths are accessible before the scheduler starts.
    Logs a clear error and raises SystemExit for each fatal misconfiguration so the
    container exits immediately with a useful message rather than silently doing nothing.
    """
    errors: list[str] = []

    # At least one library path must exist and be a directory
    found_any = False
    for path in config.library_paths:
        if os.path.isdir(path):
            found_any = True
        else:
            logger.warning("Library path does not exist or is not a directory: %s", path)
    if config.library_paths and not found_any:
        errors.append(
            f"None of the configured LIBRARY_PATHS exist: {config.library_paths}. "
            "Check that volumes are mounted correctly."
        )

    # report_path must be writable (create it if absent — it may not exist yet)
    try:
        os.makedirs(config.report_path, exist_ok=True)
        test = os.path.join(config.report_path, ".write_test")
        with open(test, "w") as fh:
            fh.write("")
        os.remove(test)
    except OSError as exc:
        errors.append(f"REPORT_PATH {config.report_path!r} is not writable: {exc}")

    for msg in errors:
        logger.error("Startup validation failed: %s", msg)
    if errors:
        raise SystemExit(1)


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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and route files but skip all writes (NFO, images, Plex)",
    )
    parser.add_argument(
        "--list-unmatched",
        action="store_true",
        help="Scan library paths, print files that cannot be routed to any plugin, then exit",
    )
    args = parser.parse_args()

    # Load all settings from environment variables; fails fast if PLEX_URL/TOKEN missing
    config = load_config(force=args.force)

    # Set up logging before anything else so startup messages are captured
    setup_logging(config.log_path, config.log_level)

    logger.info("pm starting up")
    logger.info("Library paths: %s", config.library_paths)

    # Validate the cron schedule before touching the filesystem — a bad expression
    # produces a cryptic APScheduler traceback otherwise.
    parts = config.run_schedule.strip().split()
    if len(parts) != 5:
        logger.error(
            "RUN_SCHEDULE must be a 5-field cron expression (min hour day month weekday), "
            "got %r", config.run_schedule
        )
        raise SystemExit(1)

    # Validate critical paths before starting the scheduler so misconfigurations
    # fail immediately with a clear message rather than silently producing empty runs.
    _validate_paths(config)

    # Discover plugins from the mounted plugin directory
    registry = load_plugins(config.plugin_dir)
    if not registry:
        logger.warning("No plugins loaded. Files with site tokens will be unmatched.")

    # Build the router with the loaded plugin registry
    router = Router(registry)

    if args.list_unmatched:
        # Scan libraries, print every file the router cannot dispatch, then exit.
        # Useful for diagnosing missing plugins or unexpected filename formats without
        # running a full metadata pass.
        media_files, skipped_count = scan_library(config.library_paths, force=True)
        unroutable = []
        for media in media_files:
            parsed, plugin = router.dispatch(media.stem)
            if parsed is None or plugin is None:
                unroutable.append(media.path)
        if unroutable:
            print(f"{len(unroutable)} unmatched file(s):")
            for path in unroutable:
                print(f"  {path}")
        else:
            print("All files matched a plugin.")
        return

    run_state = RunState()

    # Wrap run() so the scheduler and --once path call the same function.
    # Plex is reconnected inside run() on every execution so scheduled runs
    # don't use a stale connection after a Plex restart.
    def _run():
        run_state.start()
        try:
            run(config, router, dry_run=args.dry_run)
        finally:
            run_state.stop()

    if args.once:
        # Run immediately and exit — useful for testing or docker exec one-shots
        _run()
        return

    # --force only applies to the single --once run, not to recurring scheduled runs.
    # Reset it here so if someone mistakenly passes --force without --once, the
    # scheduler doesn't re-process every file on every nightly run.
    if args.force:
        logger.warning(
            "--force was passed without --once; ignoring --force for scheduled runs. "
            "Use --once --force to re-process files in a one-shot run."
        )
        config.force = False

    # Build the scheduler (registers SIGUSR1 handler, adds cron job).
    # We build it before starting the web server so both share the same instance.
    from app.scheduler import build_scheduler, register_sigusr2_reload
    scheduler = build_scheduler(_run, config.run_schedule)

    # SIGUSR2 triggers an in-place plugin reload without restarting the container.
    # The registry dict is shared with the router and the web dashboard; mutating
    # it in-place keeps all references up-to-date without rebuilding the router.
    register_sigusr2_reload(registry, config.plugin_dir)

    # Launch the web dashboard in a daemon thread so it runs alongside the scheduler.
    # The scheduler keeps the main thread; the web server is the side thread.
    if config.web_enabled:
        import threading
        import uvicorn
        from app.web import create_app

        web_app = create_app(config, registry, scheduler, _run, run_state)
        web_config = uvicorn.Config(
            web_app,
            host=config.web_host,
            port=config.web_port,
            log_level="warning",
            access_log=False,
        )
        web_server = uvicorn.Server(web_config)

        web_thread = threading.Thread(
            target=web_server.run,
            daemon=True,
            name="pm-web",
        )
        web_thread.start()
        logger.info("Web dashboard started on http://%s:%d", config.web_host, config.web_port)

    # Start the blocking scheduler (blocks until container stops)
    logger.info("Scheduler started. Next run scheduled via: %s", config.run_schedule)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
