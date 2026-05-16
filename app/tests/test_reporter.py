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
    for status in ("updated", "skipped", "unmatched", "add_form", "error"):
        report.record(FileResult(path=f"/{status}.mp4", status=status))
    assert report.total_scanned == 5
    assert report.updated == 1
    assert report.skipped == 1
    assert report.unmatched == 1
    assert report.add_form == 1
    assert report.errors == 1


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
