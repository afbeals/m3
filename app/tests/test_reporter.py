# Tests for app/reporter.py
from __future__ import annotations

import json
import os

import pytest

from app.reporter import FileResult, RunReport, write_report


# ---------------------------------------------------------------------------
# RunReport.record() — counter logic
# ---------------------------------------------------------------------------

def test_record_updated_increments_counter():
    report = RunReport()
    report.record(FileResult(path="/a.mp4", status="updated"))
    assert report.updated == 1
    assert report.total_scanned == 1


def test_record_all_statuses():
    report = RunReport()
    for status in ("updated", "skipped", "unmatched", "add_form", "scrape_error", "error"):
        report.record(FileResult(path=f"/{status}.mp4", status=status))
    assert report.total_scanned == 6
    assert report.updated == 1
    assert report.skipped == 1
    assert report.unmatched == 1
    assert report.add_form == 1
    assert report.scrape_errors == 1
    assert report.errors == 1


def test_record_scrape_error_increments_counter():
    report = RunReport()
    report.record(FileResult(path="/scene.mp4", status="scrape_error",
                             message="title not found at h1.scene-title"))
    assert report.scrape_errors == 1
    assert report.errors == 0
    assert report.total_scanned == 1


def test_record_multiple_updated():
    report = RunReport()
    for i in range(3):
        report.record(FileResult(path=f"/{i}.mp4", status="updated"))
    assert report.updated == 3
    assert report.total_scanned == 3


# ---------------------------------------------------------------------------
# write_report() — file output
# ---------------------------------------------------------------------------

def test_write_report_creates_json_and_txt(tmp_path):
    report = RunReport(started_at="2025-01-01T03:00:00", finished_at="2025-01-01T03:00:01")
    report.record(FileResult(path="/a.mp4", status="updated"))
    report.record(FileResult(path="/b.mp4", status="skipped"))

    write_report(report, str(tmp_path), retention_days=90)

    txt_path = tmp_path / "run_latest.txt"
    assert txt_path.exists()

    json_files = list(tmp_path.glob("run_*.json"))
    assert len(json_files) == 1


def test_write_report_json_content(tmp_path):
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/a.mp4", status="updated"))

    write_report(report, str(tmp_path), retention_days=90)

    json_file = list(tmp_path.glob("run_*.json"))[0]
    data = json.loads(json_file.read_text())

    assert data["updated"] == 1
    assert data["total_scanned"] == 1
    assert data["files"][0]["path"] == "/a.mp4"
    assert data["files"][0]["status"] == "updated"


def test_write_report_txt_contains_summary(tmp_path):
    report = RunReport(started_at="2025-05-15T03:00:01")
    report.record(FileResult(path="/a.mp4", status="updated"))
    report.record(FileResult(path="/b.mp4", status="unmatched"))

    write_report(report, str(tmp_path), retention_days=90)

    txt = (tmp_path / "run_latest.txt").read_text()
    assert "2025-05-15T03:00:01" in txt
    assert "Updated" in txt
    assert "Unmatched" in txt


def test_write_report_txt_lists_add_form_files(tmp_path):
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/Add Jane Doe.mp4", status="add_form", message="actors=['Jane Doe']"))

    write_report(report, str(tmp_path), retention_days=90)

    txt = (tmp_path / "run_latest.txt").read_text()
    assert "Manual Add files" in txt
    assert "/Add Jane Doe.mp4" in txt


def test_write_report_txt_lists_error_files(tmp_path):
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/bad.mp4", status="error", message="API timeout"))

    write_report(report, str(tmp_path), retention_days=90)

    txt = (tmp_path / "run_latest.txt").read_text()
    assert "Errors:" in txt
    assert "/bad.mp4" in txt
    assert "API timeout" in txt


def test_write_report_txt_lists_scrape_errors(tmp_path):
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/scene.mp4", status="scrape_error",
                             message="title not found at h1.scene-title"))

    write_report(report, str(tmp_path), retention_days=90)

    txt = (tmp_path / "run_latest.txt").read_text()
    assert "Scrape errors" in txt
    assert "/scene.mp4" in txt
    assert "h1.scene-title" in txt


def test_write_report_scrape_errors_appear_before_errors(tmp_path):
    """Scrape errors section must appear before generic errors in the text report."""
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/scene.mp4", status="scrape_error", message="selector missing"))
    report.record(FileResult(path="/crash.mp4", status="error", message="plugin crashed"))

    write_report(report, str(tmp_path), retention_days=90)

    txt = (tmp_path / "run_latest.txt").read_text()
    scrape_pos = txt.index("Scrape errors")
    error_pos = txt.index("Errors:")
    assert scrape_pos < error_pos


def test_write_report_txt_no_errors_message(tmp_path):
    report = RunReport(started_at="2025-01-01T03:00:00")

    write_report(report, str(tmp_path), retention_days=90)

    txt = (tmp_path / "run_latest.txt").read_text()
    assert "(none)" in txt


def test_write_report_creates_dir_if_missing(tmp_path):
    nested = tmp_path / "reports" / "subdir"
    report = RunReport(started_at="2025-01-01T03:00:00")

    write_report(report, str(nested), retention_days=90)

    assert (nested / "run_latest.txt").exists()


def test_write_report_json_is_valid_after_write(tmp_path):
    """JSON report must be readable immediately after write (atomic write guard)."""
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/a.mp4", status="updated"))

    write_report(report, str(tmp_path), retention_days=90)

    json_file = list(tmp_path.glob("run_*.json"))[0]
    data = json.loads(json_file.read_text())
    assert data["updated"] == 1


def test_write_report_no_tmp_file_left_behind(tmp_path):
    """No .tmp file should remain after a successful write."""
    report = RunReport(started_at="2025-01-01T03:00:00")
    write_report(report, str(tmp_path), retention_days=90)

    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []

# ---------------------------------------------------------------------------
# renamed counter
# ---------------------------------------------------------------------------

def test_record_renamed_increments_counter():
    report = RunReport()
    report.record(FileResult(path="/new name.mp4", status="renamed", message="renamed from 'old name'"))
    assert report.renamed == 1
    assert report.total_scanned == 1


def test_record_all_statuses_including_renamed():
    report = RunReport()
    for status in ("updated", "renamed", "skipped", "unmatched", "add_form", "scrape_error", "error"):
        report.record(FileResult(path=f"/{status}.mp4", status=status))
    assert report.total_scanned == 7
    assert report.renamed == 1


def test_write_report_txt_includes_renamed_count(tmp_path):
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/new.mp4", status="renamed", message="renamed from 'old'"))
    write_report(report, str(tmp_path), retention_days=90)
    txt = (tmp_path / "run_latest.txt").read_text()
    assert "Renamed" in txt
    assert "1" in txt


def test_write_report_json_includes_renamed_count(tmp_path):
    import json
    report = RunReport(started_at="2025-01-01T03:00:00")
    report.record(FileResult(path="/new.mp4", status="renamed", message="renamed from 'old'"))
    write_report(report, str(tmp_path), retention_days=90)
    json_files = list(tmp_path.glob("run_*.json"))
    assert json_files
    data = json.loads(json_files[0].read_text())
    assert data["renamed"] == 1
