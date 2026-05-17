# Tests for app/web routes using FastAPI TestClient.
from __future__ import annotations

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.web import create_app


def _make_config(report_path: str) -> MagicMock:
    cfg = MagicMock()
    cfg.report_path = report_path
    cfg.web_host = "127.0.0.1"
    cfg.web_port = 8765
    cfg.app_name = "pm"
    cfg.plex_url = "http://localhost:32400"
    cfg.plex_token = "fake-token"
    cfg.library_paths = ["/media"]
    cfg.plugin_dir = "/plugins"
    cfg.log_path = "/config/logs"
    cfg.run_schedule = "0 3 * * *"
    cfg.log_level = "INFO"
    cfg.log_retention_days = 30
    cfg.report_retention_days = 90
    cfg.web_enabled = True
    cfg.plugin_rate_limit_secs = 1.0
    return cfg


def _make_app(report_path: str, registry: dict | None = None):
    config = _make_config(report_path)
    scheduler = MagicMock()
    run_fn = MagicMock()
    return create_app(config, registry or {}, scheduler, run_fn)


def _write_run(directory: str, filename: str, data: dict) -> None:
    with open(os.path.join(directory, filename), "w") as fh:
        json.dump(data, fh)


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

def test_healthz_returns_200():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# / (latest run)
# ---------------------------------------------------------------------------

def test_dashboard_returns_200_with_no_runs():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/")
    assert r.status_code == 200
    assert "Run now" in r.text


def test_dashboard_shows_latest_run_stats():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "finished_at": "2025-05-15T03:00:30",
            "updated": 5, "skipped": 10, "errors": 1,
            "scrape_errors": 2, "unmatched": 0, "total_scanned": 18,
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/")
    assert "2025-05-15" in r.text
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /runs
# ---------------------------------------------------------------------------

def test_runs_returns_200():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs")
    assert r.status_code == 200


def test_runs_lists_run_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 3, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 3,
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs")
    assert "2025-05-15" in r.text


# ---------------------------------------------------------------------------
# /runs/{filename}
# ---------------------------------------------------------------------------

def test_run_detail_returns_200():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 1, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 1,
            "files": [{"path": "/media/scene.mp4", "status": "updated", "message": ""}],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs/run_20250515_030000.json")
    assert r.status_code == 200
    assert "/media/scene.mp4" in r.text


def test_run_detail_returns_404_for_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs/run_does_not_exist.json")
    assert r.status_code == 404
    # 404 renders an HTML error page, not a JSON body
    assert "Not Found" in r.text or "not found" in r.text.lower()


def test_run_detail_status_filter():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 1, "skipped": 0, "errors": 1,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 2,
            "files": [
                {"path": "/media/good.mp4", "status": "updated", "message": ""},
                {"path": "/media/bad.mp4", "status": "error", "message": "crash"},
            ],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs/run_20250515_030000.json?status=error")
    assert "bad.mp4" in r.text
    assert "good.mp4" not in r.text


# ---------------------------------------------------------------------------
# /plugins
# ---------------------------------------------------------------------------

def test_plugins_returns_200_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir, registry={})
        with TestClient(app) as client:
            r = client.get("/plugins")
    assert r.status_code == 200
    assert "No plugins loaded" in r.text


def test_plugins_lists_registered_plugins():
    from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

    class _TestPlugin(MetadataPlugin):
        site_id = "testsite"
        aliases = ["TS"]
        def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
            return None

    plugin = _TestPlugin()
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir, registry={"testsite": plugin, "ts": plugin})
        with TestClient(app) as client:
            r = client.get("/plugins")

    assert "testsite" in r.text
    assert "_TestPlugin" in r.text


# ---------------------------------------------------------------------------
# POST /trigger/run
# ---------------------------------------------------------------------------

def test_trigger_run_schedules_job():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        scheduler = MagicMock()
        run_fn = MagicMock()
        app = create_app(config, {}, scheduler, run_fn)

        with TestClient(app, follow_redirects=True) as client:
            r = client.post("/trigger/run")

    assert r.status_code == 200
    scheduler.add_job.assert_called_once()


# ---------------------------------------------------------------------------
# POST /trigger/file
# ---------------------------------------------------------------------------

def test_trigger_file_returns_400_without_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.post("/trigger/file", data={"file_path": ""})
    assert r.status_code == 400


def test_trigger_file_returns_404_for_nonexistent_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.post("/trigger/file", data={"file_path": "/nonexistent/file.mp4"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

def test_api_status_returns_idle_when_no_run_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/api/status")
    assert r.status_code == 200
    assert "Idle" in r.text


def test_api_status_returns_running_when_active():
    from app.runstate import RunState
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        scheduler = MagicMock()
        run_fn = MagicMock()
        run_state = RunState()
        run_state.start()
        from app.web import create_app
        app = create_app(config, {}, scheduler, run_fn, run_state)
        with TestClient(app) as client:
            r = client.get("/api/status")
        run_state.stop()
    assert r.status_code == 200
    assert "Running" in r.text


# ---------------------------------------------------------------------------
# GET /unmatched
# ---------------------------------------------------------------------------

def test_unmatched_returns_200_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/unmatched")
    assert r.status_code == 200
    assert "No unmatched" in r.text


def test_unmatched_lists_paths():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00", "updated": 0,
            "files": [{"path": "/media/Unknown_Site_001.mp4", "status": "unmatched", "message": ""}],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/unmatched")
    assert r.status_code == 200
    assert "Unknown_Site_001.mp4" in r.text


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------

def test_config_returns_200():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/config")
    assert r.status_code == 200


def test_config_shows_library_paths():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/config")
    assert "LIBRARY_PATHS" in r.text


def test_config_masks_plex_token():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/config")
    assert "fake-token" not in r.text
    assert "***" in r.text


# ---------------------------------------------------------------------------
# Inline retry button in run detail
# ---------------------------------------------------------------------------

def test_run_detail_shows_inline_retry_for_error_rows():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 0, "skipped": 0, "errors": 1,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 1,
            "files": [{"path": "/media/bad.mp4", "status": "error", "message": "timeout"}],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs/run_20250515_030000.json")
    assert "inline-form" in r.text


def test_run_detail_no_inline_retry_for_updated_rows():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 1, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 1,
            "files": [{"path": "/media/good.mp4", "status": "updated", "message": ""}],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs/run_20250515_030000.json")
    assert "inline-form" not in r.text


# ---------------------------------------------------------------------------
# GET /runs pagination
# ---------------------------------------------------------------------------

def test_runs_pagination_shows_page_controls():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write 30 run files — more than one page of 25
        for i in range(30):
            _write_run(tmpdir, f"run_202505{i:02d}_030000.json", {
                "started_at": f"2025-05-{i+1:02d}T03:00:00",
                "updated": i, "skipped": 0, "errors": 0,
                "scrape_errors": 0, "unmatched": 0, "total_scanned": i,
            })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs")
    assert r.status_code == 200
    assert "Page 1 of 2" in r.text or "Next" in r.text


def test_runs_pagination_page2():
    with tempfile.TemporaryDirectory() as tmpdir:
        for i in range(30):
            _write_run(tmpdir, f"run_202505{i:02d}_030000.json", {
                "started_at": f"2025-05-{i+1:02d}T03:00:00",
                "updated": 0, "skipped": 0, "errors": 0,
                "scrape_errors": 0, "unmatched": 0, "total_scanned": 0,
            })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs?page=2")
    assert r.status_code == 200
    assert "Page 2" in r.text


# ---------------------------------------------------------------------------
# GET /logs
# ---------------------------------------------------------------------------

def test_logs_returns_200_when_no_log_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/logs")
    assert r.status_code == 200
    assert "not found" in r.text.lower() or "Log" in r.text


def test_logs_shows_log_content():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        config.log_path = tmpdir
        log_file = os.path.join(tmpdir, "pm.log")
        with open(log_file, "w") as fh:
            fh.write("INFO hello from log\nINFO second line\n")
        scheduler = MagicMock()
        run_fn = MagicMock()
        app = create_app(config, {}, scheduler, run_fn)
        with TestClient(app) as client:
            r = client.get("/logs")
    assert r.status_code == 200
    assert "hello from log" in r.text


# ---------------------------------------------------------------------------
# GET /config — PLUGIN_RATE_LIMIT_SECS included
# ---------------------------------------------------------------------------

def test_config_shows_plugin_rate_limit():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/config")
    assert "PLUGIN_RATE_LIMIT_SECS" in r.text
