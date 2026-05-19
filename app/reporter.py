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
#   "error"        — plugin or writer raised an unexpected exception
# -----------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from app.logging_setup import cleanup_old_files

logger = logging.getLogger(__name__)


@dataclass
class FileResult:
    path: str
    # One of: "updated", "renamed", "skipped", "unmatched", "add_form", "scrape_error", "error"
    status: str
    # Optional detail message (e.g. error text, parsed tokens for add_form)
    message: str = ""


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
    errors: int = 0

    # Full per-file results list (written verbatim to the JSON report)
    files: list[FileResult] = field(default_factory=list)

    def record(self, result: FileResult) -> None:
        """Append a file result and increment the appropriate counter."""
        self.files.append(result)
        self.total_scanned += 1
        if result.status == "updated":
            self.updated += 1
        elif result.status == "renamed":
            self.renamed += 1
        elif result.status == "skipped":
            self.skipped += 1
        elif result.status == "unmatched":
            self.unmatched += 1
        elif result.status == "add_form":
            self.add_form += 1
        elif result.status == "scrape_error":
            self.scrape_errors += 1
        elif result.status == "error":
            self.errors += 1
        else:
            logger.warning("Unknown FileResult status %r for %s — not counted in any bucket",
                           result.status, result.path)


def write_report(
    report: RunReport,
    report_path: str,
    retention_days: int,
    app_name: str = "pm",
) -> None:
    """Write the JSON and plain-text reports, then clean up old JSON reports."""
    os.makedirs(report_path, exist_ok=True)

    # Compute wall-clock duration if both timestamps are present
    if report.started_at and report.finished_at:
        try:
            start = datetime.fromisoformat(report.started_at)
            end = datetime.fromisoformat(report.finished_at)
            report.duration_seconds = max(0, int((end - start).total_seconds()))
        except ValueError:
            pass  # malformed timestamp — leave duration_seconds as None

    # Derive the filename from the run's start time so the filename is consistent
    # with the report contents. Fall back to the current time if started_at is empty.
    raw_ts = report.started_at or datetime.now().isoformat(timespec="seconds")
    ts = raw_ts.replace(":", "").replace("-", "").replace("T", "_")[:15]

    # --- JSON report (full detail, machine-readable) ---
    # Written atomically via .tmp + os.replace() — same guard as the text report.
    # A crash mid-write would otherwise leave a truncated JSON file that raises
    # json.JSONDecodeError in history.py and silently disappears from run history.
    json_path = os.path.join(report_path, f"run_{ts}.json")
    json_tmp = json_path + ".tmp"
    try:
        with open(json_tmp, "w") as fh:
            # asdict() converts the nested dataclasses to plain dicts for JSON serialisation
            json.dump(asdict(report), fh, indent=2)
        os.replace(json_tmp, json_path)
    except OSError as exc:
        logger.error("Could not write JSON report to %s: %s", json_path, exc)
        try:
            os.remove(json_tmp)
        except OSError:
            pass

    # --- Plain-text summary (human-readable, always overwritten) ---
    txt_path = os.path.join(report_path, "run_latest.txt")
    lines = [
        f"{app_name} run — {report.started_at}",
        "─" * 40,
        f"  Total files scanned                    : {report.total_scanned}",
        f"  Updated                                : {report.updated}",
        f"  Renamed (assets updated, no re-fetch)  : {report.renamed}",
        f"  Skipped (up-to-date)                   : {report.skipped}",
        f"  Manual Add (pending)                   : {report.add_form}",
        f"  Unmatched                              : {report.unmatched}",
        f"  Scrape errors (site change / not found): {report.scrape_errors}",
        f"  Errors (unexpected)                    : {report.errors}",
        "",
    ]

    # List Manual Add files separately — these need the user to add a plugin
    # or register the studio before they can be processed automatically
    add_files = [f for f in report.files if f.status == "add_form"]
    if add_files:
        lines.append("Manual Add files (no plugin; need studio setup):")
        for f in add_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    # Unmatched files — either unparseable or no plugin for the site token
    unmatched_files = [f for f in report.files if f.status == "unmatched"]
    if unmatched_files:
        lines.append("Unmatched files:")
        for f in unmatched_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    # Scrape errors — site markup changed or record no longer exists.
    # Listed before generic errors so they're the first thing the user sees.
    scrape_error_files = [f for f in report.files if f.status == "scrape_error"]
    if scrape_error_files:
        lines.append("Scrape errors (site may have changed its markup):")
        for f in scrape_error_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")

    # Error files — plugin crashed or writer failed
    error_files = [f for f in report.files if f.status == "error"]
    if error_files:
        lines.append("Errors:")
        for f in error_files:
            lines.append(f"  {f.path}")
            if f.message:
                lines.append(f"    → {f.message}")
        lines.append("")
    else:
        lines.append("Errors:")
        lines.append("  (none)")

    # Write atomically via .tmp + os.replace() so a crash mid-write never
    # leaves run_latest.txt in a corrupt/truncated state.
    txt_tmp = txt_path + ".tmp"
    try:
        with open(txt_tmp, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(txt_tmp, txt_path)
        logger.info("Report written to %s", txt_path)
    except OSError as exc:
        logger.error("Could not write text report to %s: %s", txt_path, exc)
        try:
            os.remove(txt_tmp)
        except OSError:
            pass

    # Delete old JSON report files beyond the retention window.
    # run_latest.txt is excluded because it has no timestamp suffix.
    removed = cleanup_old_files(report_path, retention_days, pattern_suffix=".json")
    if removed:
        logger.info("Cleaned up %d old report file(s)", removed)
