"""Tests for the plugin loader."""

from __future__ import annotations
import pytest

import os
import tempfile

from app.plugins.loader import load_plugins


_VALID_PLUGIN = """
from __future__ import annotations
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

class TestPlugin(MetadataPlugin):
    site_id = "testsite"
    aliases = ("TS",)

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        return MetadataResult(title="Test")
"""

_NO_SITE_ID_PLUGIN = """
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

pytestmark = pytest.mark.unit

class BadPlugin(MetadataPlugin):
    site_id = ""
    def fetch(self, parsed):
        return None
"""

_BROKEN_PLUGIN = """
this is not valid python !!!
"""


class TestLoadPlugins:
    def test_loads_valid_plugin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write(tmpdir, "myplugin.py", _VALID_PLUGIN)
            registry = load_plugins(tmpdir)
        assert "testsite" in registry
        assert "ts" in registry

    def test_skips_plugin_with_no_site_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write(tmpdir, "bad.py", _NO_SITE_ID_PLUGIN)
            registry = load_plugins(tmpdir)
        assert registry == {}

    def test_skips_broken_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write(tmpdir, "broken.py", _BROKEN_PLUGIN)
            registry = load_plugins(tmpdir)
        assert registry == {}

    def test_returns_empty_for_missing_dir(self):
        registry = load_plugins("/nonexistent/plugins")
        assert registry == {}

    def test_skips_underscore_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write(tmpdir, "__init__.py", _VALID_PLUGIN)
            registry = load_plugins(tmpdir)
        assert registry == {}


def _write(directory: str, filename: str, content: str) -> None:
    with open(os.path.join(directory, filename), "w") as fh:
        fh.write(content)
