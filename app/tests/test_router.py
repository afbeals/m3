"""Tests for the router dispatch logic."""

from __future__ import annotations
import pytest

from unittest.mock import MagicMock

from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.router import Router

pytestmark = pytest.mark.unit


class _FakePlugin(MetadataPlugin):
    site_id = "fakesite"
    aliases = ("FS",)

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        return MetadataResult(title="Fake Result")


class TestRouter:
    def setup_method(self):
        plugin = _FakePlugin()
        self.registry = {k: plugin for k in plugin.all_ids()}
        self.router = Router(self.registry)

    def test_dispatches_to_plugin_by_site_id(self):
        parsed, plugin = self.router.dispatch("Jane Doe % fakesite - 12345")
        assert plugin is not None
        assert isinstance(plugin, _FakePlugin)

    def test_dispatches_to_plugin_by_alias(self):
        parsed, plugin = self.router.dispatch("Jane Doe % FS - 12345")
        assert plugin is not None

    def test_returns_none_plugin_for_unknown_site(self):
        parsed, plugin = self.router.dispatch("Jane Doe % unknownsite - 12345")
        assert parsed is not None
        assert plugin is None

    def test_add_form_returns_parsed_and_no_plugin(self):
        parsed, plugin = self.router.dispatch("Add Jane Doe At MyStudio")
        assert parsed is not None
        assert parsed.form == "add"
        assert plugin is None

    def test_unparseable_returns_none_none(self):
        parsed, plugin = self.router.dispatch("random file name no tokens")
        assert parsed is None
        assert plugin is None
