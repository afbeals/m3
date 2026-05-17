# Tests for specific fixes made in the review phases.
from __future__ import annotations

import os
import pytest

from app.config import load_config
from app.parser import parse
from app.plugins.base import MetadataResult


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
    assert result.direct_url == "Stranger-Than-Fiction 77675"


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

    with patch("app.writers.nfo._download_image", return_value=False):
        ok = write_images(media, result)

    assert ok is False


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

    with patch("app.writers.nfo._download_image", return_value=True):
        ok = write_images(media, result)

    assert ok is True
