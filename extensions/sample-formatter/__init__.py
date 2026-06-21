"""Sample community plugin: auto-format code on file write.

This demonstrates the standard structure for an ATA Coder community plugin.
Plugins are standard Python packages that expose an Extension subclass.

To install as a real plugin, this would be packaged as:
    ata-coder-plugin-formatter/
    ├── pyproject.toml
    └── ata_coder_plugin_formatter/
        ├── __init__.py
        └── extension.py
"""

from ata_coder.extension import Extension, ExtensionMeta, extension


@extension(
    name="sample-formatter",
    version="0.1.0",
    description="Auto-format Python code on file write using black/ruff",
    author="ATA Coder Community",
    homepage="https://github.com/jiaheng0815/ata-coder-plugins",
    license="MIT",
    tags=["tool", "formatter", "python"],
    priority=50,
)
class SampleFormatter(Extension):
    """Format Python files after they are written by the agent."""

    meta: ExtensionMeta

    def on_activate(self) -> None:
        """Called when the extension is activated."""
        import logging
        self._logger = logging.getLogger(__name__)
        self._logger.info("SampleFormatter activated — Python files will be auto-formatted")

    def on_deactivate(self) -> None:
        """Called when the extension is deactivated."""

    def get_prompt(self) -> str:
        """Inject formatting rules into the system prompt."""
        return (
            "\n## Code Formatting\n"
            "- All Python code must use 4-space indentation\n"
            "- Maximum line length: 100 characters\n"
            "- Use double quotes for strings\n"
            "- Add type hints to all function signatures\n"
            "- Sort imports: stdlib → third-party → local\n"
        )

    def validate(self) -> tuple[bool, str]:
        """Check that required formatters are available."""
        import shutil
        missing = []
        for tool in ["ruff", "black"]:
            if shutil.which(tool) is None:
                missing.append(tool)
        if missing:
            return False, f"Missing formatters: {', '.join(missing)}. Install with: pip install {' '.join(missing)}"
        return True, "OK"


# Entry point for pip-installable plugin discovery
def export_extension() -> SampleFormatter:
    """Standard entry point for community plugin loading.

    This function is referenced in the plugin's pyproject.toml:
        [project.entry-points."ata_coder.plugins"]
        sample_formatter = "ata_coder_plugin_formatter:export_extension"
    """
    return SampleFormatter()
