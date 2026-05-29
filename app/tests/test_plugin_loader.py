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


# ---------------------------------------------------------------------------
# H7 — old_registry close() path tests
# ---------------------------------------------------------------------------

class TestOldRegistryClose:
    def test_close_called_on_old_registry_plugins(self, tmp_path):
        """load_plugins with old_registry calls close() on each plugin."""
        plugin_code = '''
from app.plugins.base import MetadataPlugin, ParsedFilename, MetadataResult

class MockPlugin(MetadataPlugin):
    site_id = "testclose"
    def fetch(self, parsed): return None
    def close(self): self.closed = True
'''
        (tmp_path / "mock_plugin.py").write_text(plugin_code)
        registry = load_plugins(str(tmp_path))
        instance = registry["testclose"]

        # Reload with old_registry — should call close() on old instance
        load_plugins(str(tmp_path), old_registry=dict(registry))
        assert getattr(instance, "closed", False), "close() was not called"

    def test_close_called_once_for_aliased_plugin(self, tmp_path):
        """When a plugin is registered under site_id AND an alias, close() fires exactly once."""
        plugin_code = '''
from app.plugins.base import MetadataPlugin, ParsedFilename, MetadataResult

class AliasedPlugin(MetadataPlugin):
    site_id = "mainid"
    aliases = ("otherid",)
    def __init__(self):
        self.close_count = 0
    def fetch(self, parsed): return None
    def close(self): self.close_count += 1
'''
        (tmp_path / "aliased_plugin.py").write_text(plugin_code)
        registry = load_plugins(str(tmp_path))
        instance = registry["mainid"]
        assert registry["mainid"] is registry["otherid"]

        load_plugins(str(tmp_path), old_registry=dict(registry))
        assert instance.close_count == 1, f"close() called {instance.close_count} times, expected 1"

    def test_close_exception_does_not_abort_reload(self, tmp_path):
        """If close() raises, reload still completes and registry is updated."""
        plugin_code = '''
from app.plugins.base import MetadataPlugin, ParsedFilename, MetadataResult

class ErrorCloser(MetadataPlugin):
    site_id = "errclose"
    def fetch(self, parsed): return None
    def close(self): raise RuntimeError("close failed")
'''
        (tmp_path / "error_closer.py").write_text(plugin_code)
        registry = load_plugins(str(tmp_path))
        old_instance = registry["errclose"]

        # Should not raise despite close() failing
        new_registry = load_plugins(str(tmp_path), old_registry=dict(registry))
        assert "errclose" in new_registry
        assert new_registry["errclose"] is not old_instance


# ---------------------------------------------------------------------------
# H9 — rating validation edge cases
# ---------------------------------------------------------------------------

import math
from app.plugins.base import MetadataResult, PluginValidationError


class TestMetadataResultValidation:
    def _make(self, **kwargs):
        defaults = {"title": "Test Scene", "actors": ["Actor One"]}
        defaults.update(kwargs)
        return MetadataResult(**defaults)

    @pytest.mark.parametrize("rating", [float("nan"), float("inf"), float("-inf"), -0.1, 10.001])
    def test_invalid_rating_raises(self, rating):
        with pytest.raises((ValueError, PluginValidationError)):
            self._make(rating=rating)

    @pytest.mark.parametrize("rating", [0.0, 5.0, 10.0, None])
    def test_valid_rating_accepted(self, rating):
        result = self._make(rating=rating)
        assert result.rating == rating

    # ---------------------------------------------------------------------------
    # CQ3 — None list field raises PluginValidationError
    # ---------------------------------------------------------------------------

    def test_none_actors_raises_plugin_validation_error(self):
        with pytest.raises(PluginValidationError, match="actors must be a list"):
            MetadataResult(title="Test", actors=None)

    @pytest.mark.parametrize("field", ["genres", "tags", "labels"])
    def test_none_list_fields_raises(self, field):
        with pytest.raises(PluginValidationError, match=f"{field} must be a list"):
            MetadataResult(title="Test", **{field: None})

    # ---------------------------------------------------------------------------
    # T3 — str(None) coercion for summary/content_rating/source_url
    # ---------------------------------------------------------------------------

    @pytest.mark.parametrize("field,value", [
        ("summary", "None"),
        ("summary", "none"),
        ("summary", ""),
        ("content_rating", "None"),
        ("content_rating", "  none  "),
        ("source_url", ""),
        ("source_url", "NONE"),
    ])
    def test_string_none_coercion(self, field, value):
        result = MetadataResult(title="Test", **{field: value})
        assert getattr(result, field) is None, \
            f"Expected {field}={value!r} to be coerced to None"

    # ---------------------------------------------------------------------------
    # T4 — title whitespace stripping and empty title ValueError
    # ---------------------------------------------------------------------------

    def test_title_whitespace_stripped(self):
        result = MetadataResult(title="  My Scene  ")
        assert result.title == "My Scene"

    @pytest.mark.parametrize("title", ["", "   ", "\t\n"])
    def test_empty_title_raises(self, title):
        with pytest.raises((ValueError, PluginValidationError)):
            MetadataResult(title=title)


# ---------------------------------------------------------------------------
# T1 — two plugin classes in one file
# ---------------------------------------------------------------------------

class TestTwoPluginsInOneFile:
    def test_two_plugins_in_one_file(self, tmp_path):
        """A file with two MetadataPlugin subclasses registers both."""
        plugin_code = '''
from app.plugins.base import MetadataPlugin, ParsedFilename, MetadataResult

class PluginAlpha(MetadataPlugin):
    site_id = "alpha"
    def fetch(self, p): return None

class PluginBeta(MetadataPlugin):
    site_id = "beta"
    def fetch(self, p): return None
'''
        (tmp_path / "two_plugins.py").write_text(plugin_code)
        registry = load_plugins(str(tmp_path))
        assert "alpha" in registry
        assert "beta" in registry
        assert registry["alpha"] is not registry["beta"]


# ---------------------------------------------------------------------------
# T2 — cross-plugin id/alias collision
# ---------------------------------------------------------------------------

class TestCrossPluginAliasCollision:
    def test_cross_plugin_alias_collision(self, tmp_path):
        """Plugin B's alias colliding with Plugin A's site_id: B wins, A's entry replaced."""
        code_a = '''
from app.plugins.base import MetadataPlugin, ParsedFilename, MetadataResult
class PluginA(MetadataPlugin):
    site_id = "shared"
    def fetch(self, p): return None
'''
        code_b = '''
from app.plugins.base import MetadataPlugin, ParsedFilename, MetadataResult
class PluginB(MetadataPlugin):
    site_id = "unique"
    aliases = ("shared",)
    def fetch(self, p): return None
'''
        # Write A first, B second so B's registration processes after A
        (tmp_path / "a_plugin.py").write_text(code_a)
        (tmp_path / "b_plugin.py").write_text(code_b)
        registry = load_plugins(str(tmp_path))

        # "shared" should point to exactly one plugin (B, since it overwrites A)
        assert "shared" in registry
        assert "unique" in registry
        assert registry["shared"] is registry["unique"]  # both should be PluginB
