"""
Community plugin registry for ATA Coder.

Extends the Extension system with pip-installable plugin support:
- Plugin discovery from PyPI (packages matching ``ata-coder-*``)
- Plugin discovery from GitHub (repos tagged ``ata-coder-plugin``)
- Fallback to community index JSON
- pip-based installation with dependency resolution
- Plugin metadata management (version, author, compatibility)
- Listing, searching, and removal of installed plugins

Plugins are standard Python packages that expose an Extension subclass
via the ``ata_coder.plugins`` entry point.  This is compatible with the
existing ``Extension`` / ``ExtensionManager`` infrastructure.

Usage:
    from .plugin_registry import PluginRegistry

    registry = PluginRegistry()
    registry.refresh_index()          # search PyPI + GitHub for plugins
    registry.install("code-formatter") # pip install + register extension
    registry.list_installed()          # show what's installed
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default community plugin index URL (GitHub raw JSON) — used as fallback
DEFAULT_INDEX_URL = (
    "https://raw.githubusercontent.com/jiaheng0815/ata-coder-plugins/main/index.json"
)

# Local cache TTL for the plugin index (seconds)
INDEX_CACHE_TTL = 3600  # 1 hour

# PyPI simple API endpoint
PYPI_SIMPLE_URL = "https://pypi.org/simple/"

# GitHub API search endpoint (no auth needed for public repos, rate limit ~60/hr)
GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"


# ── Plugin metadata ───────────────────────────────────────────────────────────

@dataclass
class PluginMeta:
    """Metadata for a community plugin (extends ExtensionMeta)."""
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    package_name: str = ""       # pip package name (e.g. "ata-coder-plugin-fmt")
    entry_point: str = ""        # full entry point path (e.g. "ata_coder_plugin_fmt:export_extension")
    license: str = "MIT"
    tags: list[str] = field(default_factory=list)
    min_ata_version: str = "2.5.0"
    installed: bool = False
    installed_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "version": self.version,
            "description": self.description, "author": self.author,
            "homepage": self.homepage, "package_name": self.package_name,
            "entry_point": self.entry_point, "license": self.license,
            "tags": self.tags, "min_ata_version": self.min_ata_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PluginMeta":
        return cls(
            name=d.get("name", ""), version=d.get("version", "0.1.0"),
            description=d.get("description", ""), author=d.get("author", ""),
            homepage=d.get("homepage", ""), package_name=d.get("package_name", ""),
            entry_point=d.get("entry_point", ""), license=d.get("license", "MIT"),
            tags=d.get("tags", []), min_ata_version=d.get("min_ata_version", "2.5.0"),
        )


# ── Plugin registry ──────────────────────────────────────────────────────────

class PluginRegistry:
    """Community plugin registry — discover, install, manage plugins.

    Caches the community index locally in ~/.ata_coder/plugins/index.json.
    Installation uses pip to install the plugin package, then registers
    the Extension via the entry point.
    """

    def __init__(self, cache_dir: str | Path | None = None):
        if cache_dir is None:
            try:
                from .settings import get_settings
                cache_dir = Path(get_settings().data_dir) / "plugins"
            except Exception:
                cache_dir = Path.home() / ".ata_coder" / "plugins"
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._cache_dir / "index.json"
        self._index: dict[str, PluginMeta] = {}
        self._lock = threading.Lock()

        # Load cached index on init
        self._load_cached_index()

    # ── Index management ──────────────────────────────────────────────────

    def _load_cached_index(self) -> None:
        """Load the cached plugin index from disk."""
        if not self._index_path.exists():
            return
        try:
            with open(self._index_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for entry in data.get("plugins", []):
                    meta = PluginMeta.from_dict(entry)
                    self._index[meta.name] = meta
            logger.debug("Loaded %d plugins from cache", len(self._index))
        except Exception as e:
            logger.debug("Failed to load plugin index cache: %s", e)

    def refresh_index(self, index_url: str = "") -> bool:
        """Fetch available plugins from real platforms.

        Search order:
        1. PyPI — scan for packages matching ``ata-coder-*``
        2. GitHub — search repos tagged ``ata-coder-plugin``
        3. Community index — fall back to the static JSON URL

        When *index_url* is explicitly provided, PyPI and GitHub are
        skipped and only that URL is fetched — this avoids unnecessary
        network calls when the caller already knows the source.

        Results are merged and cached locally.  Returns True if at least
        one source succeeded.
        """
        all_plugins: dict[str, PluginMeta] = {}
        succeeded = 0

        # Explicit index_url → skip PyPI/GitHub, fetch directly
        if index_url:
            community_plugins = self._fetch_community_index(index_url)
            if community_plugins:
                all_plugins.update(community_plugins)
                succeeded += 1
                logger.info("Community index (explicit): %d plugins", len(community_plugins))
        else:
            # ── 1. PyPI ────────────────────────────────────────────────────
            pypi_plugins = self._fetch_pypi_plugins()
            if pypi_plugins:
                all_plugins.update(pypi_plugins)
                succeeded += 1
                logger.info("PyPI: %d plugins found", len(pypi_plugins))

            # ── 2. GitHub ──────────────────────────────────────────────────
            github_plugins = self._fetch_github_plugins()
            if github_plugins:
                # Merge: GitHub metadata complements PyPI
                for name, meta in github_plugins.items():
                    if name not in all_plugins:
                        all_plugins[name] = meta
                    else:
                        # Enrich with GitHub data where PyPI is sparse
                        existing = all_plugins[name]
                        if not existing.homepage and meta.homepage:
                            existing.homepage = meta.homepage
                        if not existing.description and meta.description:
                            existing.description = meta.description
                        for tag in meta.tags:
                            if tag not in existing.tags:
                                existing.tags.append(tag)
                succeeded += 1
                logger.info("GitHub: %d plugins found", len(github_plugins))

            # ── 3. Community index (fallback) ──────────────────────────────
            community_plugins = self._fetch_community_index(index_url)
            if community_plugins:
                for name, meta in community_plugins.items():
                    if name not in all_plugins:
                        all_plugins[name] = meta
                if not succeeded:
                    succeeded += 1
                logger.info("Community index: %d plugins", len(community_plugins))

        # ── Save ───────────────────────────────────────────────────────
        if all_plugins:
            self._save_index(all_plugins)
            with self._lock:
                self._index = all_plugins
            logger.info("Plugin index refreshed: %d total from %d sources",
                        len(all_plugins), succeeded)
            return True

        if not succeeded:
            logger.warning("All plugin sources failed. Check network.")
        return succeeded > 0

    # ── PyPI connector ────────────────────────────────────────────────────

    def _fetch_pypi_plugins(self) -> dict[str, PluginMeta]:
        """Search PyPI for packages with ``ata-coder`` in the name.

        Uses the free PyPI Simple API (HTML page listing all packages).
        Parses the page for package names matching the pattern, then
        fetches metadata for each from the PyPI JSON API.
        """
        import urllib.request

        plugins: dict[str, PluginMeta] = {}
        try:
            # Step 1: get the simple index and find candidate packages
            req = urllib.request.Request(
                PYPI_SIMPLE_URL, headers={"User-Agent": "ata-coder", "Accept": "application/vnd.pypi.simple.v1+json"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode("utf-8")

            # PyPI simple API returns either JSON or HTML. Parse accordingly.
            # Prefer JSON (PEP 691: application/vnd.pypi.simple.v1+json)
            ct = resp.headers.get("Content-Type", "")
            if ct.startswith("application/json") or ct.startswith("application/vnd.pypi.simple"):
                data = json.loads(text)
                projects = data.get("projects", [])
                candidates = [
                    p.get("name", "") for p in projects
                    if "ata-coder" in p.get("name", "").lower()
                ]
            else:
                # HTML fallback: <a href="...">package_name</a>
                candidates = re.findall(
                    r'<a[^>]*>([a-zA-Z0-9._-]*ata-coder[a-zA-Z0-9._-]*)</a>',
                    text, re.IGNORECASE,
                )

            if not candidates:
                logger.debug("PyPI: no ata-coder packages found")
                return {}

            # Step 2: fetch metadata for each candidate (cap at 20)
            for pkg_name in candidates[:20]:
                try:
                    info = self._fetch_pypi_package_info(pkg_name)
                    if info:
                        plugin_name = self._pkg_to_plugin_name(pkg_name)
                        plugins[plugin_name] = PluginMeta(
                            name=plugin_name,
                            version=info.get("version", "0.1.0"),
                            description=(info.get("summary") or "")[:120],
                            author=info.get("author") or "",
                            homepage=info.get("home_page") or info.get("project_url") or "",
                            package_name=pkg_name,
                            entry_point=self._guess_entry_point(pkg_name, info),
                            license=info.get("license") or "MIT",
                            tags=self._extract_tags(info),
                            min_ata_version="2.5.0",
                        )
                except Exception:
                    logger.debug("PyPI: failed to fetch info for %s", pkg_name)
        except Exception as e:
            logger.warning("PyPI search failed: %s", e)

        return plugins

    def _fetch_pypi_package_info(self, package_name: str) -> dict[str, Any] | None:
        """Fetch package metadata from PyPI JSON API."""
        import urllib.request
        url = f"https://pypi.org/pypi/{package_name}/json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ata-coder"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            info = data.get("info", {})
            # Get latest version
            latest_ver = info.get("version", "")
            return {
                "name": info.get("name", package_name),
                "version": latest_ver,
                "summary": info.get("summary", ""),
                "author": info.get("author") or info.get("author_email", ""),
                "home_page": (
                    info.get("home_page", "")
                    or info.get("project_urls", {}).get("Homepage", "")
                    or info.get("project_urls", {}).get("Source", "")
                    or info.get("package_url", "")
                ),
                "license": info.get("license", ""),
                "keywords": info.get("keywords", ""),
                "classifiers": info.get("classifiers", []),
                "requires_python": info.get("requires_python", ""),
                "entry_points": self._extract_pypi_entry_points(data),
            }
        except Exception:
            return None

    def _extract_pypi_entry_points(self, pypi_data: dict) -> str:
        """Extract the ata_coder.plugins entry point from PyPI metadata."""
        try:
            # Check PEP 621 / setuptools entry points
            eps = pypi_data.get("info", {}).get("entry_points", {})
            if isinstance(eps, dict):
                ata_eps = eps.get("ata_coder.plugins", {})
                if isinstance(ata_eps, dict) and ata_eps:
                    return list(ata_eps.values())[0]
        except Exception:
            pass
        return ""

    @staticmethod
    def _pkg_to_plugin_name(pkg_name: str) -> str:
        """Convert pip package name to plugin display name."""
        # ata-coder-plugin-fmt → fmt
        name = pkg_name.lower()
        for prefix in ("ata-coder-plugin-", "ata-coder-", "ata_coder_plugin_", "ata_coder_"):
            if name.startswith(prefix):
                return name[len(prefix):]
        return name.replace("-", " ").replace("_", " ")

    @staticmethod
    def _guess_entry_point(pkg_name: str, info: dict) -> str:
        """Guess the entry point if not explicitly declared."""
        # Check explicit entry points first
        eps = info.get("entry_points", "")
        if eps and ":" in str(eps):
            return str(eps)

        # Guess from package name
        # ata-coder-plugin-fmt → ata_coder_plugin_fmt:export_extension
        mod_name = pkg_name.replace("-", "_")
        return f"{mod_name}:export_extension"

    @staticmethod
    def _extract_tags(info: dict) -> list[str]:
        """Extract tags from PyPI keywords and classifiers."""
        tags: list[str] = []
        keywords = info.get("keywords", "")
        if keywords:
            tags.extend(k.strip() for k in str(keywords).split(",") if k.strip())

        classifiers = info.get("classifiers", [])
        topic_map = {
            "Testing": "testing", "Linter": "linter", "Formatter": "formatter",
            "Security": "security", "Documentation": "documentation",
            "Framework": "framework", "Tool": "tool", "Skill": "skill",
        }
        for c in classifiers:
            for prefix, tag in topic_map.items():
                if prefix in c and tag not in tags:
                    tags.append(tag)
        return tags[:5]

    # ── GitHub connector ───────────────────────────────────────────────────

    def _fetch_github_plugins(self) -> dict[str, PluginMeta]:
        """Search GitHub for repositories tagged ``ata-coder-plugin``."""
        import urllib.request

        plugins: dict[str, PluginMeta] = {}
        try:
            url = f"{GITHUB_SEARCH_URL}?q=topic:ata-coder-plugin&sort=updated&per_page=20"
            req = urllib.request.Request(url, headers={
                "User-Agent": "ata-coder",
                "Accept": "application/vnd.github.v3+json",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            for item in data.get("items", [])[:20]:
                try:
                    name = item.get("name", "")
                    plugin_name = self._pkg_to_plugin_name(name)
                    description = (item.get("description") or "")[:120]
                    topics = item.get("topics", [])
                    tags = [t for t in topics if t != "ata-coder-plugin"]

                    plugins[plugin_name] = PluginMeta(
                        name=plugin_name,
                        version="latest",
                        description=description,
                        author=item.get("owner", {}).get("login", ""),
                        homepage=item.get("html_url", ""),
                        package_name=name,
                        entry_point=f"{name.replace('-', '_')}:export_extension",
                        license=item.get("license", {}).get("spdx_id", "MIT") if item.get("license") else "MIT",
                        tags=tags,
                    )
                except Exception:
                    logger.debug("GitHub: failed to parse repo %s", item.get("name", "?"))
        except Exception as e:
            logger.warning("GitHub search failed: %s", e)

        return plugins

    # ── Community index fallback ───────────────────────────────────────────

    def _fetch_community_index(self, index_url: str = "") -> dict[str, PluginMeta]:
        """Fetch plugins from the community JSON index (fallback)."""
        import urllib.request

        url = index_url or DEFAULT_INDEX_URL
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ata-coder"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            plugins: dict[str, PluginMeta] = {}
            for entry in data.get("plugins", []):
                meta = PluginMeta.from_dict(entry)
                plugins[meta.name] = meta
            return plugins
        except Exception as e:
            logger.debug("Community index unavailable: %s", e)
            return {}

    def _save_index(self, plugins: dict[str, PluginMeta]) -> None:
        """Write the plugin index to disk atomically."""
        tmp = self._index_path.with_suffix(".tmp")
        try:
            data = {
                "version": "1.0",
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "plugins": [p.to_dict() for p in plugins.values()],
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            import os
            os.replace(tmp, self._index_path)
        except Exception as e:
            logger.debug("Failed to save plugin index: %s", e)

    # ── Queries ───────────────────────────────────────────────────────────

    def list_available(self, tag: str = "") -> list[PluginMeta]:
        """List all available community plugins, optionally filtered by tag."""
        with self._lock:
            plugins = list(self._index.values())
        if tag:
            plugins = [p for p in plugins if tag in p.tags]
        return sorted(plugins, key=lambda p: p.name)

    def search(self, query: str) -> list[PluginMeta]:
        """Search community plugins by name or description."""
        q = query.lower()
        with self._lock:
            results = [
                p for p in self._index.values()
                if q in p.name.lower() or q in p.description.lower()
                or any(q in t.lower() for t in p.tags)
            ]
        return sorted(results, key=lambda p: p.name)

    def get(self, name: str) -> PluginMeta | None:
        """Get a plugin's metadata by name."""
        with self._lock:
            return self._index.get(name)

    # ── Installation ──────────────────────────────────────────────────────

    def install(self, name: str, upgrade: bool = False) -> tuple[bool, str]:
        """Install a community plugin via pip.

        Args:
            name: Plugin name (must be in the index).
            upgrade: If True, use pip install --upgrade.

        Returns:
            (success, message) — message contains the outcome or error.
        """
        meta = self.get(name)
        if meta is None:
            return False, f"Plugin '{name}' not found in the community index. Try /plugin refresh first."

        pkg = meta.package_name or name
        cmd = [sys.executable, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.append(pkg)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                meta.installed = True
                # Try to detect installed version
                try:
                    import importlib.metadata
                    meta.installed_version = importlib.metadata.version(pkg)
                except Exception:
                    meta.installed_version = meta.version

                # Register the extension with the global manager
                if meta.entry_point:
                    self._register_entry_point(meta)

                msg = (
                    f"Installed {meta.name} v{meta.installed_version}\n"
                    f"  {meta.description[:80]}"
                )
                logger.info("Plugin installed: %s", meta.name)
                return True, msg
            error = result.stderr.strip().split("\n")[-1] if result.stderr else "Unknown error"
            return False, f"pip install failed: {error}"
        except subprocess.TimeoutExpired:
            return False, "Installation timed out after 120s."
        except Exception as e:
            return False, f"Installation error: {e}"

    def _register_entry_point(self, meta: PluginMeta) -> bool:
        """Load and register a plugin's Extension from its entry point."""
        try:
            # Entry point format: "module.path:function_name"
            if ":" not in meta.entry_point:
                logger.debug("Invalid entry point format: %s", meta.entry_point)
                return False
            mod_path, func_name = meta.entry_point.rsplit(":", 1)
            import importlib
            module = importlib.import_module(mod_path)
            factory = getattr(module, func_name, None)
            if factory is None:
                logger.debug("Entry point function not found: %s", func_name)
                return False

            from .extension import get_extension_manager
            ext = factory()
            mgr = get_extension_manager()
            return mgr.register(ext)
        except Exception:
            logger.exception("Failed to register plugin extension: %s", meta.name)
            return False

    def uninstall(self, name: str) -> tuple[bool, str]:
        """Uninstall a plugin via pip."""
        meta = self.get(name)
        pkg = meta.package_name if meta and meta.package_name else name

        # Deactivate and unregister from extension manager first
        if meta and meta.entry_point:
            try:
                from .extension import get_extension_manager
                mgr = get_extension_manager()
                mgr.deactivate(meta.name)
                mgr.unregister(meta.name)
            except Exception:
                pass

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", pkg],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                if meta:
                    meta.installed = False
                    meta.installed_version = ""
                return True, f"Uninstalled {name}"
            return False, f"pip uninstall failed: {result.stderr.strip()[-200:]}"
        except Exception as e:
            return False, f"Uninstall error: {e}"

    def list_installed(self) -> list[dict[str, str]]:
        """List installed plugins with their versions."""
        result: list[dict[str, str]] = []
        with self._lock:
            for meta in self._index.values():
                # Check if the package is actually installed
                try:
                    import importlib.metadata
                    ver = importlib.metadata.version(meta.package_name or meta.name)
                    result.append({
                        "name": meta.name,
                        "version": ver,
                        "description": meta.description[:80],
                        "author": meta.author,
                    })
                except importlib.metadata.PackageNotFoundError:
                    continue
        return sorted(result, key=lambda p: p["name"])

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Return registry statistics."""
        with self._lock:
            available = len(self._index)
        installed = len(self.list_installed())
        return {
            "available_plugins": available,
            "installed_plugins": installed,
            "cache_dir": str(self._cache_dir),
            "index_age": self._index_age(),
        }

    def _index_age(self) -> str:
        """Human-readable age of the cached index."""
        if not self._index_path.exists():
            return "never cached"
        mtime = self._index_path.stat().st_mtime
        ago = time.time() - mtime
        if ago < 60:
            return f"{int(ago)}s ago"
        if ago < 3600:
            return f"{int(ago / 60)}m ago"
        if ago < 86400:
            return f"{int(ago / 3600)}h ago"
        return f"{int(ago / 86400)}d ago"


# ── Global singleton ──────────────────────────────────────────────────────────

_registry: PluginRegistry | None = None


def get_plugin_registry() -> PluginRegistry:
    """Get the global PluginRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry
