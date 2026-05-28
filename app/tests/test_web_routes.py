# Tests for app/web routes using FastAPI TestClient.
from __future__ import annotations

import glob
import json
import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.web import create_app

pytestmark = pytest.mark.integration


def _make_config(report_path: str) -> MagicMock:
    cfg = MagicMock()
    cfg.report_path = report_path
    cfg.web_host = "127.0.0.1"
    cfg.web_port = 8765
    cfg.app_name = "m3"
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
    cfg.notify_url = ""
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


def test_run_detail_status_filter_renamed():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 1, "renamed": 1, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 2,
            "files": [
                {"path": "/media/updated.mp4", "status": "updated", "message": ""},
                {"path": "/media/renamed.mp4", "status": "renamed",
                 "message": "renamed from 'old name'"},
            ],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs/run_20250515_030000.json?status=renamed")
    assert "renamed.mp4" in r.text
    assert "updated.mp4" not in r.text


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
        aliases = ("TS",)
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
# /healthz?check=plex
# ---------------------------------------------------------------------------

def test_healthz_plex_check_returns_200_on_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with patch("app.writers.plex.connect_plex", return_value=MagicMock()):
            with TestClient(app) as client:
                r = client.get("/healthz?check=plex")
    assert r.status_code == 200
    assert r.json()["plex"] == "ok"


def test_healthz_plex_check_returns_503_on_failure():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with patch("app.writers.plex.connect_plex", side_effect=Exception("refused")):
            with TestClient(app) as client:
                r = client.get("/healthz?check=plex")
    assert r.status_code == 503
    assert "plex" in r.json()


# ---------------------------------------------------------------------------
# /runs/{filename} — pagination
# ---------------------------------------------------------------------------

def test_run_detail_pagination_page_2():
    with tempfile.TemporaryDirectory() as tmpdir:
        files = [{"path": f"/media/f{i}.mp4", "status": "updated", "message": ""} for i in range(250)]
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 250, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 250,
            "files": files,
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r1 = client.get("/runs/run_20250515_030000.json?page=1")
            r2 = client.get("/runs/run_20250515_030000.json?page=2")

    assert r1.status_code == 200
    assert r2.status_code == 200
    # page 1 shows f0, page 2 shows f200
    assert "/media/f0.mp4" in r1.text
    assert "/media/f200.mp4" in r2.text
    assert "/media/f200.mp4" not in r1.text


# ---------------------------------------------------------------------------
# /files
# ---------------------------------------------------------------------------

def test_files_page_returns_200():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/files")
    assert r.status_code == 200


def test_files_page_shows_history_for_queried_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 1, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 1,
            "files": [{"path": "/media/movie.mp4", "status": "updated", "message": ""}],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/files?path=%2Fmedia%2Fmovie.mp4")
    assert r.status_code == 200
    assert "/media/movie.mp4" in r.text
    assert "updated" in r.text


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
        log_file = os.path.join(tmpdir, "m3.log")
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


def test_config_shows_notify_url():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/config")
    assert "NOTIFY_URL" in r.text


# ---------------------------------------------------------------------------
# POST /trigger/reload
# ---------------------------------------------------------------------------

def test_trigger_reload_redirects_to_plugins():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        import app.web.routes as routes_module
        with TestClient(app, follow_redirects=True) as client:
            orig_platform = routes_module.platform
            orig_os = routes_module.os
            try:
                routes_module.platform = MagicMock()
                routes_module.platform.system.return_value = "Linux"
                routes_module.os = MagicMock()
                r = client.post("/trigger/reload")
            finally:
                routes_module.platform = orig_platform
                routes_module.os = orig_os
    assert r.status_code == 200
    assert "Plugins" in r.text


def test_trigger_reload_noop_on_windows():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        import app.web.routes as routes_module
        with TestClient(app, follow_redirects=True) as client:
            orig_platform = routes_module.platform
            try:
                routes_module.platform = MagicMock()
                routes_module.platform.system.return_value = "Windows"
                r = client.post("/trigger/reload")
            finally:
                routes_module.platform = orig_platform
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# run_detail — unmatched rows have inline retry
# ---------------------------------------------------------------------------

def test_run_detail_shows_inline_retry_for_unmatched_rows():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 0, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 1, "total_scanned": 1,
            "files": [{"path": "/media/mystery.mp4", "status": "unmatched", "message": "no plugin"}],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/runs/run_20250515_030000.json")
    assert "inline-form" in r.text


# ---------------------------------------------------------------------------
# /trigger/run — already-running flash message (non-HTMX callers)
# ---------------------------------------------------------------------------

def test_trigger_run_redirects_with_flash_when_already_running():
    """Non-HTMX caller: 303 redirect with ?msg=already_running when a run is in progress."""
    from app.runstate import RunState
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        scheduler = MagicMock()
        run_fn = MagicMock()
        run_state = RunState()
        run_state.start()
        app = create_app(config, {}, scheduler, run_fn, run_state)
        with TestClient(app, follow_redirects=False) as client:
            r = client.post("/trigger/run")
        run_state.stop()
    assert r.status_code == 303
    assert "already_running" in r.headers.get("location", "")


def test_trigger_run_does_not_schedule_when_already_running():
    from app.runstate import RunState
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        scheduler = MagicMock()
        run_fn = MagicMock()
        run_state = RunState()
        run_state.start()
        app = create_app(config, {}, scheduler, run_fn, run_state)
        with TestClient(app) as client:
            client.post("/trigger/run")
        run_state.stop()
    scheduler.add_job.assert_not_called()


# ---------------------------------------------------------------------------
# /trigger/run — HTMX-aware responses
# ---------------------------------------------------------------------------

def test_trigger_run_htmx_returns_warning_fragment_when_already_running():
    """HTMX caller: 200 + warning HTML fragment (no redirect) when already running."""
    from app.runstate import RunState
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        scheduler = MagicMock()
        run_fn = MagicMock()
        run_state = RunState()
        run_state.start()
        app = create_app(config, {}, scheduler, run_fn, run_state)
        with TestClient(app, follow_redirects=False) as client:
            r = client.post("/trigger/run", headers={"HX-Request": "true"})
        run_state.stop()
    assert r.status_code == 200
    assert "Already running" in r.text or "already" in r.text.lower()
    scheduler.add_job.assert_not_called()


def test_trigger_run_htmx_returns_success_fragment_when_queued():
    """HTMX caller: 200 + success HTML fragment (no redirect) when run is queued."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        scheduler = MagicMock()
        run_fn = MagicMock()
        app = create_app(config, {}, scheduler, run_fn)
        with TestClient(app, follow_redirects=False) as client:
            r = client.post("/trigger/run", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "Queued" in r.text or "queued" in r.text.lower()
    scheduler.add_job.assert_called_once()


def test_trigger_run_non_htmx_redirects_after_queuing():
    """Non-HTMX caller: 303 redirect to / after successfully queuing a run."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        scheduler = MagicMock()
        run_fn = MagicMock()
        app = create_app(config, {}, scheduler, run_fn)
        with TestClient(app, follow_redirects=False) as client:
            r = client.post("/trigger/run")
    assert r.status_code == 303
    assert r.headers.get("location", "").startswith("/")


# ---------------------------------------------------------------------------
# /trigger/file — path scope validation
# ---------------------------------------------------------------------------

def test_trigger_file_rejects_path_outside_library():
    """Paths that don't resolve under a configured library path must return 400."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a real file, but configure the app with a *different* library path
        outside_dir = tempfile.mkdtemp()
        try:
            fake_file = os.path.join(outside_dir, "evil.mp4")
            open(fake_file, "w").close()

            config = _make_config(tmpdir)
            config.library_paths = [tmpdir]  # library is tmpdir, file is in outside_dir
            app = create_app(config, {}, MagicMock(), MagicMock())
            with TestClient(app) as client:
                r = client.post("/trigger/file", data={"file_path": fake_file})
        finally:
            import shutil
            shutil.rmtree(outside_dir, ignore_errors=True)
    assert r.status_code == 400


def test_trigger_file_accepts_path_inside_library():
    """A valid file inside the configured library path must not return 400/403."""
    with tempfile.TemporaryDirectory() as tmpdir:
        real_file = os.path.join(tmpdir, "valid.mp4")
        open(real_file, "w").close()

        config = _make_config(tmpdir)
        config.library_paths = [tmpdir]
        app = create_app(config, {}, MagicMock(), MagicMock())
        with TestClient(app, follow_redirects=True) as client:
            r = client.post("/trigger/file", data={"file_path": real_file})
    # Should not return 400 (may be 200 or 303 after the thread is started)
    assert r.status_code != 400


# ---------------------------------------------------------------------------
# /trigger/file — per-path coalescing (409 when already in flight)
# ---------------------------------------------------------------------------

def test_trigger_file_returns_409_when_already_processing():
    """Concurrent requests for the same path must receive 409 after the first."""
    import app.web.routes as routes_module

    with tempfile.TemporaryDirectory() as tmpdir:
        real_file = os.path.join(tmpdir, "dup.mp4")
        open(real_file, "w").close()

        config = _make_config(tmpdir)
        config.library_paths = [tmpdir]
        app = create_app(config, {}, MagicMock(), MagicMock())

        real_path = os.path.realpath(real_file)
        # Manually acquire the per-path lock to simulate a running process
        lock = routes_module._get_file_lock(real_path)
        lock.acquire()
        try:
            with TestClient(app) as client:
                r = client.post("/trigger/file", data={"file_path": real_file})
        finally:
            lock.release()
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# /trigger/file — HTMX error fragment (200 with inline error badge)
# ---------------------------------------------------------------------------

def test_trigger_file_htmx_returns_200_fragment_for_missing_file():
    """HTMX callers get a 200 HTML error fragment instead of a 404 HTTPException."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        config.library_paths = [tmpdir]
        app = create_app(config, {}, MagicMock(), MagicMock())
        with TestClient(app) as client:
            r = client.post(
                "/trigger/file",
                data={"file_path": os.path.join(tmpdir, "nonexistent.mp4")},
                headers={"HX-Request": "true"},
            )
    assert r.status_code == 200
    assert "✗" in r.text


def test_trigger_file_htmx_returns_200_fragment_for_out_of_library_path():
    """HTMX callers get a 200 error fragment when the path is outside all library dirs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        outside_dir = tempfile.mkdtemp()
        try:
            outside_file = os.path.join(outside_dir, "evil.mp4")
            open(outside_file, "w").close()
            config = _make_config(tmpdir)
            config.library_paths = [tmpdir]
            app = create_app(config, {}, MagicMock(), MagicMock())
            with TestClient(app) as client:
                r = client.post(
                    "/trigger/file",
                    data={"file_path": outside_file},
                    headers={"HX-Request": "true"},
                )
        finally:
            import shutil; shutil.rmtree(outside_dir, ignore_errors=True)
    assert r.status_code == 200
    assert "✗" in r.text


# ---------------------------------------------------------------------------
# T2 — trigger_file background thread writes trigger record
# ---------------------------------------------------------------------------

def _make_plugin_registry(plugin_class):
    """Build a minimal registry dict with a single plugin instance."""
    instance = plugin_class()
    key = plugin_class.site_id
    return {key: instance}


def test_trigger_file_thread_writes_trigger_record_on_success():
    """When _run_single succeeds, a trigger_*.json file must be created in
    config.report_path with status='updated'."""
    from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

    class _TriggerGoodPlugin(MetadataPlugin):
        site_id = "triggersite"

        def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
            return MetadataResult(title="Triggered", source_id="999")

    with tempfile.TemporaryDirectory() as tmpdir:
        real_file = os.path.join(tmpdir, "Jane Doe % triggersite - 999.mp4")
        open(real_file, "w").close()

        config = _make_config(tmpdir)
        config.library_paths = [tmpdir]
        config.report_path = tmpdir
        config.plugin_fetch_timeout_secs = 30.0

        registry = _make_plugin_registry(_TriggerGoodPlugin)
        from app.router import Router
        router_obj = Router(registry)

        app = create_app(config, registry, MagicMock(), MagicMock())
        # Inject the real router so dispatch works
        app.state.plugin_router = router_obj

        # Use a threading.Event to detect when the background thread finishes
        done = threading.Event()
        from app.utils import atomic_replace as _real_replace

        import app.web.routes as routes_module
        original_replace = routes_module.atomic_replace

        def _replace_and_signal(src, dst):
            original_replace(src, dst)
            if "trigger_" in dst:
                done.set()

        with patch("app.web.routes.write_nfo"), \
             patch("app.web.routes.write_images", return_value=True), \
             patch("app.web.routes.connect_plex", return_value=None), \
             patch("app.web.routes.push_to_plex", return_value=True), \
             patch("app.web.routes.atomic_replace", side_effect=_replace_and_signal):
            with TestClient(app) as client:
                r = client.post("/trigger/file", data={"file_path": real_file})

            # Wait up to 5 seconds for the background thread to finish
            done.wait(timeout=5)

        trigger_files = glob.glob(os.path.join(tmpdir, "trigger_*.json"))
        assert trigger_files, "A trigger_*.json file must be written by the background thread"

        with open(trigger_files[0], encoding="utf-8") as fh:
            record = json.load(fh)

        assert record["files"][0]["status"] == "updated"


def test_trigger_file_thread_writes_error_status_when_plugin_raises():
    """When the plugin raises inside _run_single, the trigger record must
    have status='error'."""
    from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

    class _TriggerBadPlugin(MetadataPlugin):
        site_id = "triggersite2"

        def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
            raise RuntimeError("plugin exploded")

    with tempfile.TemporaryDirectory() as tmpdir:
        real_file = os.path.join(tmpdir, "Jane Doe % triggersite2 - 999.mp4")
        open(real_file, "w").close()

        config = _make_config(tmpdir)
        config.library_paths = [tmpdir]
        config.report_path = tmpdir
        config.plugin_fetch_timeout_secs = 30.0

        registry = _make_plugin_registry(_TriggerBadPlugin)
        from app.router import Router
        router_obj = Router(registry)

        app = create_app(config, registry, MagicMock(), MagicMock())
        app.state.plugin_router = router_obj

        done = threading.Event()

        import app.web.routes as routes_module
        original_replace = routes_module.atomic_replace

        def _replace_and_signal(src, dst):
            original_replace(src, dst)
            if "trigger_" in dst:
                done.set()

        with patch("app.web.routes.connect_plex", return_value=None), \
             patch("app.web.routes.atomic_replace", side_effect=_replace_and_signal):
            with TestClient(app) as client:
                r = client.post("/trigger/file", data={"file_path": real_file})

            done.wait(timeout=5)

        trigger_files = glob.glob(os.path.join(tmpdir, "trigger_*.json"))
        assert trigger_files, "A trigger_*.json file must be written even on plugin error"

        with open(trigger_files[0], encoding="utf-8") as fh:
            record = json.load(fh)

        assert record["files"][0]["status"] == "error"


# ---------------------------------------------------------------------------
# /healthz — version field
# ---------------------------------------------------------------------------

def test_healthz_returns_version():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/healthz")
    data = r.json()
    assert "version" in data
    assert data["version"]  # must be non-empty


# ---------------------------------------------------------------------------
# GET /logs — ?tail=N param
# ---------------------------------------------------------------------------

def test_logs_tail_param_respected():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        config.log_path = tmpdir
        log_file = os.path.join(tmpdir, "m3.log")
        with open(log_file, "w") as fh:
            for i in range(300):
                fh.write(f"INFO line {i}\n")
        scheduler = MagicMock()
        run_fn = MagicMock()
        app = create_app(config, {}, scheduler, run_fn)
        with TestClient(app) as client:
            r_default = client.get("/logs")
            r_500 = client.get("/logs?tail=500")
    assert r_default.status_code == 200
    # default 200 lines: line 100 (0-indexed) is the 101st — not shown; line 300 would be last
    assert "line 299" in r_default.text
    assert "line 0" not in r_default.text   # first line scrolled off in default 200
    assert r_500.status_code == 200
    assert "line 0" in r_500.text           # 500 lines covers all 300


def test_logs_tail_param_capped_at_2000():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        config.log_path = tmpdir
        log_file = os.path.join(tmpdir, "m3.log")
        with open(log_file, "w") as fh:
            fh.write("INFO test\n")
        scheduler = MagicMock()
        run_fn = MagicMock()
        app = create_app(config, {}, scheduler, run_fn)
        with TestClient(app) as client:
            r = client.get("/logs?tail=99999")
    assert r.status_code == 200
    assert "2000" in r.text   # capped value shown in the UI


# ---------------------------------------------------------------------------
# GET /config — library path existence badge
# ---------------------------------------------------------------------------

def test_config_shows_exists_badge_for_present_library_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        config.library_paths = [tmpdir]   # tmpdir definitely exists
        app = create_app(config, {}, MagicMock(), MagicMock())
        with TestClient(app) as client:
            r = client.get("/config")
    assert r.status_code == 200
    assert "exists" in r.text


def test_config_shows_missing_badge_for_absent_library_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = _make_config(tmpdir)
        config.library_paths = ["/nonexistent/totally/fake/path"]
        app = create_app(config, {}, MagicMock(), MagicMock())
        with TestClient(app) as client:
            r = client.get("/config")
    assert r.status_code == 200
    assert "missing" in r.text


# ---------------------------------------------------------------------------
# T12 — GET /healthz?verbose=1
# ---------------------------------------------------------------------------

def test_healthz_verbose_with_run_history():
    """?verbose=1 must return status, version, last_run, hours_since_last_run,
    run_count; run_count >= 1 and last_run is not None when a report exists."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 1, "skipped": 0, "errors": 0,
            "scrape_errors": 0, "unmatched": 0, "total_scanned": 1,
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/healthz?verbose=1")

    assert r.status_code == 200
    data = r.json()
    for key in ("status", "version", "last_run", "hours_since_last_run", "run_count"):
        assert key in data, f"Expected key {key!r} in verbose healthz response"
    assert data["run_count"] >= 1
    assert data["last_run"] is not None


def test_healthz_verbose_empty_history():
    """?verbose=1 with no reports must return last_run=None and
    hours_since_last_run=None."""
    with tempfile.TemporaryDirectory() as tmpdir:
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/healthz?verbose=1")

    assert r.status_code == 200
    data = r.json()
    assert data["last_run"] is None
    assert data["hours_since_last_run"] is None


# ---------------------------------------------------------------------------
# T13 — /unmatched?q= filter
# ---------------------------------------------------------------------------

def test_unmatched_q_filter_shows_only_matching_entry():
    """?q=alpha must show the alpha path and exclude the beta path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_run(tmpdir, "run_20250515_030000.json", {
            "started_at": "2025-05-15T03:00:00",
            "updated": 0,
            "files": [
                {"path": "/media/alpha.mp4", "status": "unmatched", "message": "no plugin"},
                {"path": "/media/beta.mp4", "status": "unmatched", "message": "no plugin"},
            ],
        })
        app = _make_app(tmpdir)
        with TestClient(app) as client:
            r = client.get("/unmatched?q=alpha")

    assert r.status_code == 200
    assert "alpha.mp4" in r.text
    assert "beta.mp4" not in r.text


# ---------------------------------------------------------------------------
# T14 — _mask_url_creds
# ---------------------------------------------------------------------------

def test_mask_url_creds_masks_password():
    from app.web.routes import _mask_url_creds
    result = _mask_url_creds("http://user:s3cr3t@host:8080/path")
    assert "s3cr3t" not in result
    assert "host:8080" in result


def test_mask_url_creds_no_credentials_unchanged():
    from app.web.routes import _mask_url_creds
    url = "https://host/webhook"
    result = _mask_url_creds(url)
    assert result == url


def test_mask_url_creds_empty_string():
    from app.web.routes import _mask_url_creds
    result = _mask_url_creds("")
    assert result == ""
