"""Tests for the community plugin registry."""

import json
import tempfile
from pathlib import Path

import pytest

from ata_coder.plugin_registry import PluginRegistry, PluginMeta, get_plugin_registry


class TestPluginMeta:
    """PluginMeta dataclass tests."""

    def test_from_dict_basic(self):
        """from_dict parses a minimal plugin entry."""
        d = {"name": "test-plugin", "version": "1.0.0", "description": "A test"}
        meta = PluginMeta.from_dict(d)
        assert meta.name == "test-plugin"
        assert meta.version == "1.0.0"
        assert meta.description == "A test"

    def test_from_dict_defaults(self):
        """Missing fields get sensible defaults."""
        meta = PluginMeta.from_dict({"name": "minimal"})
        assert meta.version == "0.1.0"
        assert meta.package_name == ""
        assert meta.license == "MIT"

    def test_to_dict_roundtrip(self):
        """to_dict → from_dict should preserve all fields."""
        original = PluginMeta(
            name="fmt", version="2.0.0", description="Formatter",
            author="Alice", homepage="https://example.com",
            package_name="ata-fmt", entry_point="ata_fmt:export",
            tags=["tool", "formatter"],
        )
        d = original.to_dict()
        restored = PluginMeta.from_dict(d)
        assert restored.name == original.name
        assert restored.version == original.version
        assert restored.package_name == original.package_name
        assert restored.entry_point == original.entry_point
        assert restored.tags == original.tags


class TestPluginRegistry:
    """PluginRegistry tests."""

    @pytest.fixture
    def registry(self):
        """Create a registry with a temp cache dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            reg = PluginRegistry(cache_dir=tmpdir)
            yield reg

    def test_init_creates_cache_dir(self, registry):
        """Registry init creates the cache directory."""
        assert registry._cache_dir.exists()

    def test_list_available_empty(self, registry):
        """New registry has no plugins until refresh."""
        assert registry.list_available() == []

    def test_search_empty(self, registry):
        """Search on empty index returns empty list."""
        assert registry.search("test") == []

    def test_get_missing(self, registry):
        """get() on missing plugin returns None."""
        assert registry.get("nonexistent") is None

    def test_refresh_index_with_local_file(self, registry):
        """refresh_index works with a local file:// URL."""
        # Write a mock index to a temp file
        mock_index = {
            "version": "1.0",
            "plugins": [
                {
                    "name": "code-formatter",
                    "version": "1.0.0",
                    "description": "Auto-format code on save",
                    "author": "Community",
                    "package_name": "ata-coder-plugin-fmt",
                    "entry_point": "ata_coder_plugin_fmt:export_extension",
                    "tags": ["tool", "formatter"],
                },
                {
                    "name": "sql-linter",
                    "version": "0.5.0",
                    "description": "Lint SQL queries",
                    "author": "DB Team",
                    "tags": ["linter", "sql"],
                },
            ],
        }
        mock_path = registry._cache_dir / "mock_index.json"
        mock_path.write_text(json.dumps(mock_index))

        # Override the URL to use our local file
        import urllib.request
        file_url = mock_path.as_uri()
        ok = registry.refresh_index(index_url=file_url)
        assert ok

        plugins = registry.list_available()
        assert len(plugins) == 2
        assert plugins[0].name in ("code-formatter", "sql-linter")

    def test_search_finds_by_name(self, registry):
        """Search finds plugins by name."""
        # Manually inject a plugin into the index
        registry._index["test-plugin"] = PluginMeta(
            name="test-plugin", description="A testing plugin",
            tags=["test"],
        )
        results = registry.search("test-plugin")
        assert len(results) == 1
        assert results[0].name == "test-plugin"

    def test_search_finds_by_tag(self, registry):
        """Search finds plugins by tag."""
        registry._index["linter"] = PluginMeta(
            name="linter", description="Code linter",
            tags=["linter", "quality"],
        )
        results = registry.search("linter")
        assert len(results) == 1
        assert results[0].name == "linter"

    def test_list_available_by_tag(self, registry):
        """list_available filters by tag."""
        registry._index["a"] = PluginMeta(name="a", tags=["tool"])
        registry._index["b"] = PluginMeta(name="b", tags=["skill"])
        tools = registry.list_available(tag="tool")
        assert len(tools) == 1
        assert tools[0].name == "a"

    def test_stats(self, registry):
        """stats returns expected keys."""
        s = registry.stats()
        assert "available_plugins" in s
        assert "installed_plugins" in s
        assert "cache_dir" in s


class TestGlobalSingleton:
    """get_plugin_registry singleton behavior."""

    def test_get_plugin_registry_returns_same(self):
        """Should return the same instance."""
        r1 = get_plugin_registry()
        r2 = get_plugin_registry()
        assert r1 is r2
