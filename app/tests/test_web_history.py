# Tests for app/web/history.py
from __future__ import annotations

import json
import os
import tempfile

from app.web.history import get_run, list_runs


def _write_run(directory: str, filename: str, data: dict) -> str:
    path = os.path.join(directory, filename)
    with open(path, "w") as fh:
        json.dump(data, fh)
    return path


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------

def test_list_runs_returns_empty_for_missing_dir():
    assert list_runs("/nonexistent/path") == []


def test_list_runs_returns_empty_for_empty_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert list_runs(tmpdir) == []


def test_list_runs_returns_runs_newest_first():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {"started_at": "2025-01-01T03:00:00", "updated": 1})
        _write_run(tmpdir, "run_20250102_030000.json", {"started_at": "2025-01-02T03:00:00", "updated": 2})

        runs = list_runs(tmpdir)

    assert len(runs) == 2
    assert runs[0]["started_at"] == "2025-01-02T03:00:00"
    assert runs[1]["started_at"] == "2025-01-01T03:00:00"


def test_list_runs_injects_filename():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {"updated": 1})
        runs = list_runs(tmpdir)

    assert runs[0]["filename"] == "run_20250101_030000.json"


def test_list_runs_strips_files_list():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {
            "updated": 1,
            "files": [{"path": "/a.mp4", "status": "updated"}],
        })
        runs = list_runs(tmpdir)

    assert "files" not in runs[0]


def test_list_runs_skips_non_json_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        open(os.path.join(tmpdir, "run_latest.txt"), "w").close()
        open(os.path.join(tmpdir, "other.json"), "w").close()
        _write_run(tmpdir, "run_20250101_030000.json", {"updated": 0})

        runs = list_runs(tmpdir)

    assert len(runs) == 1


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------

def test_get_run_returns_none_for_missing_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        assert get_run(tmpdir, "run_99999999_999999.json") is None


def test_get_run_returns_full_data_including_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {
            "updated": 1,
            "files": [{"path": "/a.mp4", "status": "updated"}],
        })
        run = get_run(tmpdir, "run_20250101_030000.json")

    assert run is not None
    assert run["updated"] == 1
    assert len(run["files"]) == 1


def test_get_run_rejects_path_traversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = get_run(tmpdir, "../etc/passwd")
    assert result is None


def test_get_run_rejects_non_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = get_run(tmpdir, "run_latest.txt")
    assert result is None
