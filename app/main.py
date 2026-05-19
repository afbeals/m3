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
import glob
import json
import logging
import os
import threading
import time
from datetime import datetime

from app import __version__
from app.config import load_config
from app.logging_setup import setup_logging, cleanup_old_files
from app.plugins.loader import load_plugins
from app.reporter import RunReport, FileResult, write_report
from app.router import Router
from app.runstate import RunState
from app.scanner import scan_library
from app.scrape import ScrapeError

from app.writers.nfo import write_nfo, write_images, rename_nfo_assets
from app.writers.plex import connect_plex, push_to_plex, push_nfo_to_plex

logger = logging.getLogger(__name__)


_WEBHOOK_TIMEOUT_SECS = 10

# Default per-plugin fetch timeout in seconds. A plugin that never returns would
# hang the entire run indefinitely without this guard (scheduler max_instances=1
# means subsequent scheduled runs are silently skipped while the run is stuck).
# Configurable via PLUGIN_FETCH_TIMEOUT_SECS env var; see config.py.
_DEFAULT_PLUGIN_FETCH_TIMEOUT = 60


def _fire_webhook(url: str, report, *, app_name: str = "pm") -> None:
    """POST a compact JSON run summary to the configured NOTIFY_URL.

    Uses a short timeout so a slow/unreachable endpoint doesn't delay the
    completion log line. Non-fatal: any error is logged as a warning only.
    """
    # Collect first error/scrape-error message for alert systems that display a preview
    first_error = next(
        (f.message for f in report.files if f.status == "error" and f.message), None
    )
    first_scrape_error = next(
        (f.message for f in report.files if f.status == "scrape_error" and f.message), None
    )
    payload = json.dumps({
        "app_name": app_name,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "duration_seconds": report.duration_seconds,
        "updated": report.updated,
        "skipped": report.skipped,
        "unmatched": report.unmatched,
        "scrape_errors": report.scrape_errors,
        "errors": report.errors,
        "total_scanned": report.total_scanned,
        "first_error": first_error,
        "first_scrape_error": first_scrape_error,
    })
    try:
        import httpx
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECS) as client:
            r = client.post(
                url,
                content=payload.encode(),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": f"pm-metadata-agent/{__version__}",
                },
            )
            logger.info("Webhook notification sent to %s (HTTP %d)", url, r.status_code)
    except Exception as exc:
        logger.warning("Webhook notification failed for %s: %s", url, exc)


def _call_plugin_with_timeout(plugin, parsed, timeout_secs: float):
    """Call plugin.fetch(parsed) in a thread; return result or raise on timeout/error.

    The timeout is a *reporting* boundary: the plugin thread is not killed (Python
    can't forcibly stop threads), but the run moves on and records a timeout error.
    After the timeout, the background thread continues running until the plugin's
    own network call times out or returns — it does not accumulate indefinitely because
    each plugin call eventually completes (or its HTTP client times out).
    """
    result_holder: list = []
    exc_holder: list = []

    def _worker():
        try:
            result_holder.append(plugin.fetch(parsed))
        except Exception as exc:
            exc_holder.append(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout_secs)
    if t.is_alive():
        raise TimeoutError(f"plugin fetch exceeded {timeout_secs:.0f}s")
    if exc_holder:
        raise exc_holder[0]
    return result_holder[0] if result_holder else None


def run(
    config,
    router: Router,
    dry_run: bool = False,
    media_files_override: "list | None" = None,
) -> None:
    """
    Execute one full metadata pass over all configured library paths.

    For each media file:
      - Route it to a plugin via the filename parser
      - Call the plugin to fetch metadata from the external API
      - Write an NFO sidecar + poster/fanart images next to the file
      - Push the same metadata to Plex (if connected) with field locks
      - Record the outcome in the run report

    When dry_run=True, all writes are skipped; routing and plugin fetches still run.

    media_files_override: if provided, skip the library scan entirely and process
    only these specific MediaFile objects. Used by --retry-failed to avoid
    re-scanning and re-processing the entire library.

    Plex is reconnected on every run so that long-running schedulers don't use
    a stale connection after a Plex server restart or token expiry.
    """
    if dry_run:
        logger.info("DRY RUN mode — no files or Plex records will be written")

    # In dry-run mode skip the Plex connection entirely — connecting is a network
    # side-effect that would produce confusing warnings when the intent is a zero-
    # impact parse/route test pass.
    plex_server = None
    if not dry_run:
        # Reconnect to Plex at the start of every run (not once at startup) so that
        # a Plex restart between scheduled runs doesn't leave us with a dead connection.
        plex_server = connect_plex(config.plex_url, config.plex_token)
        if plex_server is None:
            logger.warning("Plex connection failed. Metadata will be written to sidecars only.")

    report = RunReport(started_at=datetime.now().isoformat(timespec="seconds"))
    # Per-run cache for the slow Plex fallback scan. On older Plex versions that
    # don't support the fast filepath filter, without this cache each file would
    # trigger a full O(library_size) scan. One dict shared across all push_to_plex
    # calls reduces the slow path from O(n * library) to O(library + n).
    plex_item_cache: dict = {}

    if media_files_override is not None:
        # --retry-failed injects a pre-built list; skip the full library scan.
        media_files = media_files_override
        skipped_count = 0
    else:
        # Collect all video files that need processing (skips files with existing .nfo unless --force)
        media_files, skipped_count = scan_library(
            config.library_paths,
            force=config.force,
            exclude_patterns=config.library_exclude_patterns,
        )

    # Count skipped files in the report without adding per-file entries.
    # Skipped files have no path to show in the detail view, and appending
    # thousands of empty-path FileResult objects would bloat the JSON report.
    report.skipped += skipped_count
    report.total_scanned += skipped_count

    # Only warn about an empty library when media_files_override was not provided
    # (i.e. this is a real library scan, not --retry-failed). A rename-only run can
    # legitimately produce media_files=[renamed...] with no remaining_files after
    # the rename block, so the check below happens after the rename split instead.
    if media_files_override is None and not media_files and skipped_count == 0:
        logger.warning(
            "No media files found to process. Check that LIBRARY_PATHS is correct "
            "and that the directories are mounted and contain video files."
        )

    # Handle renamed files first: rename sidecar assets on disk and re-push to Plex.
    # No plugin fetch is needed — the existing NFO already has the correct metadata.
    # Processed here (before the main loop) so the renamed file's new NFO exists on
    # disk before the normal loop would see it as a "new" file.
    remaining_files = []
    for media in media_files:
        if media.renamed_from is None:
            remaining_files.append(media)
            continue

        dirpath = os.path.dirname(media.path)
        logger.info("Renaming assets: %r → %r in %s", media.renamed_from, media.stem, dirpath)

        if dry_run:
            logger.info(
                "[DRY RUN] Would rename assets %r → %r and re-push Plex for: %s",
                media.renamed_from, media.stem, media.path,
            )
            report.record(FileResult(
                path=media.path,
                status="renamed",
                message=f"renamed from {media.renamed_from!r} (dry run)",
            ))
            continue

        try:
            rename_nfo_assets(dirpath, media.renamed_from, media.stem)
        except Exception as exc:
            logger.exception("Asset rename failed for %s", media.path)
            report.record(FileResult(path=media.path, status="error", message=str(exc)))
            continue

        if plex_server is not None:
            try:
                push_nfo_to_plex(
                    plex_server, media.path, media.nfo_path,
                    _fallback_cache=plex_item_cache,
                )
            except Exception as exc:
                logger.warning("Plex re-push failed for renamed file %s: %s", media.path, exc)

        report.record(FileResult(
            path=media.path,
            status="renamed",
            message=f"renamed from {media.renamed_from!r}",
        ))
        logger.info("Renamed: %s (was %r)", media.path, media.renamed_from)

    for media in remaining_files:
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

        # Case 4: call the plugin to fetch metadata from the external API.
        # A per-plugin timeout prevents a hung plugin from blocking the entire run;
        # the watchdog thread is not killed (Python limitation) but the run moves on.
        try:
            result = _call_plugin_with_timeout(
                plugin, parsed, config.plugin_fetch_timeout_secs
            )
        except ScrapeError as exc:
            # ScrapeError means the site changed its markup or the record is gone —
            # a distinct status so the user can distinguish "my plugin is broken"
            # from "the site changed" in the run report.
            logger.warning("Scrape error for %s: %s", media.path, exc)
            report.record(FileResult(path=media.path, status="scrape_error", message=str(exc)))
            continue
        except Exception as exc:
            logger.warning("Plugin error for %s: %s", media.path, exc)
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
        if dry_run:
            logger.info("[DRY RUN] Would push to Plex for: %s", media.path)
        elif plex_server is not None:
            try:
                ok = push_to_plex(plex_server, media.path, result, _fallback_cache=plex_item_cache)
                if not ok:
                    logger.warning("Plex push returned failure for %s", media.path)
            except Exception as exc:
                logger.warning("Plex push failed for %s: %s", media.path, exc)

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
        write_report(report, config.report_path, config.report_retention_days, app_name=config.app_name)

        # Fire the optional webhook with a compact run summary. Non-fatal: a webhook
        # failure never aborts the run or prevents the report from being written.
        if config.notify_url:
            _fire_webhook(config.notify_url, report, app_name=config.app_name)

    # Delete old log files beyond the retention window (runs regardless of dry_run)
    cleanup_old_files(config.log_path, config.log_retention_days, pattern_suffix=".log")

    logger.info(
        "Run complete. updated=%d renamed=%d skipped=%d unmatched=%d add_form=%d scrape_errors=%d errors=%d",
        report.updated, report.renamed, report.skipped, report.unmatched, report.add_form,
        report.scrape_errors, report.errors,
    )


def _run_with_media(config, router: Router, media_files: list) -> None:
    """Run a targeted pass over a pre-built list of MediaFile objects.

    Used by --retry-failed to re-process specific files without triggering a
    full library scan. Connects to Plex and writes the run report exactly as
    a normal run does.
    """
    run(config, router, media_files_override=media_files)


def _sweep_tmp_orphans(library_paths: list[str], max_age_secs: float = 1800) -> None:
    """Remove stale .tmp files left by interrupted atomic writes.

    Atomic writes use .tmp + os.replace(); if the process crashes after
    creating the .tmp but before the rename, the orphan lingers forever.
    A 30-minute threshold avoids racing with writes in progress.
    """
    cutoff = time.time() - max_age_secs
    for lib_path in library_paths:
        for tmp_file in glob.glob(os.path.join(lib_path, "**", "*.tmp"), recursive=True):
            try:
                if os.path.getmtime(tmp_file) < cutoff:
                    os.remove(tmp_file)
                    logger.info("Removed stale .tmp orphan: %s", tmp_file)
            except OSError:
                pass  # race between check and remove is harmless


def _validate_paths(config, library_only: bool = False) -> None:
    """
    Check that critical filesystem paths are accessible before the scheduler starts.
    Logs a clear error and raises SystemExit for each fatal misconfiguration so the
    container exits immediately with a useful message rather than silently doing nothing.

    When library_only=True, only library paths are checked (used for --list-unmatched
    which doesn't need write access to report/log dirs).
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

    if not library_only:
        # report_path and log_path must be writable (create them if absent — they may not exist yet)
        for path_name, path_val in [("REPORT_PATH", config.report_path), ("LOG_PATH", config.log_path)]:
            try:
                os.makedirs(path_val, exist_ok=True)
                test = os.path.join(path_val, ".write_test")
                with open(test, "w") as fh:
                    fh.write("")
                os.remove(test)
            except OSError as exc:
                errors.append(f"{path_name} {path_val!r} is not writable: {exc}")

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
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Re-process all error and scrape_error files from the most recent run, then exit",
    )
    args = parser.parse_args()

    # Load all settings from environment variables; fails fast if PLEX_URL/TOKEN missing
    config = load_config(force=args.force)

    # Set up logging before anything else so startup messages are captured
    setup_logging(config.log_path, config.log_level)

    logger.info("pm %s starting up", __version__)
    logger.info("Library paths: %s", config.library_paths)

    # --force without --once is almost certainly a mistake: it would re-process every
    # file on every scheduled run, defeating the purpose of the skip logic. Warn early,
    # before path validation, so the user sees it regardless of path issues.
    if args.force and not args.once and not args.list_unmatched:
        logger.warning(
            "--force was passed without --once; ignoring --force for scheduled runs. "
            "Use --once --force to re-process files in a one-shot run."
        )
        config.force = False

    # Validate the cron schedule before touching the filesystem.
    # Check field count first (fast), then attempt a full CronTrigger parse so
    # invalid values like "0 99 * * *" are caught with a clear message rather than
    # a cryptic APScheduler traceback when the scheduler first fires.
    parts = config.run_schedule.strip().split()
    if len(parts) != 5:
        logger.error(
            "RUN_SCHEDULE must be a 5-field cron expression (min hour day month weekday), "
            "got %r", config.run_schedule
        )
        raise SystemExit(1)
    try:
        from apscheduler.triggers.cron import CronTrigger as _CT
        _CT.from_crontab(config.run_schedule)
    except Exception as exc:
        logger.error("RUN_SCHEDULE %r is not a valid cron expression: %s", config.run_schedule, exc)
        raise SystemExit(1)

    # --list-unmatched only needs library paths to exist; it doesn't write reports or
    # logs, so skip the write-path validation that would block a diagnostic scan.
    if args.list_unmatched:
        _validate_paths(config, library_only=True)
    else:
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
        # Respects normal skip logic (honours --force if also passed) so the output
        # matches the set of files that would actually be processed in a real run.
        media_files, skipped_count = scan_library(
            config.library_paths,
            force=config.force,
            exclude_patterns=config.library_exclude_patterns,
        )
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

    if args.retry_failed:
        from app.web.history import list_runs, get_run as get_run_detail
        from app.scanner import MediaFile
        runs = list_runs(config.report_path)
        if not runs:
            print("No run reports found. Run pm at least once first.")
            raise SystemExit(0)
        latest_summary = runs[0]
        full_run = get_run_detail(config.report_path, latest_summary["filename"])
        if full_run is None:
            print(f"Could not read latest run report: {latest_summary['filename']}")
            raise SystemExit(1)
        failed_paths = [
            f["path"] for f in full_run.get("files", [])
            if f.get("status") in ("error", "scrape_error") and f.get("path")
        ]
        if not failed_paths:
            print(f"No errors in {latest_summary['filename']}. Nothing to retry.")
            raise SystemExit(0)
        print(f"Retrying {len(failed_paths)} failed file(s) from {latest_summary['filename']}:")
        for p in failed_paths:
            print(f"  {p}")

        # Build MediaFile objects only for the specific failed paths — do NOT call
        # scan_library with force=True, which would re-process the entire library.
        retry_media = []
        for path in failed_paths:
            if not os.path.isfile(path):
                logger.warning("Retry target no longer exists: %s", path)
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            nfo_path = os.path.join(os.path.dirname(path), f"{stem}.nfo")
            retry_media.append(MediaFile(path=path, stem=stem, nfo_path=nfo_path))

        _sweep_tmp_orphans(config.library_paths)
        _run_with_media(config, router, retry_media)
        raise SystemExit(0)

    # Sweep any stale .tmp orphans from previous interrupted writes before running.
    _sweep_tmp_orphans(config.library_paths)

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
