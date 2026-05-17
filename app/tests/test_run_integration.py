# Integration tests for app/main.run() — the core orchestration function.
# All external I/O (Plex, HTTP, file writes) is mocked so tests are fast and
# deterministic. The goal is to verify the routing/reporting logic, not the
# individual writers (those have their own unit tests).
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from app.main import run
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.router import Router
from app.scanner import MediaFile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(tmp_path, library_path=None):
    cfg = MagicMock()
    cfg.library_paths = [str(library_path or tmp_path)]
    cfg.force = False
    cfg.report_path = str(tmp_path / "reports")
    cfg.report_retention_days = 90
    cfg.log_path = str(tmp_path / "logs")
    cfg.log_retention_days = 30
    cfg.plex_url = "http://localhost:32400"
    cfg.plex_token = "token"
    return cfg


def _make_media(tmp_path, name="Jane Doe % mysite - 12345"):
    path = str(tmp_path / f"{name}.mp4")
    open(path, "w").close()
    return MediaFile(path=path, stem=name, nfo_path=str(tmp_path / f"{name}.nfo"))


class _GoodPlugin(MetadataPlugin):
    site_id = "mysite"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        return MetadataResult(title="Good Title", source_id="42")


class _NonePlugin(MetadataPlugin):
    site_id = "nonesite"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        return None


class _ErrorPlugin(MetadataPlugin):
    site_id = "errorsite"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        raise RuntimeError("API down")


# ---------------------------------------------------------------------------
# run() — core routing outcomes
# ---------------------------------------------------------------------------

def test_run_marks_file_updated_on_success(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=True), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.updated == 1
    assert report.errors == 0


def test_run_marks_file_unmatched_when_parse_fails(tmp_path):
    media = _make_media(tmp_path, "no percent sign here")
    router = Router({})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.unmatched == 1
    assert report.updated == 0


def test_run_marks_file_add_form(tmp_path):
    media = _make_media(tmp_path, "Add Jane Doe At MyStudio")
    router = Router({})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.add_form == 1


def test_run_marks_file_unmatched_when_no_plugin(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % unknownsite - 99")
    router = Router({})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.unmatched == 1


def test_run_marks_file_unmatched_when_plugin_returns_none(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % nonesite - 99")
    router = Router({"nonesite": _NonePlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.unmatched == 1


def test_run_marks_file_error_when_plugin_raises(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % errorsite - 99")
    router = Router({"errorsite": _ErrorPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.errors == 1


def test_run_marks_file_error_when_nfo_write_fails(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo", side_effect=OSError("disk full")), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.errors == 1
    assert report.updated == 0


def test_run_dry_run_skips_nfo_write(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo") as mock_nfo, \
         patch("app.main.write_images") as mock_img, \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router, dry_run=True)

    mock_nfo.assert_not_called()
    mock_img.assert_not_called()
    report = mock_report.call_args.args[0]
    assert report.updated == 1  # still marked updated in dry run


def test_run_dry_run_skips_plex_push(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)
    mock_plex = MagicMock()

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=True), \
         patch("app.main.connect_plex", return_value=mock_plex), \
         patch("app.main.push_to_plex") as mock_push, \
         patch("app.main.write_report"):
        run(cfg, router, dry_run=True)

    mock_push.assert_not_called()


def test_run_continues_after_one_file_errors(tmp_path):
    good = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    bad = _make_media(tmp_path, "Jane Doe % errorsite - 99")
    router = Router({"mysite": _GoodPlugin(), "errorsite": _ErrorPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([bad, good], 0)), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=True), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.errors == 1
    assert report.updated == 1


def test_run_pushes_to_plex_when_connected(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)
    mock_plex = MagicMock()

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=True), \
         patch("app.main.connect_plex", return_value=mock_plex), \
         patch("app.main.push_to_plex") as mock_push, \
         patch("app.main.write_report"):
        run(cfg, router)

    mock_push.assert_called_once()
