# Tests for app/config.py
from __future__ import annotations

import os

import pytest

from app.config import load_config

pytestmark = pytest.mark.unit


def _base_env(overrides: dict | None = None) -> dict:
    env = {
        "PLEX_URL": "http://localhost:32400",
        "PLEX_TOKEN": "test-token",
        "LIBRARY_PATHS": "/media",
    }
    if overrides:
        env.update(overrides)
    return env


def test_app_name_defaults_to_pm(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("APP_NAME", raising=False)
    cfg = load_config()
    assert cfg.app_name == "m3"


def test_app_name_reads_from_env(monkeypatch):
    for k, v in _base_env({"APP_NAME": "myagent"}).items():
        monkeypatch.setenv(k, v)
    cfg = load_config()
    assert cfg.app_name == "myagent"


def test_missing_plex_url_raises(monkeypatch):
    monkeypatch.setenv("PLEX_TOKEN", "tok")
    monkeypatch.delenv("PLEX_URL", raising=False)
    with pytest.raises(ValueError, match="PLEX_URL"):
        load_config()


def test_missing_plex_token_raises(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://localhost:32400")
    monkeypatch.delenv("PLEX_TOKEN", raising=False)
    with pytest.raises(ValueError, match="PLEX_TOKEN"):
        load_config()


def test_library_paths_splits_on_comma(monkeypatch):
    for k, v in _base_env({"LIBRARY_PATHS": "/media/Movies,/media/TV"}).items():
        monkeypatch.setenv(k, v)
    cfg = load_config()
    assert cfg.library_paths == ["/media/Movies", "/media/TV"]


def test_web_enabled_false_variants(monkeypatch):
    for val in ("false", "0", "no", "off"):
        for k, v in _base_env({"WEB_ENABLED": val}).items():
            monkeypatch.setenv(k, v)
        cfg = load_config()
        assert cfg.web_enabled is False, f"Expected False for WEB_ENABLED={val!r}"


def test_notify_min_errors_defaults_to_zero(monkeypatch):
    for k, v in _base_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("NOTIFY_MIN_ERRORS", raising=False)
    cfg = load_config()
    assert cfg.notify_min_errors == 0


def test_notify_min_errors_reads_from_env(monkeypatch):
    for k, v in _base_env({"NOTIFY_MIN_ERRORS": "1"}).items():
        monkeypatch.setenv(k, v)
    cfg = load_config()
    assert cfg.notify_min_errors == 1
