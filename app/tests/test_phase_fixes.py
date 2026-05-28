# Tests for specific fixes made in the review phases.
from __future__ import annotations

import os
import tempfile
import time
import pytest
from unittest.mock import MagicMock

from app.config import load_config
from app.logging_setup import cleanup_old_files
from app.main import _validate_paths
from app.parser import parse
from app.plugins.base import MetadataResult
from app.scheduler import build_scheduler

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Parser: empty payload returns None (issue #8)
# ---------------------------------------------------------------------------

def test_parse_percent_with_empty_payload_returns_none():
    assert parse("Jane Doe %") is None


def test_parse_percent_with_only_spaces_returns_none():
    assert parse("Jane Doe %   ") is None


# ---------------------------------------------------------------------------
# Parser: url slug heuristic (issue #20)
# ---------------------------------------------------------------------------

def test_title_ending_in_digit_is_not_slug():
    result = parse("Jane Doe % mysite - Chapter 5")
    assert result is not None
    assert result.match_subtype == "limited"
    assert result.title == "Chapter 5"
    assert result.direct_url is None


def test_hyphenated_no_space_token_is_slug():
    result = parse("Jane Doe % mysite - eager-hands")
    assert result is not None
    assert result.match_subtype == "exact"
    assert result.direct_url == "eager-hands"


def test_hyphenated_with_trailing_numeric_id_is_slug():
    result = parse("Jane Doe % mysite - Stranger-Than-Fiction 77675")
    assert result is not None
    assert result.match_subtype == "exact"
    # DC2 fix: slug and id are split — direct_url holds only the slug part
    assert result.direct_url == "Stranger-Than-Fiction"
    assert result.scene_id == "77675"


# ---------------------------------------------------------------------------
# MetadataResult: source_id normalisation (issue #25)
# ---------------------------------------------------------------------------

def test_source_id_none_string_is_normalised():
    r = MetadataResult(title="Test", source_id="None")
    assert r.source_id is None


def test_source_id_empty_string_is_normalised():
    r = MetadataResult(title="Test", source_id="")
    assert r.source_id is None


def test_source_id_valid_value_preserved():
    r = MetadataResult(title="Test", source_id="12345")
    assert r.source_id == "12345"


def test_source_id_none_is_preserved():
    r = MetadataResult(title="Test", source_id=None)
    assert r.source_id is None


# ---------------------------------------------------------------------------
# Config: int env var parsing (issue #17)
# ---------------------------------------------------------------------------

def test_config_bad_int_env_var_raises_clear_error(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://localhost:32400")
    monkeypatch.setenv("PLEX_TOKEN", "token")
    monkeypatch.setenv("LOG_RETENTION_DAYS", "thirty")
    with pytest.raises(ValueError, match="LOG_RETENTION_DAYS"):
        load_config()


def test_config_valid_int_env_var_parses(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://localhost:32400")
    monkeypatch.setenv("PLEX_TOKEN", "token")
    monkeypatch.setenv("LOG_RETENTION_DAYS", "14")
    cfg = load_config()
    assert cfg.log_retention_days == 14


# ---------------------------------------------------------------------------
# Config: plex_token redacted in repr (issue #18)
# ---------------------------------------------------------------------------

def test_config_repr_redacts_plex_token(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://localhost:32400")
    monkeypatch.setenv("PLEX_TOKEN", "super-secret-token")
    cfg = load_config()
    assert "super-secret-token" not in repr(cfg)
    assert "***" in repr(cfg)


# ---------------------------------------------------------------------------
# write_images: returns bool and logs errors (issue #12)
# ---------------------------------------------------------------------------

def test_write_images_returns_false_when_download_fails(tmp_path):
    from unittest.mock import patch
    from app.scanner import MediaFile
    from app.writers.nfo import write_images

    media = MediaFile(
        path=str(tmp_path / "My Movie.mp4"),
        stem="My Movie",
        nfo_path=str(tmp_path / "My Movie.nfo"),
    )
    result = MetadataResult(title="T", poster_url="http://x.com/p.jpg")

    with patch("app.writers.nfo._download_image", return_value=None):
        ok, poster_path, fanart_path = write_images(media, result)

    assert ok is False
    assert poster_path is None


def test_write_images_returns_true_when_all_succeed(tmp_path):
    from unittest.mock import patch
    from app.scanner import MediaFile
    from app.writers.nfo import write_images

    media = MediaFile(
        path=str(tmp_path / "My Movie.mp4"),
        stem="My Movie",
        nfo_path=str(tmp_path / "My Movie.nfo"),
    )
    result = MetadataResult(title="T", poster_url="http://x.com/p.jpg",
                            fanart_url="http://x.com/f.jpg")

    with patch("app.writers.nfo._download_image", return_value="/some/path.jpg"):
        ok, poster_path, fanart_path = write_images(media, result)

    assert ok is True
    assert poster_path == "/some/path.jpg"
    assert fanart_path == "/some/path.jpg"


# ---------------------------------------------------------------------------
# Parser: bare "Add" stem returns None (no actors = invalid)
# ---------------------------------------------------------------------------

def test_parse_bare_add_returns_none():
    assert parse("Add") is None


def test_parse_bare_add_case_insensitive_returns_none():
    assert parse("add") is None


def test_parse_add_with_actor_is_valid():
    result = parse("Add Jane Doe")
    assert result is not None
    assert result.form == "add"
    assert "Jane Doe" in result.actors


# ---------------------------------------------------------------------------
# Parser: % in match payload corrupts site token
# ---------------------------------------------------------------------------

def test_parse_percent_in_payload_returns_none():
    # The payload starts with a %-encoded character before the real site token.
    # The parser should return None rather than treating "%20" as the site.
    assert parse("Jane Doe % %20 - real-site - 12345") is None


# ---------------------------------------------------------------------------
# Scheduler: ValueError on bad cron expression
# ---------------------------------------------------------------------------

def test_scheduler_raises_on_bad_cron_expression():
    with pytest.raises(ValueError, match="5-field cron"):
        build_scheduler(lambda: None, "not-a-cron")


def test_scheduler_raises_on_too_few_cron_fields():
    with pytest.raises(ValueError, match="5-field cron"):
        build_scheduler(lambda: None, "0 3 * *")  # only 4 fields


# ---------------------------------------------------------------------------
# cleanup_old_files: rotated log files deleted correctly
# ---------------------------------------------------------------------------

def test_cleanup_deletes_rotated_log_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a rotated log file with an old mtime
        rotated = os.path.join(tmpdir, "m3.log.1")
        open(rotated, "w").close()
        old_time = time.time() - (40 * 24 * 3600)  # 40 days ago
        os.utime(rotated, (old_time, old_time))

        removed = cleanup_old_files(tmpdir, retention_days=30, pattern_suffix=".log")

        assert removed == 1
        assert not os.path.exists(rotated)


def test_cleanup_skips_active_log_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Active log file with old mtime — must not be deleted
        active = os.path.join(tmpdir, "m3.log")
        open(active, "w").close()
        old_time = time.time() - (40 * 24 * 3600)
        os.utime(active, (old_time, old_time))

        removed = cleanup_old_files(tmpdir, retention_days=30, pattern_suffix=".log")

        assert removed == 0
        assert os.path.exists(active)


def test_cleanup_skips_files_within_retention():
    with tempfile.TemporaryDirectory() as tmpdir:
        recent = os.path.join(tmpdir, "m3.log.1")
        open(recent, "w").close()
        # mtime is now — within retention window

        removed = cleanup_old_files(tmpdir, retention_days=30, pattern_suffix=".log")

        assert removed == 0
        assert os.path.exists(recent)


# ---------------------------------------------------------------------------
# _validate_paths: startup path validation
# ---------------------------------------------------------------------------

def test_validate_paths_passes_when_dir_exists_and_writable(monkeypatch, tmp_path):
    lib = tmp_path / "media"
    lib.mkdir()
    reports = tmp_path / "reports"
    logs = tmp_path / "logs"

    cfg = MagicMock()
    cfg.library_paths = [str(lib)]
    cfg.report_path = str(reports)
    cfg.log_path = str(logs)

    # Should not raise
    _validate_paths(cfg)


def test_validate_paths_raises_when_no_library_path_exists(tmp_path):
    cfg = MagicMock()
    cfg.library_paths = ["/nonexistent/path/abc123"]
    cfg.report_path = str(tmp_path / "reports")
    cfg.log_path = str(tmp_path / "logs")

    with pytest.raises(SystemExit):
        _validate_paths(cfg)


def test_validate_paths_raises_when_report_path_not_writable(tmp_path, monkeypatch):
    lib = tmp_path / "media"
    lib.mkdir()

    cfg = MagicMock()
    cfg.library_paths = [str(lib)]
    cfg.report_path = "/nonexistent/deeply/nested/unwritable/path"
    cfg.log_path = str(tmp_path / "logs")

    with pytest.raises(SystemExit):
        _validate_paths(cfg)
