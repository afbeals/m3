# -----------------------------------------------------------------------------
# app/reporter.py
#
# Tracks the result of every file processed during a run and writes two
# output files at the end:
#
#   run_<timestamp>.json  — full machine-readable report with per-file details;
#                           kept for REPORT_RETENTION_DAYS then auto-deleted.
#   run_latest.txt        — human-readable summary, always overwritten so you
#                           can check the most recent run at a glance.
#
# File statuses:
#   "updated"      — metadata fetched and written successfully (NFO + Plex)
#   "renamed"      — file was renamed; assets renamed on disk, Plex re-pushed from NFO
#   "skipped"      — file already had a sidecar and --force was not set
#   "unmatched"    — filename couldn't be parsed or no plugin registered for site
#   "add_form"     — filename used the Manual Add form; needs human follow-up
#   "scrape_error" — plugin raised ScrapeError/SelectorMissingError; site may
#                    have changed its markup or the record no longer exists
#   "image_error"  — NFO written successfully but one or more images failed to download
#   "error"        — plugin or writer raised an unexpected exception
# -----------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from app.logging_setup import cleanup_old_files
from app.utils import atomic_replace

logger = logging.getLogger(__name__)


@dataclass
class FileResult:
    path: str
    # One of: "updated", "renamed", "skipped", "unmatched", "add_form", "scrape_error", "image_error", "error"
    status: str
    # Optional detail message (e.g. error text, parsed tokens for add_form)
    message: str = ""
    # True when the NFO was written but the Plex push returned False
    plex_failed: bool = False


@dataclass
class RunReport:
    # ISO timestamps set by main.run() at the start and end of each run
    started_at: str = ""
    finished_at: str = ""
    # Wall-clock duration in seconds; computed by write_report() when both timestamps are present
    duration_seconds: int | None = None

    # Counters — updated by record() as each file result comes in
    total_scanned: int = 0
    updated: int = 0
    renamed: int = 0
    skipped: int = 0
    unmatched: int = 0
    add_form: int = 0
    scrape_errors: int = 0
    image_errors: int = 0
    errors: int = 0

    # Full per-file results list (written verbatim to the JSON report)
    files: list[FileResult] = field(default_factory=list)

    def record(self, result: FileResult) -> None:
        """Append a file result and increment the appropriate counter."""
        self.files.append(result)
        if result.status == "updated":
            self.total_scanned += 1
            self.updated += 1
        elif result.status == "renamed":
            self.total_scanned += 1
            self.renamed += 1
        elif result.status == "skipped":
            self.total_scanned += 1
            self.skipped += 1
        elif result.status == "unmatched":
            self.total_scanned += 1
            self.unmatched += 1
        elif result.status == "add_form":
            self.total_scanned += 1
            self.add_form += 1
        elif result.status == "scrape_error":
            self.total_scanned += 1
            self.scrape_errors += 1
        elif result.status == "image_error":
            self.total_scanned += 1
            self.image_errors += 1
        elif result.status == "error":
            self.total_scanned += 1
            self.errors += 1
        else:
            logger.warning("Unknown FileResult status %r for %s — not counted in any bucket",
                           result.status, result.path)


def write_report(
    report: RunReport,
    report_path: str,
    retention_days: int,
    app_name: str = "m3",
) -> None:
    """Write the JSON and plain-text reports, then clean up old JSON reports."""
    try:
        os.makedirs(report_path, exist_ok=True)
    except OSError as exc:
        logger.error("Could not create report directory %s: %s", report_path, exc)
        raise

    # Compute wall-clock duration if both timestamps are present
    if report.started_at and report.finished_at:
        try:
            start = datetime.fromisoformat(report.started_at)
            end = datetime.fromisoformat(report.finished_at)
            report.duration_seconds = max(0, int(round((end - start).total_seconds())))
        except ValueError:
            pass  # malformed timestamp — leave duration_seconds as None

    # Derive the filename from the run's start time so the filename is consistent
    # with the report contents. Fall back to the current time if started_at is empty.
    try:
        ts = datetime.fromisoformat(report.started_at).strftime("%Y%m%d_%H%M%S")
    except (ValueError, TypeError):
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    duration_str = (
        f"{report.duration_seconds}s" if report.duration_seconds is not None else "N/A"
    )

    # --- JSON report (full detail, machine-readable) ---
    # Written atomically via .tmp + os.replace() — same guard as the text report.
    # A crash mid-write would otherwise leave a truncated JSON file that raises
    # json.JSONDecodeError in history.py and silently disappears from run history.
    json_path = os.path.join(report_path, f"run_{ts}.json")
    json_tmp = json_path + ".tmp"
    try:
        with open(json_tmp, "w", encoding="utf-8") as fh:
            # asdict() converts the nested dataclasses to plain dicts for JSON serialisation
            json.dump(asdict(report), fh, indent=2)
        atomic_replace(json_tmp, json_path)
    except Exception as exc:
        logger.error("Could not write JSON report to %s: %s", json_path, exc)
    finally:
        try:
            os.remove(json_tmp)
        except FileNotFoundError:
            pass  # already moved by atomic_replace or never created
        except OSError as _e:
            logger.debug("Could not remove .tmp file %s: %s", json_tmp, _e)

    # --- Plain-text summary (human-readable, always overwritten) ---
    txt_path = os.path.join(report_path, "run_latest.txt")
    plex_failed_count = sum(1 for f in report.files if f.plex_failed)
    lines = [
        f"{app_name} run — {report.started_at}  (duration: {duration_str})",
        "─" * 40,
        f"  Total files scanned                    : {report.total_scanned}",
        f"  Updated                                : {report.updated}"
        + (f" ({plex_failed_count} Plex push failed)" if plex_failed_count else ""),
        f"  Renamed (assets updated, no re-fetch)  : {report.renamed}",
        f"  Skipped (up-to-date)                   : {report.skipped}",
        f"  Manual Add (pending)                   : {report.add_form}",
        f"  Unmatched                              : {report.unmatched}",
        f"  Scrape errors (site change / not found): {report.scrape_errors}",
        f"  Image errors (NFO ok, images failed)   : {report.image_errors}",
        f"  Errors (unexpected)                    : {report.errors}",
        "",
    ]

    # Single pass over report.files to group by status (avoids 5 separate linear scans).
    by_status: dict[str, list[FileResult]] = defaultdict(list)
    for f in report.files:
        by_status[f.status].append(f)

    add_files = by_status.get("add_form", [])
    if add_files:
        lines.append("Manual Add files (no plugin; need studio setup):")
        for f in add_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    unmatched_files = by_status.get("unmatched", [])
    if unmatched_files:
        lines.append("Unmatched files:")
        for f in unmatched_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    image_error_files = by_status.get("image_error", [])
    if image_error_files:
        lines.append("Image errors (NFO written, images missing):")
        for f in image_error_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    scrape_error_files = by_status.get("scrape_error", [])
    if scrape_error_files:
        lines.append("Scrape errors (site may have changed its markup):")
        for f in scrape_error_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    error_files = by_status.get("error", [])
    if error_files:
        lines.append("Errors:")
        for f in error_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    # Write atomically via .tmp + os.replace() so a crash mid-write never
    # leaves run_latest.txt in a corrupt/truncated state.
    txt_tmp = txt_path + ".tmp"
    try:
        with open(txt_tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        atomic_replace(txt_tmp, txt_path)
        logger.info("Report written to %s", txt_path)
    except Exception as exc:
        logger.error("Could not write text report to %s: %s", txt_path, exc)
    finally:
        try:
            os.remove(txt_tmp)
        except FileNotFoundError:
            pass  # already moved by atomic_replace or never created
        except OSError as _e:
            logger.debug("Could not remove .tmp file %s: %s", txt_tmp, _e)

    # Delete old JSON report files beyond the retention window.
    # run_latest.txt is excluded because it has no timestamp suffix.
    try:
        removed = cleanup_old_files(report_path, retention_days, pattern_suffix=".json", app_name=app_name)
        if removed:
            logger.info("Cleaned up %d old report file(s)", removed)
    except OSError as exc:
        logger.warning("Could not clean up old report files in %s: %s", report_path, exc)
