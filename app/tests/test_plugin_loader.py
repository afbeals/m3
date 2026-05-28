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


# ---------------------------------------------------------------------------
# T1 — duplicate site_id: last-wins (alphabetically last filename)
# ---------------------------------------------------------------------------

_DUP_PLUGIN_A = """
from __future__ import annotations
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

class AlphaPlugin(MetadataPlugin):
    site_id = "dupsite"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        return MetadataResult(title="Alpha")
"""

_DUP_PLUGIN_B = """
from __future__ import annotations
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

class BetaPlugin(MetadataPlugin):
    site_id = "dupsite"

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        return MetadataResult(title="Beta")
"""


class TestDuplicateSiteId:
    @pytest.mark.unit
    def test_duplicate_site_id_last_wins(self, caplog):
        """Two plugins declaring the same site_id: the one from the
        alphabetically-later filename overwrites the first, and a
        logger.error is emitted for the collision."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # "aaa_plugin.py" sorts before "zzz_plugin.py"
            _write(tmpdir, "aaa_plugin.py", _DUP_PLUGIN_A)
            _write(tmpdir, "zzz_plugin.py", _DUP_PLUGIN_B)
            with caplog.at_level("ERROR"):
                registry = load_plugins(tmpdir)

        # Exactly one entry for the duplicate id
        assert "dupsite" in registry
        dup_entries = {k: v for k, v in registry.items() if k == "dupsite"}
        assert len(dup_entries) == 1

        # The winning instance must be from the later file (BetaPlugin)
        assert type(registry["dupsite"]).__name__ == "BetaPlugin"

        # A logger.error must have been emitted for the id collision
        assert any("dupsite" in r.message and r.levelname == "ERROR" for r in caplog.records), (
            "Expected an ERROR log about the duplicate plugin id 'dupsite'"
        )


# ---------------------------------------------------------------------------
# T11 — plugin __init__ raises
# ---------------------------------------------------------------------------

_INIT_RAISES_PLUGIN = """
from __future__ import annotations
from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

class InitRaisesPlugin(MetadataPlugin):
    site_id = "initfail"

    def __init__(self):
        raise RuntimeError("init failed")

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        return None
"""


class TestInitRaises:
    @pytest.mark.unit
    def test_init_raises_returns_empty_registry(self, caplog):
        """A plugin whose __init__ raises must be skipped (empty registry for
        that file) and the exception must be logged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write(tmpdir, "bad_init.py", _INIT_RAISES_PLUGIN)
            with caplog.at_level("WARNING"):
                registry = load_plugins(tmpdir)

        assert "initfail" not in registry

        # The exception should have been logged (as exception → ERROR or WARNING)
        assert any(
            "InitRaisesPlugin" in r.message or "bad_init" in r.message
            for r in caplog.records
        ), "Expected a log message about the failing plugin instantiation"

    @pytest.mark.unit
    def test_init_raises_does_not_block_other_plugins(self, caplog):
        """Even when one plugin's __init__ raises, other valid plugins in the
        same directory must still be loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            _write(tmpdir, "aaa_bad.py", _INIT_RAISES_PLUGIN)
            _write(tmpdir, "zzz_good.py", _VALID_PLUGIN)
            with caplog.at_level("WARNING"):
                registry = load_plugins(tmpdir)

        # The valid plugin should still be registered
        assert "testsite" in registry
