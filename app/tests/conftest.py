# Shared pytest fixtures for the m3 test suite.
#
# Import these in test files via normal pytest fixture injection — no explicit
# import needed. pytest discovers this file automatically.
#
# Usage in a test:
#
#   def test_something(client):
#       r = client.get("/healthz")
#       assert r.status_code == 200
#
#   def test_with_run(client, write_run, config):
#       write_run("run_20250101_030000.json", {"updated": 5, ...})
#       r = client.get("/runs")
#       ...

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.web import create_app


@pytest.fixture()
def config(tmp_path):
    """A MagicMock Config backed by a real temporary directory for report/log paths."""
    cfg = MagicMock()
    cfg.report_path = str(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    cfg.log_path = str(log_dir)
    cfg.web_host = "127.0.0.1"
    cfg.web_port = 8765
    cfg.app_name = "m3"
    cfg.plex_url = "http://localhost:32400"
    cfg.plex_token = "fake-token"
    cfg.library_paths = [str(tmp_path / "media")]
    cfg.plugin_dir = str(tmp_path / "plugins")
    cfg.run_schedule = "0 3 * * *"
    cfg.log_level = "INFO"
    cfg.log_retention_days = 30
    cfg.report_retention_days = 90
    cfg.web_enabled = True
    cfg.plugin_rate_limit_secs = 0.0
    cfg.plugin_fetch_timeout_secs = 30.0
    cfg.notify_url = ""
    cfg.notify_min_errors = 0
    cfg.force = False
    cfg.library_exclude_patterns = []
    return cfg


@pytest.fixture()
def app(config):
    """FastAPI application instance wired with the shared config fixture."""
    scheduler = MagicMock()
    run_fn = MagicMock()
    return create_app(config, {}, scheduler, run_fn)


@pytest.fixture()
def client(app):
    """TestClient for the FastAPI app. Use for route-level tests."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def write_run(config):
    """Factory fixture: writes a run report JSON file into config.report_path.

    Usage:
        write_run("run_20250101_030000.json", {"updated": 3, "errors": 0, ...})
    """
    def _write(filename: str, data: dict) -> None:
        os.makedirs(config.report_path, exist_ok=True)
        with open(os.path.join(config.report_path, filename), "w") as fh:
            json.dump(data, fh)
    return _write
