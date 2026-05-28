# Tests for the per-plugin fetch timeout watchdog.
# A plugin whose fetch() sleeps longer than the timeout should be recorded as
# status=scrape_error and the run should continue processing remaining files.
from __future__ import annotations
import pytest

import threading
from unittest.mock import MagicMock, patch

from app.main import run
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.router import Router
from app.scanner import MediaFile

pytestmark = pytest.mark.integration

_never_set = threading.Event()


class _SlowPlugin(MetadataPlugin):
    site_id = "slowsite"
    aliases = ()

    def fetch(self, parsed: ParsedFilename):
        _never_set.wait()  # blocks indefinitely until process exits; no real sleep
        return MetadataResult(title="Should not reach here")


class _GoodPlugin(MetadataPlugin):
    site_id = "goodsite"
    aliases = ()

    def fetch(self, parsed: ParsedFilename):
        return MetadataResult(title="Fast Title")


def _config(tmp_path, timeout=0.2):
    cfg = MagicMock()
    cfg.library_paths = [str(tmp_path)]
    cfg.force = False
    cfg.dry_run = False
    cfg.report_path = str(tmp_path / "reports")
    cfg.report_retention_days = 90
    cfg.log_path = str(tmp_path / "logs")
    cfg.log_retention_days = 30
    cfg.plex_url = "http://localhost:32400"
    cfg.plex_token = "token"
    cfg.plugin_rate_limit_secs = 0.0
    cfg.plugin_fetch_timeout_secs = timeout
    cfg.notify_url = ""
    return cfg


def _media(tmp_path, name):
    path = str(tmp_path / f"{name}.mp4")
    open(path, "w").close()
    return MediaFile(path=path, stem=name, nfo_path=str(tmp_path / f"{name}.nfo"))


def test_plugin_timeout_marks_file_as_error(tmp_path):
    media = _media(tmp_path, "Jane Doe % slowsite - 1")
    router = Router({"slowsite": _SlowPlugin()})
    cfg = _config(tmp_path, timeout=0.2)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.scrape_errors == 1
    assert report.errors == 0
    assert report.updated == 0
    timed_out = [f for f in report.files if "timed out" in (f.message or "").lower()]
    assert len(timed_out) == 1


def test_run_continues_after_timeout(tmp_path):
    slow = _media(tmp_path, "Jane Doe % slowsite - 1")
    fast = _media(tmp_path, "Jane Doe % goodsite - 2")
    router = Router({"slowsite": _SlowPlugin(), "goodsite": _GoodPlugin()})
    cfg = _config(tmp_path, timeout=0.2)

    with patch("app.main.scan_library", return_value=([slow, fast], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=True), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.scrape_errors == 1
    assert report.errors == 0
    assert report.updated == 1
