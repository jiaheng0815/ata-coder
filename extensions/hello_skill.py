# -*- coding: utf-8 -*-
"""
Example extension — demonstrates the Extension API.

This is a minimal example showing how to create a custom extension
that contributes a system prompt and a tool.

Usage:
    # Auto-discovered from extensions/ directory
    # Or register manually:
    from ata_coder.extension import get_extension_manager
    from extensions.hello_skill import HelloSkill
    get_extension_manager().register(HelloSkill())
    get_extension_manager().activate("hello-skill")
"""

from ata_coder.extension import Extension, ExtensionMeta, extension


@extension(
    name="hello-skill",
    version="1.0.0",
    description="A friendly companion skill that greets you",
    tags=["skill", "example"],
    priority=90,
)
class HelloSkill(Extension):
    """Example skill extension that adds a friendly tone to responses."""

    def get_prompt(self) -> str:
        return (
            "You are a friendly and encouraging coding assistant. "
            "Always start your responses with a warm greeting and "
            "end with an encouraging note."
        )

    def on_activate(self) -> None:
        """Called when this extension is activated."""
        import logging
        logger = logging.getLogger(__name__)
        logger.info("HelloSkill activated — be friendly!")

    def on_deactivate(self) -> None:
        """Called when this extension is deactivated."""
        import logging
        logger = logging.getLogger(__name__)
        logger.info("HelloSkill deactivated — back to normal.")
