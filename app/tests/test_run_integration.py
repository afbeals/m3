# Integration tests for app/main.run() — the core orchestration function.
# All external I/O (Plex, HTTP, file writes) is mocked so tests are fast and
# deterministic. The goal is to verify the routing/reporting logic, not the
# individual writers (those have their own unit tests).
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import time

from app.main import run, _sweep_tmp_orphans

pytestmark = pytest.mark.integration
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
    cfg.library_exclude_patterns = []
    cfg.notify_min_errors = 1  # default changed to 1 in this cycle
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


class _ValidationErrorPlugin(MetadataPlugin):
    site_id = "validsite"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        from app.plugins.base import PluginValidationError
        raise PluginValidationError("rating must be finite, got nan")


# ---------------------------------------------------------------------------
# run() — core routing outcomes
# ---------------------------------------------------------------------------

def test_run_marks_file_updated_on_success(tmp_path):
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=(True, "/p.jpg", "/f.jpg")), \
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
         patch("app.main.write_images", return_value=(True, "/p.jpg", "/f.jpg")), \
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
         patch("app.main.write_images", return_value=(True, "/p.jpg", "/f.jpg")), \
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
         patch("app.main.write_images", return_value=(True, "/p.jpg", "/f.jpg")), \
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
         patch("app.main.write_images", return_value=(True, "/p.jpg", "/f.jpg")), \
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


def test_run_marks_file_plugin_error_on_validation_failure(tmp_path):
    """When plugin.fetch() raises PluginValidationError, status='plugin_error'."""
    media = _make_media(tmp_path, "Jane Doe % validsite - 12345")
    router = Router({"validsite": _ValidationErrorPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.plugin_errors == 1
    assert report.files[0].status == "plugin_error"


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
         patch("app.main.write_images", return_value=(True, "/p.jpg", "/f.jpg")), \
         patch("app.main.write_report"):
        run(cfg, router, media_files_override=[media])

    mock_scan.assert_not_called()


# ---------------------------------------------------------------------------
# dry_run_strict path (H10)
# ---------------------------------------------------------------------------

def test_dry_run_strict_skips_plugin_fetch(tmp_path):
    """With dry_run_strict=True, plugin.fetch() must not be called."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    plugin = _GoodPlugin()
    router = Router({"mysite": plugin})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report"), \
         patch.object(plugin, "fetch") as mock_fetch:
        run(cfg, router, dry_run_strict=True)

    mock_fetch.assert_not_called()


def test_dry_run_strict_records_skipped_status(tmp_path):
    """Files routed to a plugin in dry_run_strict mode get status='skipped'."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        # dry_run=False so write_report IS called, letting us inspect the report
        run(cfg, router, dry_run_strict=True)

    report = mock_report.call_args.args[0]
    assert report.skipped == 1
    file_result = report.files[0]
    assert file_result.status == "skipped"
    assert "dry-run-strict" in file_result.message


def test_dry_run_strict_skips_connect_plex(tmp_path):
    """dry_run_strict with dry_run=True must not attempt a Plex connection."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex") as mock_connect, \
         patch("app.main.write_report"):
        run(cfg, router, dry_run=True, dry_run_strict=True)

    mock_connect.assert_not_called()


def test_dry_run_strict_without_dry_run_also_skips_plex(tmp_path):
    """Passing dry_run_strict=True alone (without dry_run=True) must also skip Plex."""
    config = _config(tmp_path)
    router = MagicMock()
    router.dispatch.return_value = (MagicMock(), None)

    with patch("app.main.connect_plex") as mock_connect:
        with patch("app.main.scan_library", return_value=([], 0)):
            run(config, router, dry_run=False, dry_run_strict=True)

    mock_connect.assert_not_called()


def test_dry_run_strict_skips_write_report(tmp_path):
    """dry_run_strict with dry_run=True must not write report files."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router, dry_run=True, dry_run_strict=True)

    mock_report.assert_not_called()


def test_dry_run_strict_unmatched_not_counted_as_skipped(tmp_path):
    """Files that can't be routed are still recorded as 'unmatched', not 'skipped'."""
    media = _make_media(tmp_path, "Jane Doe % unknownsite - 99")
    router = Router({})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router, dry_run_strict=True)

    report = mock_report.call_args.args[0]
    assert report.unmatched == 1
    assert report.skipped == 0


# ---------------------------------------------------------------------------
# --retry-failed dedup and boundary (H11)
# ---------------------------------------------------------------------------

def _make_mock_config(tmp_path):
    cfg = MagicMock()
    cfg.report_path = str(tmp_path / "reports")
    cfg.plugin_dir = str(tmp_path / "plugins")
    cfg.library_paths = [str(tmp_path)]
    cfg.log_path = str(tmp_path / "logs")
    cfg.log_retention_days = 30
    cfg.force = False
    cfg.run_schedule = "0 2 * * *"
    cfg.app_name = "m3"
    return cfg


def _invoke_retry(tmp_path, argv_extra, list_runs_val, get_run_map):
    """Run main() retry-failed path with all external dependencies mocked.

    Returns the mock for _run_with_media so callers can assert on the
    MediaFile list it was called with.
    """
    import sys as _sys
    from app.main import main

    mock_cfg = _make_mock_config(tmp_path)

    def fake_get_run(_report_path, filename):
        return get_run_map.get(filename)

    with patch.object(_sys, "argv", ["m3"] + argv_extra), \
         patch("app.main.load_config", return_value=mock_cfg), \
         patch("app.main.setup_logging"), \
         patch("app.main._validate_paths"), \
         patch("app.main.load_plugins", return_value={}), \
         patch("app.web.history.list_runs", return_value=list_runs_val), \
         patch("app.web.history.get_run", side_effect=fake_get_run), \
         patch("app.main._sweep_tmp_orphans"), \
         patch("app.main._run_with_media") as mock_run:
        try:
            main()
        except SystemExit:
            pass

    return mock_run


def test_retry_failed_dedup_across_runs(tmp_path):
    """Same path appearing in two run reports must be retried only once."""
    shared_path = str(tmp_path / "Jane Doe % mysite - 12345.mp4")
    open(shared_path, "w").close()

    list_runs_val = [
        {"filename": "run_20250102.json"},
        {"filename": "run_20250101.json"},
    ]
    get_run_map = {
        "run_20250102.json": {"files": [{"path": shared_path, "status": "error"}]},
        "run_20250101.json": {"files": [{"path": shared_path, "status": "scrape_error"}]},
    }

    mock_run = _invoke_retry(tmp_path, ["--retry-failed", "2"], list_runs_val, get_run_map)

    mock_run.assert_called_once()
    _, _, media_list = mock_run.call_args.args
    assert len(media_list) == 1
    assert media_list[0].path == shared_path


def test_retry_failed_n_boundary(tmp_path):
    """--retry-failed=1 reads only the most recent run; --retry-failed=2 reads two."""
    path_run1 = str(tmp_path / "Jane Doe % mysite - 001.mp4")
    path_run2 = str(tmp_path / "Jane Doe % mysite - 002.mp4")
    open(path_run1, "w").close()
    open(path_run2, "w").close()

    list_runs_val = [
        {"filename": "run_20250102.json"},  # most recent
        {"filename": "run_20250101.json"},
    ]
    get_run_map = {
        "run_20250102.json": {"files": [{"path": path_run1, "status": "error"}]},
        "run_20250101.json": {"files": [{"path": path_run2, "status": "error"}]},
    }

    # N=1: only the most recent run → only path_run1
    mock_run_1 = _invoke_retry(tmp_path, ["--retry-failed", "1"], list_runs_val, get_run_map)
    mock_run_1.assert_called_once()
    _, _, media_1 = mock_run_1.call_args.args
    assert len(media_1) == 1
    assert media_1[0].path == path_run1

    # N=2: both runs → both paths
    mock_run_2 = _invoke_retry(tmp_path, ["--retry-failed", "2"], list_runs_val, get_run_map)
    mock_run_2.assert_called_once()
    _, _, media_2 = mock_run_2.call_args.args
    assert len(media_2) == 2


@pytest.mark.parametrize("status", ["error", "scrape_error", "image_error", "plugin_error"])
def test_retry_failed_includes_all_error_statuses(tmp_path, status):
    """--retry-failed must retry all error-like statuses including image_error and plugin_error."""
    path = str(tmp_path / "Jane Doe % mysite - 001.mp4")
    open(path, "w").close()

    list_runs_val = [{"filename": "run_20250101.json"}]
    get_run_map = {
        "run_20250101.json": {"files": [{"path": path, "status": status}]},
    }

    mock_run = _invoke_retry(tmp_path, ["--retry-failed", "1"], list_runs_val, get_run_map)

    mock_run.assert_called_once()
    _, _, media_list = mock_run.call_args.args
    assert len(media_list) == 1, f"Expected 1 file for status={status!r}, got {len(media_list)}"
    assert media_list[0].path == path


def test_retry_failed_skips_nonexistent_files(tmp_path):
    """Paths in the report that no longer exist on disk must be skipped silently."""
    existing_path = str(tmp_path / "Jane Doe % mysite - 001.mp4")
    missing_path = str(tmp_path / "Deleted File % mysite - 999.mp4")
    open(existing_path, "w").close()
    # missing_path is intentionally NOT created

    list_runs_val = [{"filename": "run_20250101.json"}]
    get_run_map = {
        "run_20250101.json": {
            "files": [
                {"path": existing_path, "status": "error"},
                {"path": missing_path, "status": "error"},
            ]
        },
    }

    mock_run = _invoke_retry(tmp_path, ["--retry-failed", "1"], list_runs_val, get_run_map)

    mock_run.assert_called_once()
    _, _, media_list = mock_run.call_args.args
    paths = [m.path for m in media_list]
    assert existing_path in paths
    assert missing_path not in paths


# ---------------------------------------------------------------------------
# T3 — image_error when write_images returns False
# ---------------------------------------------------------------------------

def test_run_image_error_when_write_images_returns_false(tmp_path):
    """When write_images returns False, the file must be recorded as
    image_error (not updated) and the image_errors counter incremented."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=(False, None, None)), \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.image_errors == 1
    assert report.updated == 0
    # DC4: message must say "not written" (not "NFO written")
    image_error_result = next(f for f in report.files if f.status == "image_error")
    assert image_error_result.message is not None, "image_error FileResult should have a message"
    assert "not written" in image_error_result.message.lower()


# ---------------------------------------------------------------------------
# T1 (plex) — push_to_plex returning False → plex_failed=True
# ---------------------------------------------------------------------------

def test_run_marks_plex_failed_when_push_to_plex_returns_false(tmp_path):
    """When push_to_plex returns False, FileResult.plex_failed must be True."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)
    mock_plex = MagicMock()

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_nfo"), \
         patch("app.main.write_images", return_value=(True, "/p.jpg", "/f.jpg")), \
         patch("app.main.connect_plex", return_value=mock_plex), \
         patch("app.main.push_to_plex", return_value=False), \
         patch("app.main.write_report") as mock_report:
        run(cfg, router)

    report = mock_report.call_args.args[0]
    assert report.files[0].status == "updated"
    assert report.files[0].plex_failed is True, (
        "plex_failed should be True when push_to_plex returns False"
    )


# ---------------------------------------------------------------------------
# T1 — write_images paths flow as basenames into write_nfo
# ---------------------------------------------------------------------------

def test_run_passes_image_basenames_to_write_nfo(tmp_path):
    """write_images returned paths must be passed as basenames to write_nfo."""
    media = _make_media(tmp_path, "Jane Doe % mysite - 12345")
    router = Router({"mysite": _GoodPlugin()})
    cfg = _config(tmp_path)

    poster_full = str(tmp_path / "Jane Doe % mysite - 12345-poster.webp")
    fanart_full = str(tmp_path / "Jane Doe % mysite - 12345-fanart.png")

    with patch("app.main.scan_library", return_value=([media], 0)), \
         patch("app.main.write_images", return_value=(True, poster_full, fanart_full)), \
         patch("app.main.write_nfo") as mock_write_nfo, \
         patch("app.main.connect_plex", return_value=None), \
         patch("app.main.write_report"):
        run(cfg, router)

    assert mock_write_nfo.called, "write_nfo should have been called"
    call_kwargs = mock_write_nfo.call_args[1]
    assert call_kwargs.get("poster_filename") == "Jane Doe % mysite - 12345-poster.webp", \
        f"Expected basename-only poster_filename, got {call_kwargs.get('poster_filename')!r}"
    assert call_kwargs.get("fanart_filename") == "Jane Doe % mysite - 12345-fanart.png", \
        f"Expected basename-only fanart_filename, got {call_kwargs.get('fanart_filename')!r}"


# ---------------------------------------------------------------------------
# T4 — _sweep_tmp_orphans
# ---------------------------------------------------------------------------

class TestSweepTmpOrphans:
    def test_removes_stale_tmp_file(self, tmp_path):
        """A .tmp file with mtime older than 30 min must be deleted."""
        stale = tmp_path / "stale_write.tmp"
        stale.write_text("partial")
        # Set mtime to 31 minutes ago
        old_time = time.time() - 31 * 60
        os.utime(str(stale), (old_time, old_time))

        _sweep_tmp_orphans([str(tmp_path)])

        assert not stale.exists(), "Stale .tmp file should have been removed"

    def test_keeps_recent_tmp_file(self, tmp_path):
        """A .tmp file with mtime less than 60 seconds ago must NOT be deleted."""
        fresh = tmp_path / "fresh_write.tmp"
        fresh.write_text("in-progress")
        # Leave mtime as-is (just created — well within the safe window)

        _sweep_tmp_orphans([str(tmp_path)])

        assert fresh.exists(), "Recent .tmp file should not be removed"

    def test_does_not_delete_non_tmp_files(self, tmp_path):
        """Non-.tmp files must never be deleted, regardless of age."""
        nfo = tmp_path / "movie.nfo"
        nfo.write_text("<movie/>")
        # Set mtime to 2 hours ago
        old_time = time.time() - 2 * 3600
        os.utime(str(nfo), (old_time, old_time))

        _sweep_tmp_orphans([str(tmp_path)])

        assert nfo.exists(), "Non-.tmp files must not be deleted"

    def test_removes_stale_tmp_in_subdirectory(self, tmp_path):
        """_sweep_tmp_orphans must find and remove stale .tmp files in subdirectories."""
        subdir = tmp_path / "subdir" / "nested"
        subdir.mkdir(parents=True)
        stale_tmp = subdir / "old.nfo.tmp"
        stale_tmp.write_bytes(b"stale")

        # Set mtime to more than 30 minutes ago
        old_mtime = time.time() - 2000
        os.utime(str(stale_tmp), (old_mtime, old_mtime))

        _sweep_tmp_orphans([str(tmp_path)])

        assert not stale_tmp.exists(), "Stale .tmp in subdirectory should be removed"


# ---------------------------------------------------------------------------
# H8 — Full-stack plugin→NFO round-trip integration test
# ---------------------------------------------------------------------------

def test_plugin_fetch_result_roundtrips_through_nfo(tmp_path):
    """Full-stack: plugin.fetch() → write_nfo() → parse NFO and verify fields."""
    from app.plugins.base import MetadataResult, ParsedFilename
    from app.writers.nfo import write_nfo
    from lxml import etree

    # Build a minimal concrete plugin inline (plain class — no site_id required)
    class _TestPlugin:
        def fetch(self, parsed):
            return MetadataResult(
                title="Round Trip Scene",
                actors=["Jane Doe", "John Smith"],
                genres=["Drama", "Thriller"],
                year=2024,
                rating=7.5,
                summary="A test scene for round-trip verification.",
                source_id="rt-12345",
                source_url="https://example.com/scenes/rt-12345",
            )

    plugin = _TestPlugin()
    parsed = ParsedFilename(
        form="general",
        actors=["Jane Doe"],
        genres=[],
        site="testsite",
        match_subtype="exact",
        scene_id="rt-12345",
        raw_match_payload="testsite - rt-12345",
    )

    media = MediaFile(
        path=str(tmp_path / "Jane Doe % testsite - rt-12345.mp4"),
        stem="Jane Doe % testsite - rt-12345",
        nfo_path=str(tmp_path / "Jane Doe % testsite - rt-12345.nfo"),
    )

    # Call plugin and write NFO
    result = plugin.fetch(parsed)
    assert result is not None
    write_nfo(media, result)

    # Parse and verify
    nfo_path = tmp_path / "Jane Doe % testsite - rt-12345.nfo"
    assert nfo_path.exists(), "NFO file was not created"

    tree = etree.parse(str(nfo_path))
    root = tree.getroot()

    assert root.findtext("title") == "Round Trip Scene"
    assert root.findtext("year") == "2024"
    assert root.findtext("rating") == "7.5"
    assert root.findtext("plot") == "A test scene for round-trip verification."
    # uniqueid has a type="m3" attribute; findtext still works for the text content
    uid_el = root.find("uniqueid")
    assert uid_el is not None, "uniqueid element missing"
    assert uid_el.text == "rt-12345"

    actor_names = [el.findtext("name") for el in root.findall("actor")]
    assert "Jane Doe" in actor_names
    assert "John Smith" in actor_names

    genres = [el.text for el in root.findall("genre")]
    assert "Drama" in genres
    assert "Thriller" in genres
