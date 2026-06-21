# -*- coding: utf-8 -*-
"""
Extension API — unified plugin system.

Provides:
- Extension base class: lifecycle hooks (load / unload / activate / deactivate)
- ExtensionManager: extension discovery, registration, activation, unloading
- @extension decorator: declarative registration
- ExtensionPoint: marker class for defining extension points

Usage example:

    from .extension import Extension, extension

    @extension(name="my-skill", version="1.0.0",
               description="A custom skill extension")
    class MySkill(Extension):
        def on_activate(self):
            print("Skill activated!")

        def get_prompt(self) -> str:
            return "You are an expert in..."

    # Register with the global manager
    from .extension import get_extension_manager
    get_extension_manager().register(MySkill())
"""

import logging
import sys
import threading
from abc import ABC

from .extension_point import ExtensionPoint  # extracted to own module (v2.5.6+)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Extension metadata
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ExtensionMeta:
    """Metadata describing an extension."""
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = ""
    dependencies: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    priority: int = 100  # lower = higher priority

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "license": self.license,
            "dependencies": self.dependencies,
            "tags": self.tags,
            "priority": self.priority,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Extension type enum
# ═══════════════════════════════════════════════════════════════════════════════

class ExtensionType:
    """Well-known extension types."""
    SKILL = "skill"
    MCP = "mcp"
    TEMPLATE = "template"
    TOOL = "tool"
    MIDDLEWARE = "middleware"
    CUSTOM = "custom"


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionPoint — marker for extensible locations
# ═══════════════════════════════════════════════════════════════════════════════

# ExtensionPoint → extension_point.py (extracted v2.5.6+)


# ═══════════════════════════════════════════════════════════════════════════════
# Extension base class
# ═══════════════════════════════════════════════════════════════════════════════

class Extension(ABC):
    """
    Extension base class. Base class for all ATA Coder extensions.

    Lifecycle:
        1. __init__()          — instantiation
        2. on_load(manager)    — called when loaded by the manager
        3. on_activate()       — called when activated
        4. on_deactivate()     — called when deactivated
        5. on_unload()         — called when unloaded

    Subclasses MUST set:
        - meta: ExtensionMeta

    Subclasses MAY override:
        - on_load() / on_unload() / on_activate() / on_deactivate()
        - get_tools() → list of tool definitions
        - get_prompt() → system prompt string
        - validate() → verify extension is usable
    """

    meta: ExtensionMeta

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "meta"):
            cls.meta = ExtensionMeta(name=cls.__name__)

    def on_load(self, manager: "ExtensionManager") -> None:
        """Extension was loaded into a manager."""

    def on_unload(self) -> None:
        """Extension is being unloaded."""

    def on_activate(self) -> None:
        """Extension was activated."""

    def on_deactivate(self) -> None:
        """Extension was deactivated."""

    def get_tools(self) -> list[dict[str, Any]]:
        """Return the tool definitions provided by this extension."""
        return []

    def get_prompt(self) -> str:
        """Return the system prompt fragment provided by this extension."""
        return ""

    def get_middleware(self) -> list[Callable]:
        """Return the middleware list provided by this extension."""
        return []

    def validate(self) -> tuple[bool, str]:
        """
        验证扩展是否可用。

        Returns:
            (ok, reason) — ok=True 表示通过, reason 为描述。
        """
        return True, "OK"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.meta.name!r}, v{self.meta.version})"


# ═══════════════════════════════════════════════════════════════════════════════
# Extension manager
# ═══════════════════════════════════════════════════════════════════════════════

class ExtensionManager:
    """
    Extension manager — discovers, loads, activates, and manages extensions.

    Usage:

        mgr = ExtensionManager()
        mgr.discover("./extensions/")
        mgr.activate("my-skill")
    """

    def __init__(self):
        self._extensions: dict[str, Extension] = {}
        self._active: set[str] = set()
        self._activating: set[str] = set()  # cycle detection stack
        self._loaded_dirs: list[Path] = []
        self._lock = threading.Lock()  # protects _extensions, _active, _loaded_dirs

        # ── 扩展点注册表 ────────────────────────────────────────────────
        self._extension_points: dict[str, ExtensionPoint] = {}
        self._ep_lock = threading.Lock()  # protects _extension_points

    # ── Extension point management ──────────────────────────────────────────

    def extension_point(self, name: str, description: str = "") -> ExtensionPoint:
        """
        获取或创建一个命名扩展点（线程安全）。

        扩展点提供了类型安全的钩子系统。一个模块定义扩展点,
        多个扩展可以向其注册处理器。
        """
        with self._ep_lock:
            if name not in self._extension_points:
                self._extension_points[name] = ExtensionPoint(name, description)
            return self._extension_points[name]

    def list_extension_points(self) -> dict[str, ExtensionPoint]:
        """列出所有已注册的扩展点（线程安全）。"""
        with self._ep_lock:
            return dict(self._extension_points)

    # ── Registration ────────────────────────────────────────────────────────

    def register(self, extension: Extension) -> bool:
        """
        注册一个扩展（线程安全）。

        Returns:
            True 如果成功, False 如果同名扩展已存在。
        """
        name = extension.meta.name

        # Validate BEFORE acquiring lock (may be slow)
        ok, reason = extension.validate()
        if not ok:
            logger.error("Extension %r validation failed: %s", name, reason)
            return False

        # Insert under lock, but call on_load outside to prevent deadlock
        with self._lock:
            if name in self._extensions:
                logger.debug("Extension %r already registered, skipping", name)
                return False
            self._extensions[name] = extension

        # on_load 在锁外调用，避免回调中 register/activate 导致死锁
        try:
            extension.on_load(self)
        except Exception:
            logger.exception("Extension %r on_load failed", name)

        logger.debug("Extension registered: %s v%s", name, extension.meta.version)
        return True

    def unregister(self, name: str) -> bool:
        """注销一个扩展（线程安全）。"""
        with self._lock:
            ext = self._extensions.pop(name, None)
        if ext is None:
            return False

        with self._lock:
            was_active = name in self._active
            if was_active:
                self._active.discard(name)

        if was_active:
            try:
                ext.on_deactivate()
            except Exception:
                logger.exception("Extension %r on_deactivate failed", name)

        try:
            ext.on_unload()
        except Exception:
            logger.exception("Extension %r on_unload failed", name)

        logger.info("Extension unregistered: %s", name)
        return True

    # ── Activation ──────────────────────────────────────────────────────────

    def activate(self, name: str) -> bool:
        """激活一个扩展（线程安全）。"""
        with self._lock:
            ext = self._extensions.get(name)
            if ext is None:
                logger.debug("Extension not found: %r", name)
                return False
            if name in self._active:
                return True  # already active
            # Cycle detection — detect circular dependencies
            if name in self._activating:
                logger.error(
                    "Circular dependency detected: %s is already being activated. "
                    "Active path: %s",
                    name, ", ".join(self._activating),
                )
                return False
            self._activating.add(name)
            deps = list(ext.meta.dependencies)
            # Snapshot _active while still under the outer lock.
            # DO NOT re-acquire self._lock here — threading.Lock()
            # is NOT reentrant and would deadlock.
            active_snapshot = set(self._active)

        try:
            # Activate dependencies (try raw name first, then skill: prefix)
            for dep in deps:
                if dep not in active_snapshot:
                    if not self.activate(dep):
                        self.activate(f"skill:{dep}")

            # on_activate 在锁外调用，避免死锁
            try:
                ext.on_activate()
            except Exception:
                logger.exception("Extension %r on_activate failed", name)
                return False

            with self._lock:
                self._active.add(name)
            logger.debug("Extension activated: %s", name)
            return True
        finally:
            with self._lock:
                self._activating.discard(name)

    def deactivate(self, name: str) -> bool:
        """停用一个扩展（线程安全）。"""
        with self._lock:
            ext = self._extensions.get(name)
            if ext is None or name not in self._active:
                return False

            # 检查是否有其他扩展依赖此扩展
            for other_name, other_ext in self._extensions.items():
                if other_name != name and name in other_ext.meta.dependencies:
                    if other_name in self._active:
                        logger.warning(
                            "Cannot deactivate %r: required by %r", name, other_name
                        )
                        return False

            self._active.discard(name)

        # on_deactivate 在锁外调用
        try:
            ext.on_deactivate()
        except Exception:
            logger.exception("Extension %r on_deactivate failed", name)

        logger.info("Extension deactivated: %s", name)
        return True

    # ── Discovery ───────────────────────────────────────────────────────────

    def discover(self, directory: str | Path) -> list[str]:
        """
        从目录中发现扩展。

        扫描 directory 下的 Python 文件, 查找 @extension 装饰的类。
        跳过以 _ 或 . 开头的文件。

        Returns:
            成功加载的扩展名称列表。
        """
        directory = Path(directory)
        if not directory.exists():
            logger.warning("Extension directory not found: %s", directory)
            return []

        loaded: list[str] = []
        self._loaded_dirs.append(directory)

        for fp in sorted(directory.glob("*.py")):
            if fp.name.startswith("_") or fp.name.startswith("."):
                continue
            try:
                names = self._load_module(fp)
                loaded.extend(names)
            except Exception:
                logger.exception("Failed to load extension module: %s", fp)

        return loaded

    def _load_module(self, path: Path) -> list[str]:
        """从单个 .py 文件加载扩展类。"""
        import importlib.util

        module_name = path.stem
        spec = importlib.util.spec_from_file_location(
            f"ata_coder_ext_{module_name}", str(path)
        )
        if spec is None or spec.loader is None:
            return []

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        # 查找 Extension 子类
        loaded: list[str] = []
        for attr_name in dir(module):
            obj = getattr(module, attr_name)
            if (
                isinstance(obj, type)
                and issubclass(obj, Extension)
                and obj is not Extension
                and hasattr(obj, "meta")
            ):
                try:
                    instance = obj()
                    if self.register(instance):
                        loaded.append(instance.meta.name)
                except Exception:
                    logger.exception("Failed to instantiate %s", attr_name)

        return loaded

    # ── Queries ─────────────────────────────────────────────────────────────

    def get_extension(self, name: str) -> Extension | None:
        """按名称获取扩展（线程安全）。"""
        with self._lock:
            return self._extensions.get(name)

    def list_extensions(self) -> list[Extension]:
        """列出所有已注册的扩展（线程安全，返回快照）。"""
        with self._lock:
            return list(self._extensions.values())

    def list_active(self) -> list[Extension]:
        """列出所有已激活的扩展（线程安全，返回快照）。"""
        with self._lock:
            return [self._extensions[n] for n in self._active if n in self._extensions]

    def get_by_type(self, ext_type: str) -> list[Extension]:
        """按标签获取扩展（线程安全）。"""
        with self._lock:
            return [
                ext for ext in self._extensions.values()
                if ext_type in ext.meta.tags
            ]

    def get_by_tag(self, tag: str) -> list[Extension]:
        """按标签获取扩展（线程安全）。"""
        return self.get_by_type(tag)

    # ── System prompt aggregation ──────────────────────────────────────────

    def aggregate_prompts(self, base_prompt: str = "") -> str:
        """
        聚合所有激活扩展的提示片段（线程安全）。

        按 priority 排序后拼接。
        """
        with self._lock:
            active = sorted(
                [self._extensions[n] for n in self._active if n in self._extensions],
                key=lambda e: e.meta.priority,
            )
        parts = [base_prompt] if base_prompt else []
        for ext in active:
            prompt = ext.get_prompt()
            if prompt:
                parts.append(prompt)
        return "\n\n".join(parts)

    # ── Tools aggregation ──────────────────────────────────────────────────

    def aggregate_tools(self) -> list[dict[str, Any]]:
        """聚合所有激活扩展的工具定义（线程安全）。

        跳过来自 SkillExtension 的纯字符串（那是工具限制列表，
        不是完整定义），只聚合完整的 OpenAI 格式工具 dict。
        """
        with self._lock:
            active = [self._extensions[n] for n in self._active if n in self._extensions]
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        for ext in active:
            for tool in ext.get_tools():
                # SkillExtension returns str names; real extensions return dicts
                if isinstance(tool, str):
                    continue
                name = tool.get("function", {}).get("name", "")
                if name and name not in seen:
                    seen.add(name)
                    tools.append(tool)
        return tools

    def get_tool_names(self) -> set[str]:
        """获取所有激活扩展的工具名称集合（线程安全）。"""
        tools = self.aggregate_tools()
        return {
            t.get("function", {}).get("name", "")
            for t in tools
            if isinstance(t, dict) and t.get("function", {}).get("name")
        }

    # ── Stats ───────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """返回扩展系统的统计信息（线程安全）。"""
        with self._lock:
            result = {
                "total": len(self._extensions),
                "active": len(self._active),
                "loaded_dirs": len(self._loaded_dirs),
                "by_status": {
                    name: "active" if name in self._active else "loaded"
                    for name in self._extensions
                },
            }
        with self._ep_lock:
            result["extension_points"] = len(self._extension_points)
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# @extension decorator
# ═══════════════════════════════════════════════════════════════════════════════

def extension(
    name: str,
    version: str = "0.1.0",
    description: str = "",
    author: str = "",
    homepage: str = "",
    license: str = "",
    dependencies: list[str] | None = None,
    tags: list[str] | None = None,
    priority: int = 100,
    **kwargs: Any,
) -> Callable:
    """
    Class decorator — declare an extension.

    Usage:

        @extension(name="my-skill", version="1.0.0",
                   tags=["skill"], priority=10)
        class MySkill(Extension):
            def get_prompt(self):
                return "You are an expert..."

    Args:
        name: 扩展唯一名称 (kebab-case 推荐)
        version: 语义化版本号
        description: 简要描述
        author: 作者
        homepage: 项目主页
        license: 许可证
        dependencies: 依赖的其他扩展名称列表
        tags: 标签 (如 "skill", "mcp", "template", "tool")
        priority: 优先级 (越小越高, 默认 100)
    """

    def decorator(cls: type) -> type:
        meta = ExtensionMeta(
            name=name,
            version=version,
            description=description,
            author=author,
            homepage=homepage,
            license=license,
            dependencies=dependencies or [],
            tags=tags or [],
            priority=priority,
        )
        setattr(cls, "meta", meta)

        # 如果 kwargs 中有额外元数据, 存入 meta 的属性
        for k, v in kwargs.items():
            if not hasattr(meta, k):
                setattr(meta, k, v)

        return cls

    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# Global manager singleton
# ═══════════════════════════════════════════════════════════════════════════════

_extension_manager: ExtensionManager | None = None


def get_extension_manager() -> ExtensionManager:
    """Get the global ExtensionManager singleton."""
    global _extension_manager
    if _extension_manager is None:
        _extension_manager = ExtensionManager()
    return _extension_manager


def reset_extension_manager() -> None:
    """Reset the global extension manager (mainly for testing)."""
    global _extension_manager
    _extension_manager = None
