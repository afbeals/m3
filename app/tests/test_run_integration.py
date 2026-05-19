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
from app.scrape import ScrapeError, SelectorMissingError


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
    cfg.plugin_rate_limit_secs = 0.0  # no delay in tests
    cfg.plugin_fetch_timeout_secs = 30.0
    cfg.notify_url = ""  # disable webhook in tests
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


class _ScrapeErrorPlugin(MetadataPlugin):
    site_id = "scrapesite"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        raise SelectorMissingError("title not found at h1.scene-title")


class _ScrapeGenericPlugin(MetadataPlugin):
    site_id = "scrapegeneric"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        raise ScrapeError("HTTP 503 after 3 attempts")


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
    # dry_run skips write_report entirely (no report files are created)
    mock_report.assert_not_called()


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


def test_run_marks_scrape_error_when_selector_missing(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % scrapesite - 12345")
    router = Router({"scrapesite": _ScrapeErrorPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.scrape_errors == 1
    assert report.errors == 0


def test_run_scrape_error_message_included_in_result(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % scrapegeneric - 12345")
    router = Router({"scrapegeneric": _ScrapeGenericPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.scrape_errors == 1
    file_result = next(f for f in report.files if f.status == "scrape_error")
    assert "HTTP 503" in file_result.message


# ---------------------------------------------------------------------------
# Rename workflow in run()
# ---------------------------------------------------------------------------

def test_run_handles_renamed_file(tmp_path):
    """A MediaFile with renamed_from set should call rename_nfo_assets and
    record status='renamed' without calling any plugin."""
    old_stem = "Old Title % mysite - 12345"
    new_stem = "New Title % mysite - 12345"
    media = MediaFile(
        path=str(tmp_path / f"{new_stem}.mp4"),
        stem=new_stem,
        nfo_path=str(tmp_path / f"{new_stem}.nfo"),
        renamed_from=old_stem,
    )
    open(media.path, "w").close()
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.rename_nfo_assets") as mock_rename, \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    mock_rename.assert_called_once_with(str(tmp_path), old_stem, new_stem)
    report = mock_report.call_args.args[0]
    assert report.renamed == 1
    assert report.updated == 0


def test_run_renamed_dry_run_skips_rename(tmp_path):
    """In dry-run mode, rename_nfo_assets must NOT be called."""
    media = MediaFile(
        path=str(tmp_path / "new.mp4"),
        stem="new",
        nfo_path=str(tmp_path / "new.nfo"),
        renamed_from="old",
    )
    open(media.path, "w").close()
    router = Router({})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.rename_nfo_assets") as mock_rename, \
         patch("app.main.write_report"):
        run(cfg, router, dry_run=True)

    mock_rename.assert_not_called()


def test_run_renamed_file_rename_failure_records_error(tmp_path):
    """If rename_nfo_assets raises, the file is recorded as status='error'."""
    media = MediaFile(
        path=str(tmp_path / "new.mp4"),
        stem="new",
        nfo_path=str(tmp_path / "new.nfo"),
        renamed_from="old",
    )
    open(media.path, "w").close()
    router = Router({})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.rename_nfo_assets", side_effect=OSError("disk full")), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.errors == 1
    assert report.renamed == 0


def test_run_media_files_override_bypasses_scan(tmp_path):
    """When media_files_override is provided, scan_library must not be called."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library") as mock_scan, \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images"), \
         patch("app.main.write_report"):
        run(cfg, router, media_files_override=[media])

    mock_scan.assert_not_called()
