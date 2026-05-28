# Tests for dry-run mode: connect_plex must NOT be called, and "Would push" lines
# must appear in the log even when plex_server is None.
from __future__ import annotations
import pytest

import logging
from unittest.mock import MagicMock, patch

from app.main import run
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.router import Router
from app.scanner import MediaFile

pytestmark = pytest.mark.integration


def _good_result():
    return MetadataResult(title="Test Title", actors=["Jane Doe"])


class _GoodPlugin(MetadataPlugin):
    site_id = "drysite"
    aliases = ()

    def fetch(self, parsed: ParsedFilename):
        return _good_result()


def _config(tmp_path):
    cfg = MagicMock()
    cfg.library_paths = [str(tmp_path)]
    cfg.force = False
    cfg.report_path = str(tmp_path / "reports")
    cfg.report_retention_days = 90
    cfg.log_path = str(tmp_path / "logs")
    cfg.log_retention_days = 30
    cfg.plex_url = "http://localhost:32400"
    cfg.plex_token = "token"
    cfg.plugin_rate_limit_secs = 0.0
    cfg.plugin_fetch_timeout_secs = 30.0
    cfg.notify_url = ""
    return cfg


def _make_media(tmp_path, name="Jane Doe % drysite - 12345"):
    path = str(tmp_path / f"{name}.mp4")
    open(path, "w").close()
    stem = name
    nfo = str(tmp_path / f"{name}.nfo")
    return MediaFile(path=path, stem=stem, nfo_path=nfo)


def test_dry_run_does_not_call_connect_plex(tmp_path):
    media = _make_media(tmp_path)
    router = Router({"drysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo") as mock_nfo, \
         patch("app.main.write_images") as mock_img, \
         patch("app.main.connect_plex") as mock_connect, \
         patch("app.main.write_report"):
        run(cfg, router, dry_run=True)

    mock_connect.assert_not_called()
    mock_nfo.assert_not_called()
    mock_img.assert_not_called()


def test_dry_run_logs_would_push_line(tmp_path, caplog):
    media = _make_media(tmp_path)
    router = Router({"drysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report"), \
         caplog.at_level(logging.INFO, logger="app.main"):
        run(cfg, router, dry_run=True)

    would_push_lines = [r for r in caplog.records if "Would push to Plex" in r.message]
    assert len(would_push_lines) >= 1, "Expected at least one '[DRY RUN] Would push to Plex' log entry"
