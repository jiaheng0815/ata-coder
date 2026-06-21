"""
Tests for permissions — PermissionStore, PermissionMode, tool_category.
"""

from ata_coder.permissions import (
    PermissionStore,
    PermissionMode,
    PermissionRule,
    tool_category,
    READ_TOOLS,
    WRITE_TOOLS,
)


class TestToolCategory:
    """tool_category function."""

    def test_read_tool(self):
        """read_file should be classified as 'read'."""
        assert tool_category("read_file") == "read"

    def test_write_tool(self):
        """write_file should be classified as 'write'."""
        assert tool_category("write_file") == "write"

    def test_shell_tool(self):
        """run_shell should be classified as 'shell'."""
        assert tool_category("run_shell") == "shell"

    def test_mcp_tool(self):
        """mcp__* tools should be classified as 'mcp'."""
        assert tool_category("mcp__read_file") == "mcp"
        assert tool_category("mcp__run_command") == "mcp"

    def test_unknown_tool(self):
        """Unknown tools should be classified as 'other'."""
        assert tool_category("unknown_tool") == "other"

    def test_all_read_tools(self):
        """All tools in READ_TOOLS set should return 'read'."""
        for tool in READ_TOOLS:
            assert tool_category(tool) == "read"

    def test_all_write_tools(self):
        """All tools in WRITE_TOOLS set should return 'write'."""
        for tool in WRITE_TOOLS:
            assert tool_category(tool) == "write"


class TestPermissionMode:
    """PermissionMode enum."""

    def test_to_label_allow(self):
        """ALLOW mode should show checkmark."""
        assert "ALLOW" in PermissionMode.ALLOW.to_label()

    def test_to_label_deny(self):
        """DENY mode should show cross."""
        assert "DENY" in PermissionMode.DENY.to_label()

    def test_to_label_ask(self):
        """ASK mode should show question mark."""
        assert "ASK" in PermissionMode.ASK.to_label()


class TestPermissionStore:
    """PermissionStore core logic."""

    def test_read_tools_always_allowed(self):
        """Read tools should always be allowed by default."""
        store = PermissionStore()
        assert store.check("read_file") is True

    def test_deny_by_exact_rule(self):
        """Exact tool name rule with DENY should block."""
        store = PermissionStore()
        store.set_rule("write_file", PermissionMode.DENY)
        assert store.check("write_file") is False

    def test_allow_by_exact_rule(self):
        """Exact tool name rule with ALLOW should pass."""
        store = PermissionStore()
        store.set_rule("write_file", PermissionMode.ALLOW)
        assert store.check("write_file") is True

    def test_allow_once(self):
        """allow_once should permit a single call."""
        store = PermissionStore()
        store.allow_once("write_file")
        assert store.check("write_file") is True
        # Second call should not be automatically allowed
        assert store.check("write_file") is not True  # no prompt callback = denied

    def test_category_deny(self):
        """Category rule with DENY should block all tools in that category."""
        store = PermissionStore()
        store.set_category_rule("shell", PermissionMode.DENY)
        assert store.check("run_shell") is False

    def test_category_allow(self):
        """Category rule with ALLOW should permit all tools in that category."""
        store = PermissionStore()
        store.set_category_rule("write", PermissionMode.ALLOW)
        assert store.check("write_file") is True
        assert store.check("edit_file") is True

    def test_tool_rule_overrides_category(self):
        """Exact tool rule should take precedence over category rule."""
        store = PermissionStore()
        store.set_category_rule("shell", PermissionMode.DENY)
        store.set_rule("run_shell", PermissionMode.ALLOW)
        assert store.check("run_shell") is True

    def test_wildcard_deny(self):
        """Wildcard '*' with DENY should block tools without specific rules."""
        store = PermissionStore()
        store.set_rule("*", PermissionMode.DENY)
        # Shell tools are denied, but read tools still pass (checked before wildcard)
        assert store.check("write_file") is False

    def test_no_prompt_callback_denies_write(self):
        """Without prompt callback, write tools should be denied."""
        store = PermissionStore()
        assert store.check("write_file") is False

    def test_prompt_callback_called(self):
        """Prompt callback should be invoked for write tools."""
        store = PermissionStore()
        callback_called = [False]

        def prompt_cb(tool_name, arguments, category):
            callback_called[0] = True
            return True

        store.set_prompt_callback(prompt_cb)
        result = store.check("write_file", {"file_path": "test.txt"})
        assert result is True
        assert callback_called[0] is True

    def test_prompt_callback_receives_args(self):
        """Prompt callback should receive correct arguments."""
        store = PermissionStore()
        captured = [None, None, None]

        def prompt_cb(tool_name, arguments, category):
            captured[0] = tool_name
            captured[1] = arguments
            captured[2] = category
            return True

        store.set_prompt_callback(prompt_cb)
        store.check("run_shell", {"command": "ls"})
        assert captured[0] == "run_shell"
        assert captured[1] == {"command": "ls"}
        assert captured[2] == "shell"

    def test_get_category_mode_none(self):
        """get_category_mode should return None for unset categories."""
        store = PermissionStore()
        assert store.get_category_mode("shell") is None

    def test_get_category_mode_after_set(self):
        """get_category_mode should return the set mode."""
        store = PermissionStore()
        store.set_category_rule("shell", PermissionMode.ALLOW)
        assert store.get_category_mode("shell") == PermissionMode.ALLOW

    def test_set_rule_ask_removes_rule(self):
        """Setting a rule to ASK should remove it (revert to default)."""
        store = PermissionStore()
        store.set_rule("write_file", PermissionMode.ALLOW)
        store.set_rule("write_file", PermissionMode.ASK)
        assert store.check("write_file") is False  # no rule + no callback = denied

    def test_describe(self):
        """describe() should return a readable string."""
        store = PermissionStore()
        desc = store.describe()
        assert "Reads: always allowed" in desc
        assert "shell: ask" in desc


class TestPermissionRule:
    """PermissionRule dataclass."""

    def test_rule_creation(self):
        """PermissionRule should hold tool_name and mode."""
        rule = PermissionRule(tool_name="write_file", mode=PermissionMode.DENY)
        assert rule.tool_name == "write_file"
        assert rule.mode == PermissionMode.DENY

    def test_rule_with_category(self):
        """PermissionRule should accept optional category."""
        rule = PermissionRule(tool_name="run_shell", mode=PermissionMode.ALLOW, category="shell")
        assert rule.category == "shell"
