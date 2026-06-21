# -*- coding: utf-8 -*-
"""Tests for the extension API."""

from ata_coder.extension import (
    Extension,
    ExtensionManager,
    ExtensionMeta,
    ExtensionPoint,
    ExtensionType,
    extension,
    get_extension_manager,
    reset_extension_manager,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

class _SimpleExt(Extension):
    meta = ExtensionMeta(name="simple", version="1.0.0", description="A simple test extension")


class _PromptExt(Extension):
    meta = ExtensionMeta(name="prompt-ext", version="1.0.0")

    def get_prompt(self) -> str:
        return "You are helpful."


class _ToolExt(Extension):
    meta = ExtensionMeta(name="tool-ext", version="1.0.0")

    def get_tools(self) -> list[dict]:
        return [{
            "type": "function",
            "function": {"name": "hello", "description": "Say hello", "parameters": {}},
        }]


class _LifecycleExt(Extension):
    meta = ExtensionMeta(name="lifecycle-ext", version="1.0.0")

    def __init__(self):
        super().__init__()
        self.events: list[str] = []

    def on_load(self, manager):
        self.events.append("load")

    def on_activate(self):
        self.events.append("activate")

    def on_deactivate(self):
        self.events.append("deactivate")

    def on_unload(self):
        self.events.append("unload")


class _FailingExt(Extension):
    meta = ExtensionMeta(name="failing-ext", version="1.0.0")

    def validate(self) -> tuple[bool, str]:
        return False, "Not ready"


class _DepExt(Extension):
    meta = ExtensionMeta(name="dep-ext", version="1.0.0",
                         dependencies=["lifecycle-ext"])


@extension(name="decorated-ext", version="2.0.0",
           description="Created via decorator",
           tags=["test", "decorator"], priority=50)
class _DecoratedExt(Extension):
    def get_prompt(self) -> str:
        return "Decorated prompt"


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionMeta
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionMeta:
    def test_default_values(self):
        meta = ExtensionMeta(name="test")
        assert meta.name == "test"
        assert meta.version == "0.1.0"
        assert meta.description == ""
        assert meta.priority == 100

    def test_to_dict(self):
        meta = ExtensionMeta(name="test", version="1.0", tags=["a", "b"])
        d = meta.to_dict()
        assert d["name"] == "test"
        assert d["version"] == "1.0"
        assert d["tags"] == ["a", "b"]


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionPoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionPoint:
    def test_register_and_trigger(self):
        ep = ExtensionPoint("test")
        results = []

        def handler(x):
            results.append(x)
            return x

        ep.register(handler)
        ep.trigger(42)

        assert results == [42]

    def test_multiple_handlers(self):
        ep = ExtensionPoint("test")

        ep.register(lambda: "a")
        ep.register(lambda: "b")

        assert ep.trigger() == ["a", "b"]

    def test_trigger_first_stops_at_first_result(self):
        ep = ExtensionPoint("test")

        ep.register(lambda: None)
        ep.register(lambda: "found")
        ep.register(lambda: "never")  # won't be called

        assert ep.trigger_first() == "found"

    def test_trigger_first_all_none(self):
        ep = ExtensionPoint("test")
        ep.register(lambda: None)
        assert ep.trigger_first() is None

    def test_unregister(self):
        ep = ExtensionPoint("test")

        def h():
            return 1

        ep.register(h)
        ep.unregister(h)
        assert ep.trigger() == []

    def test_handler_exception_is_caught(self):
        ep = ExtensionPoint("test")

        def bad():
            raise RuntimeError("boom")

        def good():
            return "ok"

        ep.register(bad)
        ep.register(good)
        results = ep.trigger()

        assert results == ["ok"]

    def test_clear(self):
        ep = ExtensionPoint("test")
        ep.register(lambda: 1)
        ep.clear()
        assert ep.trigger() == []

    def test_repr(self):
        ep = ExtensionPoint("my-point")
        assert "my-point" in repr(ep)
        assert "0" in repr(ep)


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionManager — registration
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionManagerRegistration:
    def setup_method(self):
        reset_extension_manager()
        self.mgr = ExtensionManager()

    def test_register_success(self):
        assert self.mgr.register(_SimpleExt())

    def test_list_after_register(self):
        self.mgr.register(_SimpleExt())
        assert len(self.mgr.list_extensions()) == 1
        assert self.mgr.get_extension("simple") is not None

    def test_duplicate_name_rejected(self):
        self.mgr.register(_SimpleExt())
        assert not self.mgr.register(_SimpleExt())
        assert len(self.mgr.list_extensions()) == 1

    def test_failing_validation_rejected(self):
        assert not self.mgr.register(_FailingExt())
        assert self.mgr.get_extension("failing-ext") is None

    def test_unregister(self):
        self.mgr.register(_SimpleExt())
        assert self.mgr.unregister("simple")
        assert self.mgr.get_extension("simple") is None

    def test_unregister_nonexistent(self):
        assert not self.mgr.unregister("ghost")

    def test_unregister_deactivates(self):
        ext = _LifecycleExt()
        self.mgr.register(ext)
        self.mgr.activate("lifecycle-ext")
        assert "activate" in ext.events

        self.mgr.unregister("lifecycle-ext")
        assert "deactivate" in ext.events
        assert "unload" in ext.events


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionManager — activation
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionManagerActivation:
    def setup_method(self):
        reset_extension_manager()
        self.mgr = ExtensionManager()

    def test_activate_and_deactivate(self):
        ext = _LifecycleExt()
        self.mgr.register(ext)

        assert self.mgr.activate("lifecycle-ext")
        assert "activate" in ext.events
        assert len(self.mgr.list_active()) == 1

        assert self.mgr.deactivate("lifecycle-ext")
        assert "deactivate" in ext.events
        assert len(self.mgr.list_active()) == 0

    def test_full_lifecycle_order(self):
        ext = _LifecycleExt()
        self.mgr.register(ext)  # triggers on_load
        self.mgr.activate("lifecycle-ext")
        self.mgr.deactivate("lifecycle-ext")
        self.mgr.unregister("lifecycle-ext")
        assert ext.events == ["load", "activate", "deactivate", "unload"]

    def test_activate_nonexistent(self):
        assert not self.mgr.activate("nope")

    def test_double_activate_is_idempotent(self):
        self.mgr.register(_SimpleExt())
        assert self.mgr.activate("simple")
        assert self.mgr.activate("simple")  # no-op, no error

    def test_deactivate_required_by_other_fails(self):
        lifecycle = _LifecycleExt()
        self.mgr.register(lifecycle)
        self.mgr.register(_DepExt())

        self.mgr.activate("lifecycle-ext")
        self.mgr.activate("dep-ext")

        # dep-ext depends on lifecycle-ext, should block deactivation
        assert not self.mgr.deactivate("lifecycle-ext")

    def test_activate_resolves_dependencies(self):
        lifecycle = _LifecycleExt()
        self.mgr.register(lifecycle)
        self.mgr.register(_DepExt())

        self.mgr.activate("dep-ext")
        # lifecycle-ext should have been activated as dependency
        assert "lifecycle-ext" in self.mgr.list_active()[0].meta.name or \
               any(e.meta.name == "lifecycle-ext" for e in self.mgr.list_active())


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionManager — prompt & tool aggregation
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionManagerAggregation:
    def setup_method(self):
        reset_extension_manager()
        self.mgr = ExtensionManager()

    def test_aggregate_prompts(self):
        self.mgr.register(_PromptExt())
        self.mgr.activate("prompt-ext")
        result = self.mgr.aggregate_prompts("BASE")
        assert "BASE" in result
        assert "You are helpful" in result

    def test_aggregate_prompts_no_active(self):
        self.mgr.register(_PromptExt())
        result = self.mgr.aggregate_prompts("BASE")
        assert result == "BASE"

    def test_aggregate_tools(self):
        self.mgr.register(_ToolExt())
        self.mgr.activate("tool-ext")
        tools = self.mgr.aggregate_tools()
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "hello"

    def test_aggregate_tools_no_duplicates(self):
        # Two extensions providing same tool name
        class _ToolExtB(Extension):
            meta = ExtensionMeta(name="tool-ext-b", version="1.0.0")

            def get_tools(self):
                return [{
                    "type": "function",
                    "function": {"name": "hello", "description": "Also hello", "parameters": {}},
                }]

        self.mgr.register(_ToolExt())
        self.mgr.register(_ToolExtB())
        self.mgr.activate("tool-ext")
        self.mgr.activate("tool-ext-b")
        tools = self.mgr.aggregate_tools()
        assert len(tools) == 1  # deduplicated


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionManager — queries
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionManagerQueries:
    def setup_method(self):
        reset_extension_manager()
        self.mgr = ExtensionManager()

    def test_get_by_tag(self):
        self.mgr.register(_DecoratedExt())
        found = self.mgr.get_by_tag("test")
        assert len(found) == 1
        assert found[0].meta.name == "decorated-ext"

    def test_get_by_tag_no_match(self):
        self.mgr.register(_SimpleExt())
        assert self.mgr.get_by_tag("nonexistent") == []

    def test_stats(self):
        self.mgr.register(_SimpleExt())
        self.mgr.activate("simple")
        stats = self.mgr.stats()
        assert stats["total"] == 1
        assert stats["active"] == 1

    def test_list_active_only_shows_activated(self):
        self.mgr.register(_SimpleExt())
        self.mgr.register(_PromptExt())
        self.mgr.activate("simple")
        active = self.mgr.list_active()
        assert len(active) == 1
        assert active[0].meta.name == "simple"


# ═══════════════════════════════════════════════════════════════════════════════
# ExtensionManager — extension points
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionManagerPoints:
    def setup_method(self):
        reset_extension_manager()
        self.mgr = ExtensionManager()

    def test_create_extension_point(self):
        ep = self.mgr.extension_point("on_tool_call")
        assert isinstance(ep, ExtensionPoint)
        assert ep.name == "on_tool_call"

    def test_reuse_existing_point(self):
        ep1 = self.mgr.extension_point("x")
        ep2 = self.mgr.extension_point("x")
        assert ep1 is ep2

    def test_list_extension_points(self):
        self.mgr.extension_point("a")
        self.mgr.extension_point("b")
        points = self.mgr.list_extension_points()
        assert "a" in points
        assert "b" in points


# ═══════════════════════════════════════════════════════════════════════════════
# @extension decorator
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionDecorator:
    def test_meta_is_set(self):
        assert _DecoratedExt.meta.name == "decorated-ext"
        assert _DecoratedExt.meta.version == "2.0.0"
        assert _DecoratedExt.meta.description == "Created via decorator"
        assert _DecoratedExt.meta.tags == ["test", "decorator"]
        assert _DecoratedExt.meta.priority == 50

    def test_instance_has_prompt(self):
        inst = _DecoratedExt()
        assert inst.get_prompt() == "Decorated prompt"


# ═══════════════════════════════════════════════════════════════════════════════
# Global manager singleton
# ═══════════════════════════════════════════════════════════════════════════════

class TestGlobalManager:
    def setup_method(self):
        reset_extension_manager()

    def test_get_returns_singleton(self):
        mgr1 = get_extension_manager()
        mgr2 = get_extension_manager()
        assert mgr1 is mgr2

    def test_reset_creates_new(self):
        mgr1 = get_extension_manager()
        reset_extension_manager()
        mgr2 = get_extension_manager()
        assert mgr1 is not mgr2


# ═══════════════════════════════════════════════════════════════════════════════
# Extension types
# ═══════════════════════════════════════════════════════════════════════════════

class TestExtensionType:
    def test_types_exist(self):
        assert ExtensionType.SKILL == "skill"
        assert ExtensionType.MCP == "mcp"
        assert ExtensionType.TEMPLATE == "template"
        assert ExtensionType.TOOL == "tool"
        assert ExtensionType.MIDDLEWARE == "middleware"
