# -----------------------------------------------------------------------------
# app/main.py
#
# Entrypoint for the m3 metadata agent.
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

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import time
import traceback as _tb
from datetime import datetime, timezone

# Load .env file if present (local development convenience).
# Only applied when the file exists — production containers set env vars directly
# and should not have a .env file, so this is a no-op in production.
try:
    from dotenv import load_dotenv
    if os.path.isfile(".env"):
        load_dotenv(override=False)  # override=False: container env vars take precedence
except ImportError:
    pass  # python-dotenv not installed — silently skip

from app import __version__
from app.config import Config, load_config
from app.utils import call_with_timeout
from app.logging_setup import setup_logging, cleanup_old_files
from app.plugins.loader import load_plugins
from app.reporter import RunReport, FileResult, write_report
from app.router import Router
from app.runstate import RunState
from app.scanner import MediaFile, scan_library
from app.scrape import ScrapeError

from app.writers.nfo import write_nfo, write_images, rename_nfo_assets
from app.writers.plex import connect_plex, push_to_plex, push_nfo_to_plex

logger = logging.getLogger(__name__)

_WEBHOOK_TIMEOUT_SECS = 10
_TRACEBACK_LIMIT = 5
_TRACEBACK_MAX_CHARS = 500


def _fire_webhook(url: str, report: RunReport, *, app_name: str = "m3") -> None:
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
                    "User-Agent": f"m3-metadata-agent/{__version__}",
                },
            )
            logger.info("Webhook notification sent to %s (HTTP %d)", url, r.status_code)
    except Exception as exc:
        logger.warning("Webhook notification failed for %s: %s", url, exc)



def run(
    config: Config,
    router: Router,
    dry_run: bool = False,
    dry_run_strict: bool = False,
    media_files_override: list[MediaFile] | None = None,
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
    if dry_run_strict:
        logger.info("Run started [DRY RUN STRICT — no API calls, no writes]")
        logger.info("DRY RUN STRICT mode — no files written, no Plex updated, no plugin fetches")
    elif dry_run:
        logger.info("Run started [DRY RUN]")
        logger.info("DRY RUN mode — no files or Plex records will be written")
    else:
        logger.info("Run started")

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

    report = RunReport(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
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

    # Per-plugin file counts for the dry-run-strict routing summary
    _strict_counts: dict[str, int] = {}

    _total_remaining = len(remaining_files)
    for _file_idx, media in enumerate(remaining_files, 1):
        # Ask the router to parse the filename and find the right plugin
        parsed, plugin = router.dispatch(media.stem)

        _pfx = f"[{_file_idx}/{_total_remaining}]"

        # Case 1: filename couldn't be decoded at all
        if parsed is None:
            logger.info("%s Unmatched (could not parse): %s", _pfx, media.path)
            report.record(FileResult(
                path=media.path,
                status="unmatched",
                message="could not parse filename — check format against FILENAME_PATTERNS.md",
            ))
            continue

        # Case 2: Manual Add form — no plugin needed, flag for human review
        if parsed.form == "add":
            msg = f"actors={parsed.actors} title={parsed.title!r} studio={parsed.studio!r}"
            logger.info("%s Manual Add form (no plugin): %s | %s", _pfx, media.path, msg)
            report.record(FileResult(path=media.path, status="add_form", message=msg))
            continue

        # Case 3: parsed OK but no plugin is registered for this site
        if plugin is None:
            logger.info("%s Unmatched (no plugin for site %r): %s", _pfx, parsed.site, media.path)
            _tokens = (
                f"site={parsed.site!r} subtype={parsed.match_subtype!r}"
                + (f" scene_id={parsed.scene_id!r}" if parsed.scene_id else "")
            )
            report.record(FileResult(
                path=media.path,
                status="unmatched",
                message=f"no plugin registered for site {parsed.site!r} ({_tokens})",
            ))
            continue

        # Case 4: call the plugin to fetch metadata from the external API.
        # A per-plugin timeout prevents a hung plugin from blocking the entire run;
        # the watchdog thread is not killed (Python limitation) but the run moves on.
        # dry_run_strict skips the fetch entirely so developers can test routing offline.
        if dry_run_strict:
            plugin_name = type(plugin).__name__
            logger.info("%s [DRY RUN STRICT] Would fetch from plugin %s for: %s",
                        _pfx, plugin_name, media.path)
            _strict_counts[plugin_name] = _strict_counts.get(plugin_name, 0) + 1
            report.record(FileResult(path=media.path, status="skipped",
                                     message="dry-run-strict: fetch skipped"))
            continue
        try:
            result = call_with_timeout(
                (lambda p=parsed, pl=plugin: pl.fetch(p)),
                config.plugin_fetch_timeout_secs,
                description="plugin fetch",
            )
        except ScrapeError as exc:
            # ScrapeError means the site changed its markup or the record is gone —
            # a distinct status so the user can distinguish "my plugin is broken"
            # from "the site changed" in the run report.
            logger.warning("Scrape error for %s: %s", media.path, exc)
            report.record(FileResult(path=media.path, status="scrape_error", message=str(exc)))
            continue
        except TimeoutError as exc:
            # Plugin fetch exceeded the configured timeout — treat as scrape_error so
            # users can distinguish a hung plugin from an unexpected crash.
            logger.warning("Plugin fetch timed out for %s: %s", media.path, exc)
            report.record(FileResult(path=media.path, status="scrape_error", message=f"fetch timed out: {exc}"))
            continue
        except Exception as exc:
            tb_short = _tb.format_exc(limit=_TRACEBACK_LIMIT)[-_TRACEBACK_MAX_CHARS:]
            logger.warning("Plugin error for %s: %s\n%s", media.path, exc, tb_short)
            report.record(FileResult(path=media.path, status="error", message=str(exc)))
            continue

        # Plugin can return None if the lookup found nothing (e.g. scene not in database)
        if result is None:
            logger.info("%s Plugin returned no result for: %s", _pfx, media.path)
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

        # Write images first so we know the actual filenames (extension derived from
        # Content-Type) before writing the NFO <art> block.
        if dry_run:
            logger.info("[DRY RUN] Would write NFO + images for: %s", media.path)
        else:
            try:
                images_ok, poster_path, fanart_path = write_images(media, result)
            except Exception as exc:
                logger.exception("Image write error for %s", media.path)
                report.record(FileResult(path=media.path, status="error", message=str(exc)))
                continue
            if not images_ok:
                logger.warning(
                    "Image download failed for %s — NFO not written, images missing. "
                    "Recording as image_error.",
                    media.path,
                )
                report.record(FileResult(
                    path=media.path,
                    status="image_error",
                    message="NFO not written — one or more images failed to download",
                ))
                # Images failed; skip NFO write and Plex push
                continue

            # Build bare filenames for the NFO <art> block using the actual written paths
            poster_filename = os.path.basename(poster_path) if poster_path else None
            fanart_filename = os.path.basename(fanart_path) if fanart_path else None

            try:
                write_nfo(media, result,
                          poster_filename=poster_filename,
                          fanart_filename=fanart_filename)
            except Exception as exc:
                logger.exception("NFO write error for %s", media.path)
                report.record(FileResult(path=media.path, status="error", message=str(exc)))
                continue

        # Push the same metadata to Plex with field locks so Plex's built-in agent
        # won't overwrite our values on the next scheduled refresh.
        # This is non-fatal: if Plex is unreachable we still have the NFO sidecar.
        plex_failed = False
        if dry_run:
            logger.info("[DRY RUN] Would push to Plex for: %s", media.path)
        elif plex_server is not None:
            try:
                ok = push_to_plex(plex_server, media.path, result, _fallback_cache=plex_item_cache)
                if not ok:
                    logger.warning("Plex push returned failure for %s", media.path)
                    plex_failed = True
            except Exception as exc:
                logger.warning("Plex push failed for %s: %s", media.path, exc)
                plex_failed = True

        report.record(FileResult(path=media.path, status="updated", plex_failed=plex_failed))
        logger.info("%s Updated: %s", _pfx, media.path)

    # Print a structured routing summary when running in dry-run-strict mode so
    # developers can see at a glance which plugins would handle which files.
    if dry_run_strict:
        print("\nPlugin routing summary (dry-run-strict):")
        for plugin_name, count in sorted(_strict_counts.items(), key=lambda x: -x[1]):
            print(f"  {plugin_name:<30} : {count} file(s)")
        if report.unmatched:
            print(f"  {'Unmatched':<30} : {report.unmatched} file(s)")
        if report.add_form:
            print(f"  {'Add-form (no plugin)':<30} : {report.add_form} file(s)")
        print()

    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Dry runs don't write report files — they'd pollute history with zero-action entries.
    # The summary is logged to stdout instead so the user still sees what would have run.
    if dry_run:
        logger.info(
            "[DRY RUN] Skipping report write. "
            "updated=%d renamed=%d skipped=%d unmatched=%d add_form=%d scrape_errors=%d image_errors=%d errors=%d",
            report.updated, report.renamed, report.skipped, report.unmatched, report.add_form,
            report.scrape_errors, report.image_errors, report.errors,
        )
    else:
        # Write JSON + plain-text report files and clean up old ones
        try:
            write_report(report, config.report_path, config.report_retention_days, app_name=config.app_name)
        except Exception as exc:
            logger.error("Failed to write run report: %s", exc)

        # Fire the optional webhook with a compact run summary. Non-fatal: a webhook
        # failure never aborts the run or prevents the report from being written.
        # NOTIFY_MIN_ERRORS gates the webhook: when set to 1 the webhook only fires
        # when something broke, avoiding noise on clean nightly runs.
        if config.notify_url:
            total_errors = report.errors + report.scrape_errors + report.image_errors
            if total_errors >= config.notify_min_errors:
                _fire_webhook(config.notify_url, report, app_name=config.app_name)

    # Delete old log files beyond the retention window (runs regardless of dry_run)
    cleanup_old_files(config.log_path, config.log_retention_days, pattern_suffix=".log", app_name=config.app_name)

    logger.info(
        "Run complete. updated=%d renamed=%d skipped=%d unmatched=%d add_form=%d "
        "scrape_errors=%d image_errors=%d errors=%d",
        report.updated, report.renamed, report.skipped, report.unmatched, report.add_form,
        report.scrape_errors, report.image_errors, report.errors,
    )


def _run_with_media(config: Config, router: Router, media_files: list[MediaFile], dry_run: bool = False) -> None:
    """Run a targeted pass over a pre-built list of MediaFile objects.

    Used by --retry-failed to re-process specific files without triggering a
    full library scan. Connects to Plex and writes the run report exactly as
    a normal run does.
    """
    run(config, router, media_files_override=media_files, dry_run=dry_run)


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
                if not os.path.isfile(tmp_file):
                    continue
                if os.path.getmtime(tmp_file) < cutoff:
                    os.remove(tmp_file)
                    logger.info("Removed stale .tmp orphan: %s", tmp_file)
            except OSError as exc:
                logger.debug("Could not remove stale .tmp file %s: %s", tmp_file, exc)


def _validate_paths(config: Config, library_only: bool = False) -> None:
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
                test = os.path.join(path_val, f".write_test.{os.getpid()}")
                try:
                    with open(test, "w") as fh:
                        fh.write("")
                finally:
                    try:
                        os.remove(test)
                    except OSError:
                        pass
            except OSError as exc:
                errors.append(f"{path_name} {path_val!r} is not writable: {exc}")

    for msg in errors:
        logger.error("Startup validation failed: %s", msg)
    if errors:
        raise SystemExit(1)


def _run_validate_plugins(plugin_dir: str) -> None:
    """Load all plugins and print a structured pass/fail validation report."""
    print(f"Validating plugins in: {plugin_dir}\n")
    if not os.path.isdir(plugin_dir):
        print(f"ERROR: plugin directory not found: {plugin_dir}")
        return

    import importlib.util, inspect, sys as _sys
    from app.plugins.base import MetadataPlugin as _MP

    files = sorted(f for f in os.listdir(plugin_dir)
                   if f.endswith(".py") and not f.startswith("_"))
    if not files:
        print("No plugin files found.")
        return

    any_fail = False
    for fname in files:
        fpath = os.path.join(plugin_dir, fname)
        issues = []
        classes_found = []
        try:
            module_name = f"m3_plugin_validate.{fname[:-3]}"
            spec = importlib.util.spec_from_file_location(module_name, fpath)
            if spec is None or spec.loader is None:
                issues.append(f"  - could not build module spec for {fpath}")
            else:
                module = importlib.util.module_from_spec(spec)
                _sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                finally:
                    _sys.modules.pop(module_name, None)
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if obj is _MP or not issubclass(obj, _MP):
                        continue
                    classes_found.append(obj.__name__)
                    if not obj.site_id:
                        issues.append(f"  - {obj.__name__}: missing site_id")
                    elif not isinstance(obj.site_id, str):
                        issues.append(f"  - {obj.__name__}: site_id must be a str")
                    if not callable(getattr(obj, "fetch", None)):
                        issues.append(f"  - {obj.__name__}: missing fetch() method")
        except Exception as exc:
            issues.append(f"  - import error: {exc}")
        finally:
            _sys.modules.pop(f"m3_plugin_validate.{fname[:-3]}", None)

        if not classes_found and not issues:
            issues.append("  - no MetadataPlugin subclass found")

        status = "PASS" if not issues else "FAIL"
        if issues:
            any_fail = True
        classes_str = ", ".join(classes_found) if classes_found else "(none)"
        print(f"[{status}] {fname}  classes={classes_str}")
        for issue in issues:
            print(issue)

    print()
    if any_fail:
        print("Validation FAILED — fix the issues above before deploying.")
        raise SystemExit(1)
    else:
        print("All plugins passed validation.")


def _run_test_plugin(plugin_file: str, filename_stem: str | None) -> None:
    """Load a single plugin file and call fetch() on a test filename stem."""
    import importlib.util, inspect, sys as _sys
    from app.plugins.base import MetadataPlugin as _MP
    from app.parser import parse

    if not os.path.isfile(plugin_file):
        print(f"ERROR: plugin file not found: {plugin_file}")
        raise SystemExit(1)

    stem = filename_stem or ""
    if not stem:
        print("ERROR: --filename is required with --test-plugin")
        print("  Example: --filename \"Jane Doe % mysite - 12345\"")
        raise SystemExit(1)

    # Load the plugin
    module_name = "m3_plugin_test._testplugin"
    spec = importlib.util.spec_from_file_location(module_name, plugin_file)
    if spec is None or spec.loader is None:
        print(f"ERROR: could not build module spec for {plugin_file}")
        raise SystemExit(1)
    try:
        module = importlib.util.module_from_spec(spec)
        _sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            _sys.modules.pop(module_name, None)
    except Exception as exc:
        print(f"ERROR: could not import {plugin_file}: {exc}")
        raise SystemExit(1)

    plugin_classes = [
        obj for _, obj in inspect.getmembers(module, inspect.isclass)
        if obj is not _MP and issubclass(obj, _MP) and obj.site_id
    ]
    if not plugin_classes:
        print(f"ERROR: no MetadataPlugin subclass with site_id found in {plugin_file}")
        raise SystemExit(1)

    plugin = plugin_classes[0]()
    print(f"Plugin:   {type(plugin).__name__}  (site_id={plugin.site_id!r})")
    print(f"Filename: {stem!r}")

    parsed = parse(stem)
    if parsed is None:
        print("\nERROR: filename could not be parsed — check the format")
        raise SystemExit(1)

    print(f"Parsed:   form={parsed.form!r}  subtype={parsed.match_subtype!r}")
    _pf = parsed
    for _k, _v in [
        ("site",              _pf.site),
        ("scene_id",          _pf.scene_id),
        ("direct_url",        _pf.direct_url),
        ("date",              _pf.date),
        ("title",             _pf.title),
        ("actors",            _pf.actors or None),
        ("genres",            _pf.genres or None),
        ("extra_actors",      _pf.extra_actors or None),
        ("raw_match_payload", _pf.raw_match_payload),
        ("studio",            _pf.studio),
        ("studio_id",         _pf.studio_id),
        ("actress_id",        _pf.actress_id),
    ]:
        if _v is not None:
            print(f"          {_k}={_v!r}")

    if parsed.site and parsed.site.lower() not in plugin.all_ids():
        print(f"\nWARNING: parsed site {parsed.site!r} does not match plugin ids {plugin.all_ids()}")

    print("\nCalling plugin.fetch() …")
    try:
        result = plugin.fetch(parsed)
    except Exception as exc:
        print(f"\nERROR: plugin.fetch() raised: {exc}")
        raise SystemExit(1)

    if result is None:
        print("\nResult: None  (plugin returned no match)")
        return

    print(f"\nResult:")
    print(f"  title:          {result.title!r}")
    print(f"  summary:        {result.summary!r}")
    print(f"  rating:         {result.rating}")
    print(f"  year:           {result.year}")
    print(f"  content_rating: {result.content_rating!r}")
    print(f"  genres:         {result.genres}")
    print(f"  tags:           {result.tags}")
    print(f"  labels:         {result.labels}")
    print(f"  actors:         {result.actors}")
    print(f"  poster_url:     {result.poster_url!r}")
    print(f"  fanart_url:     {result.fanart_url!r}")
    print(f"  source_url:     {result.source_url!r}")
    print(f"  source_id:      {result.source_id!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description="m3 — Plex Metadata Agent")
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
        nargs="?",
        const="1",
        default=None,
        metavar="N",
        help=(
            "Re-process error and scrape_error files from the N most recent runs "
            "(default: 1 = most recent run only), then exit"
        ),
    )
    parser.add_argument(
        "--dry-run-strict",
        action="store_true",
        help=(
            "Like --dry-run but also skips plugin fetch() calls — "
            "tests filename parsing and routing with zero network access; "
            "no API keys required"
        ),
    )
    parser.add_argument(
        "--test-plugin",
        metavar="PLUGIN_FILE",
        default=None,
        help=(
            "Load a single plugin file, call fetch() on a test filename, "
            "and print the result. Use with --filename to supply the test stem."
        ),
    )
    parser.add_argument(
        "--filename",
        metavar="STEM",
        default=None,
        help="Filename stem (no extension) to use with --test-plugin",
    )
    parser.add_argument(
        "--validate-plugins",
        action="store_true",
        help="Load all plugins, check for structural issues, print a report, then exit",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Watch PLUGIN_DIR for .py changes and auto-reload plugins "
            "(cross-platform alternative to SIGUSR2; requires watchfiles)"
        ),
    )
    args = parser.parse_args()

    # --dry-run-strict implies --dry-run
    if args.dry_run_strict:
        args.dry_run = True

    # Some modes never connect to Plex — skip the PLEX_URL/TOKEN requirement for them
    _plex_free = bool(args.validate_plugins or args.test_plugin or
                      args.dry_run_strict or args.list_unmatched)

    try:
        config = load_config(force=args.force, plex_required=not _plex_free)
    except ValueError as exc:
        import sys as _sys
        _sys.stderr.write(
            f"\n[m3] Configuration error: {exc}\n"
            "Copy .env.example to .env and fill in the required values.\n\n"
        )
        raise SystemExit(1)

    # Set up logging before anything else so startup messages are captured
    setup_logging(config.log_path, config.log_level, app_name=config.app_name)

    logger.info("m3 %s starting up", __version__)
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
        _cron_trigger = _CT.from_crontab(config.run_schedule)
    except Exception as exc:
        logger.error("RUN_SCHEDULE %r is not a valid cron expression: %s", config.run_schedule, exc)
        raise SystemExit(1)

    if args.validate_plugins:
        _run_validate_plugins(config.plugin_dir)
        raise SystemExit(0)

    if args.test_plugin:
        _run_test_plugin(args.test_plugin, args.filename)
        raise SystemExit(0)

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
            if parsed is None or (plugin is None and parsed.form != "add"):
                unroutable.append(media.path)
        if unroutable:
            print(f"{len(unroutable)} unmatched file(s):")
            for path in unroutable:
                print(f"  {path}")
        else:
            print("All files matched a plugin.")
        return

    if args.retry_failed is not None:
        from app.web.history import list_runs, get_run as get_run_detail

        try:
            num_runs = max(1, int(args.retry_failed))
        except (ValueError, TypeError):
            logger.error("--retry-failed requires a positive integer, got %r", args.retry_failed)
            raise SystemExit(1)

        runs = list_runs(config.report_path)
        if not runs:
            print("No run reports found. Run m3 at least once first.")
            raise SystemExit(0)

        target_runs = runs[:num_runs]
        failed_paths: list[str] = []
        seen_paths: set[str] = set()
        for summary in target_runs:
            full_run = get_run_detail(config.report_path, summary["filename"])
            if full_run is None:
                print(f"Could not read run report: {summary['filename']}")
                continue
            for f in full_run.get("files", []):
                if f.get("status") in ("error", "scrape_error") and f.get("path"):
                    p = f["path"]
                    if p not in seen_paths:
                        seen_paths.add(p)
                        failed_paths.append(p)

        if not failed_paths:
            label = "most recent run" if num_runs == 1 else f"{num_runs} most recent runs"
            print(f"No errors in the {label}. Nothing to retry.")
            raise SystemExit(0)

        label = target_runs[0]["filename"] if num_runs == 1 else f"{num_runs} runs"
        print(f"Retrying {len(failed_paths)} failed file(s) from {label}:")
        for p in failed_paths:
            print(f"  {p}")

        # Build MediaFile objects only for the specific failed paths — do NOT call
        # scan_library with force=True, which would re-process the entire library.
        retry_paths = failed_paths
        retry_media = []
        for path in retry_paths:
            if not os.path.isfile(path):
                logger.warning("Retry target no longer exists: %s", path)
                continue
            stem = os.path.splitext(os.path.basename(path))[0]
            nfo_path = os.path.join(os.path.dirname(path), f"{stem}.nfo")
            retry_media.append(MediaFile(path=path, stem=stem, nfo_path=nfo_path))

        logger.info(
            "Retrying %d of %d failed files (%d paths not found on disk)",
            len(retry_media), len(retry_paths), len(retry_paths) - len(retry_media),
        )

        _sweep_tmp_orphans(config.library_paths)
        _run_with_media(config, router, retry_media, dry_run=args.dry_run)
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
            run(config, router, dry_run=args.dry_run,
                dry_run_strict=args.dry_run_strict)
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
    register_sigusr2_reload(registry, config.plugin_dir, scheduler=scheduler)

    # --watch: cross-platform file watcher that auto-reloads plugins when any .py
    # file in PLUGIN_DIR changes. Useful for local development on Windows where
    # SIGUSR2 is not available. Requires watchfiles (pip install watchfiles).
    if args.watch:
        try:
            from watchfiles import watch as _watch
            import threading as _threading

            def _plugin_watcher():
                logger.info("--watch: watching %s for plugin changes", config.plugin_dir)
                try:
                    for _ in _watch(config.plugin_dir, watch_filter=lambda _c, p: p.endswith(".py")):
                        logger.info("--watch: change detected — reloading plugins")
                        try:
                            from app.plugins.loader import load_plugins as _lp
                            prev_count = len(registry)
                            new_reg = _lp(config.plugin_dir)
                            registry.clear()
                            registry.update(new_reg)
                            if not new_reg:
                                logger.warning(
                                    "--watch: reload produced an empty plugin registry — "
                                    "all files will be unmatched until a valid plugin is saved"
                                )
                            elif len(new_reg) < prev_count:
                                logger.warning(
                                    "--watch: plugin count dropped from %d to %d after reload — "
                                    "check for syntax errors or removed site_id values",
                                    prev_count, len(new_reg),
                                )
                            else:
                                logger.info("--watch: reloaded %d plugin(s)", len(new_reg))
                        except Exception as exc:
                            logger.warning("--watch: reload failed: %s", exc)
                except Exception:
                    logger.exception("--watch: watcher thread crashed; plugin auto-reload disabled")

            _threading.Thread(target=_plugin_watcher, daemon=True, name="m3-watcher").start()
        except ImportError:
            logger.warning(
                "--watch requires watchfiles: pip install watchfiles  "
                "(or: pip install -r requirements-dev.txt)"
            )

    # Launch the web dashboard in a daemon thread so it runs alongside the scheduler.
    # The scheduler keeps the main thread; the web server is the side thread.
    # Note: uvicorn reload mode requires running as the main process and cannot be used
    # in a daemon thread. Use `python -m uvicorn app.web:create_app --reload` for live
    # reload during UI/template development.
    debug_mode = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
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
            name="m3-web",
        )
        web_thread.start()

        def _web_watchdog():
            web_thread.join()
            logger.error(
                "Web server thread exited unexpectedly — dashboard is offline. "
                "Check logs for uvicorn errors."
            )

        threading.Thread(target=_web_watchdog, daemon=True, name="m3-web-watchdog").start()

        if debug_mode:
            logger.info(
                "Web dashboard started on http://%s:%d (DEBUG mode — "
                "for live reload run: python -m uvicorn app.web:create_app --reload)",
                config.web_host, config.web_port,
            )
        else:
            logger.info("Web dashboard started on http://%s:%d", config.web_host, config.web_port)

    # Print a structured startup health summary to stdout so developers and ops
    # can confirm the key configuration at a glance before the first scheduled run.
    _next_run = _cron_trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    _next_str = _next_run.strftime("%Y-%m-%d %H:%M:%S") if _next_run else "unknown"
    _lib_status = ", ".join(
        f"{p} ({'ok' if os.path.isdir(p) else 'MISSING'})"
        for p in config.library_paths
    )
    print(
        f"\nm3 {__version__} ready"
        f"\n  Plugins loaded : {len(registry)}"
        f"\n  Library paths  : {_lib_status}"
        f"\n  Next run       : {_next_str}  ({config.run_schedule})"
        + (f"\n  Dashboard      : http://{config.web_host}:{config.web_port}" if config.web_enabled else "")
        + "\n"
    )

    # Start the blocking scheduler (blocks until container stops)
    logger.info("Scheduler started. Next run scheduled via: %s", config.run_schedule)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
    except Exception:
        logger.exception("Scheduler crashed unexpectedly")


if __name__ == "__main__":
    main()
