# Tests for webhook notification payload shape and edge cases.
# httpx is imported inside _fire_webhook's try block, so we patch "httpx.Client"
# at the module level rather than "app.main.httpx".
from __future__ import annotations
import pytest

import json
from unittest.mock import MagicMock, patch

from app.main import _fire_webhook

pytestmark = pytest.mark.integration


def _run_report(**kwargs):
    report = MagicMock()
    report.updated = kwargs.get("updated", 1)
    report.skipped = kwargs.get("skipped", 0)
    report.errors = kwargs.get("errors", 0)
    report.scrape_errors = kwargs.get("scrape_errors", 0)
    report.unmatched = kwargs.get("unmatched", 0)
    report.total_scanned = kwargs.get("total_scanned", 1)
    report.duration_seconds = kwargs.get("duration_seconds", 5)
    report.started_at = kwargs.get("started_at", "2025-05-01T03:00:00")
    report.finished_at = kwargs.get("finished_at", "2025-05-01T03:00:05")
    report.filename = kwargs.get("filename", "run_20250501_030000.json")

    files = []
    if kwargs.get("errors", 0):
        f = MagicMock()
        f.status = "error"
        f.message = kwargs.get("first_error_msg", "some error")
        files.append(f)
    if kwargs.get("scrape_errors", 0):
        f = MagicMock()
        f.status = "scrape_error"
        f.message = kwargs.get("first_scrape_error_msg", "scrape fail")
        files.append(f)
    report.files = files
    return report


def _make_mock_client(captured: dict):
    """Return a context-manager-compatible mock httpx.Client."""
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)

    def fake_post(url, content, **kw):
        captured["url"] = url
        captured["body"] = content
        return mock_response

    mock_client.post.side_effect = fake_post
    return mock_client


def test_webhook_not_called_in_run_when_notify_url_empty(tmp_path):
    """run() must not call _fire_webhook when notify_url is empty."""
    from unittest.mock import patch as _patch
    from app.main import run
    from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
    from app.router import Router
    from app.scanner import MediaFile

    class _P(MetadataPlugin):
        site_id = "wh"
        aliases = ()
        def fetch(self, p): return MetadataResult(title="T")

    path = str(tmp_path / "Jane Doe % wh - 1.mp4")
    open(path, "w").close()
    media = MediaFile(path=path, stem="Jane Doe % wh - 1", nfo_path=str(tmp_path / "Jane Doe % wh - 1.nfo"))

    cfg = MagicMock()
    cfg.library_paths = [str(tmp_path)]
    cfg.force = False
    cfg.report_path = str(tmp_path / "r")
    cfg.report_retention_days = 90
    cfg.log_path = str(tmp_path / "l")
    cfg.log_retention_days = 30
    cfg.plex_url = "http://x"
    cfg.plex_token = "t"
    cfg.plugin_rate_limit_secs = 0.0
    cfg.plugin_fetch_timeout_secs = 30.0
    cfg.notify_url = ""

    with _patch("app.main.scan_library", return_value=([media], 0)), \
         _patch("app.main.write_nfo"), \
         _patch("app.main.write_images", return_value=(True, None, None)), \
         _patch("app.main.connect_plex", return_value=None), \
         _patch("app.main.write_report"), \
         _patch("app.main._fire_webhook") as mock_webhook:
        run(cfg, Router({"wh": _P()}))

    mock_webhook.assert_not_called()


def test_webhook_payload_contains_required_fields():
    report = _run_report(updated=3, errors=1, first_error_msg="oops")
    captured = {}
    mock_client = _make_mock_client(captured)

    with patch("httpx.Client", return_value=mock_client):
        _fire_webhook("http://localhost/hook", report, app_name="m3")

    body = json.loads(captured["body"])
    assert body["updated"] == 3
    assert body["errors"] == 1
    assert body["app_name"] == "m3"
    assert body["first_error"] == "oops"


def test_webhook_payload_is_valid_json_with_required_fields():
    report = _run_report()
    captured = {}
    mock_client = _make_mock_client(captured)

    with patch("httpx.Client", return_value=mock_client):
        _fire_webhook("http://localhost/hook", report, app_name="m3")

    body = json.loads(captured["body"])
    assert "app_name" in body
    assert "started_at" in body
    assert "finished_at" in body
    assert "total_scanned" in body


def test_webhook_network_failure_does_not_raise():
    """A network error must only log a warning — never raise to the caller."""
    report = _run_report()
    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = Exception("network down")

    with patch("httpx.Client", return_value=mock_client):
        _fire_webhook("http://localhost/hook", report, app_name="m3")


def test_webhook_payload_omits_first_error_when_no_errors():
    report = _run_report(errors=0, scrape_errors=0)
    captured = {}
    mock_client = _make_mock_client(captured)

    with patch("httpx.Client", return_value=mock_client):
        _fire_webhook("http://localhost/hook", report, app_name="m3")

    body = json.loads(captured["body"])
    assert body["first_error"] is None
    assert body["first_scrape_error"] is None


def test_notify_min_errors_suppresses_webhook_on_clean_run():
    """When notify_min_errors=1 and there are no errors, webhook must not fire."""
    import tempfile, os
    from unittest.mock import patch as _patch, MagicMock
    from app.main import run
    from app.plugins.base import MetadataPlugin, MetadataResult
    from app.router import Router
    from app.scanner import MediaFile

    class _P(MetadataPlugin):
        site_id = "ne"
        aliases = ()
        def fetch(self, p): return MetadataResult(title="T")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Jane Doe % ne - 1.mp4")
        open(path, "w").close()
        media = MediaFile(path=path, stem="Jane Doe % ne - 1",
                          nfo_path=os.path.join(tmp, "Jane Doe % ne - 1.nfo"))
        cfg = MagicMock()
        cfg.library_paths = [tmp]
        cfg.force = False
        cfg.report_path = os.path.join(tmp, "r")
        cfg.report_retention_days = 90
        cfg.log_path = os.path.join(tmp, "l")
        cfg.log_retention_days = 30
        cfg.plex_url = "http://x"
        cfg.plex_token = "t"
        cfg.plugin_rate_limit_secs = 0.0
        cfg.plugin_fetch_timeout_secs = 30.0
        cfg.notify_url = "http://localhost/hook"
        cfg.notify_min_errors = 1  # only fire when errors >= 1

        with _patch("app.main.scan_library", return_value=([media], 0)), \
             _patch("app.main.write_nfo"), \
             _patch("app.main.write_images", return_value=(True, None, None)), \
             _patch("app.main.connect_plex", return_value=None), \
             _patch("app.main.write_report"), \
             _patch("app.main._fire_webhook") as mock_webhook:
            run(cfg, Router({"ne": _P()}))

    mock_webhook.assert_not_called()


def test_notify_min_errors_fires_webhook_when_errors_meet_threshold():
    """When notify_min_errors=1 and errors >= 1, webhook must fire."""
    import tempfile, os
    from unittest.mock import patch as _patch, MagicMock
    from app.main import run
    from app.plugins.base import MetadataPlugin
    from app.router import Router
    from app.scanner import MediaFile

    class _FailPlugin(MetadataPlugin):
        site_id = "nef"
        aliases = ()
        def fetch(self, p): raise RuntimeError("bang")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Jane Doe % nef - 1.mp4")
        open(path, "w").close()
        media = MediaFile(path=path, stem="Jane Doe % nef - 1",
                          nfo_path=os.path.join(tmp, "Jane Doe % nef - 1.nfo"))
        cfg = MagicMock()
        cfg.library_paths = [tmp]
        cfg.force = False
        cfg.report_path = os.path.join(tmp, "r")
        cfg.report_retention_days = 90
        cfg.log_path = os.path.join(tmp, "l")
        cfg.log_retention_days = 30
        cfg.plex_url = "http://x"
        cfg.plex_token = "t"
        cfg.plugin_rate_limit_secs = 0.0
        cfg.plugin_fetch_timeout_secs = 30.0
        cfg.notify_url = "http://localhost/hook"
        cfg.notify_min_errors = 1

        with _patch("app.main.scan_library", return_value=([media], 0)), \
             _patch("app.main.connect_plex", return_value=None), \
             _patch("app.main.write_report"), \
             _patch("app.main._fire_webhook") as mock_webhook:
            run(cfg, Router({"nef": _FailPlugin()}))

    mock_webhook.assert_called_once()
