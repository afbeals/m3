# Tests for app/web/history.py
from __future__ import annotations
import pytest

import json
import os
import tempfile

from app.web.history import aggregate_unmatched, get_run, list_runs

pytestmark = pytest.mark.unit


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


# ---------------------------------------------------------------------------
# aggregate_unmatched
# ---------------------------------------------------------------------------

def test_aggregate_unmatched_empty_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = aggregate_unmatched(tmpdir)
    assert result == []


def test_aggregate_unmatched_counts_across_runs():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {
            "started_at": "2025-01-01T03:00:00", "updated": 0,
            "files": [{"path": "/media/X.mp4", "status": "unmatched", "message": "no plugin"}],
        })
        _write_run(tmpdir, "run_20250102_030000.json", {
            "started_at": "2025-01-02T03:00:00", "updated": 0,
            "files": [
                {"path": "/media/X.mp4", "status": "unmatched", "message": "no plugin"},
                {"path": "/media/Y.mp4", "status": "unmatched", "message": "no plugin"},
            ],
        })
        result = aggregate_unmatched(tmpdir)

    paths = {e["path"]: e for e in result}
    assert paths["/media/X.mp4"]["count"] == 2
    assert paths["/media/Y.mp4"]["count"] == 1


def test_aggregate_unmatched_last_seen_is_newest():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {
            "started_at": "2025-01-01T03:00:00", "updated": 0,
            "files": [{"path": "/media/X.mp4", "status": "unmatched", "message": ""}],
        })
        _write_run(tmpdir, "run_20250102_030000.json", {
            "started_at": "2025-01-02T03:00:00", "updated": 0,
            "files": [{"path": "/media/X.mp4", "status": "unmatched", "message": ""}],
        })
        result = aggregate_unmatched(tmpdir)

    assert result[0]["last_seen"] == "2025-01-02T03:00:00"


def test_aggregate_unmatched_sorted_by_count_desc():
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(1, 4):
            _write_run(tmpdir, f"run_2025010{i}_030000.json", {
                "started_at": f"2025-01-0{i}T03:00:00", "updated": 0,
                "files": [
                    {"path": "/media/frequent.mp4", "status": "unmatched", "message": ""},
                ],
            })
        _write_run(tmpdir, "run_20250104_030000.json", {
            "started_at": "2025-01-04T03:00:00", "updated": 0,
            "files": [{"path": "/media/rare.mp4", "status": "unmatched", "message": ""}],
        })
        result = aggregate_unmatched(tmpdir)

    assert result[0]["path"] == "/media/frequent.mp4"
    assert result[0]["count"] == 3


def test_aggregate_unmatched_ignores_non_unmatched_statuses():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {
            "started_at": "2025-01-01T03:00:00", "updated": 1,
            "files": [
                {"path": "/media/good.mp4", "status": "updated", "message": ""},
                {"path": "/media/bad.mp4", "status": "unmatched", "message": ""},
            ],
        })
        result = aggregate_unmatched(tmpdir)

    assert len(result) == 1
    assert result[0]["path"] == "/media/bad.mp4"


# ---------------------------------------------------------------------------
# get_file_history
# ---------------------------------------------------------------------------

def test_get_file_history_returns_empty_for_missing_dir():
    from app.web.history import get_file_history
    result = get_file_history("/nonexistent/path", "/media/movie.mp4")
    assert result == []


def test_get_file_history_returns_matching_entries():
    from app.web.history import get_file_history
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {
            "started_at": "2025-01-01T03:00:00", "updated": 1,
            "files": [
                {"path": "/media/movie.mp4", "status": "updated", "message": ""},
                {"path": "/media/other.mp4", "status": "skipped", "message": ""},
            ],
        })
        _write_run(tmpdir, "run_20250102_030000.json", {
            "started_at": "2025-01-02T03:00:00", "updated": 0,
            "files": [{"path": "/media/movie.mp4", "status": "error", "message": "crash"}],
        })
        result = get_file_history(tmpdir, "/media/movie.mp4")

    assert len(result) == 2
    # Newest run first
    assert result[0]["started_at"] == "2025-01-02T03:00:00"
    assert result[0]["status"] == "error"
    assert result[1]["started_at"] == "2025-01-01T03:00:00"
    assert result[1]["status"] == "updated"


def test_get_file_history_ignores_runs_without_path():
    from app.web.history import get_file_history
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250101_030000.json", {
            "started_at": "2025-01-01T03:00:00", "updated": 0,
            "files": [{"path": "/media/other.mp4", "status": "skipped", "message": ""}],
        })
        result = get_file_history(tmpdir, "/media/movie.mp4")

    assert result == []


def test_aggregate_unmatched_cache_invalidated_on_deletion():
    """Deleting a report file must invalidate the aggregate_unmatched cache so
    the deleted file's entries no longer appear in subsequent calls."""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_file = os.path.join(tmpdir, "run_20250101_030000.json")
        _write_run(tmpdir, "run_20250101_030000.json", {
            "started_at": "2025-01-01T03:00:00", "updated": 0,
            "files": [{"path": "/media/gone.mp4", "status": "unmatched", "message": ""}],
        })

        # Warm the cache
        result_before = aggregate_unmatched(tmpdir)
        assert any(e["path"] == "/media/gone.mp4" for e in result_before)

        # Delete the report file — the cache key includes file_count so this
        # must produce a cache miss on the next call
        os.remove(run_file)

        result_after = aggregate_unmatched(tmpdir)
        assert not any(e["path"] == "/media/gone.mp4" for e in result_after), (
            "Cache was not invalidated after report file was deleted"
        )


# ---------------------------------------------------------------------------
# T10 — get_run with corrupt JSON
# ---------------------------------------------------------------------------

def test_get_run_returns_none_for_corrupt_json():
    """get_run must return None (not raise) when the JSON file is truncated
    or otherwise malformed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        corrupt_path = os.path.join(tmpdir, "run_20250101_030000.json")
        with open(corrupt_path, "w") as fh:
            fh.write("{")  # truncated JSON

        result = get_run(tmpdir, "run_20250101_030000.json")

    assert result is None, (
        "get_run must return None for a corrupt/truncated JSON file, not raise"
    )
