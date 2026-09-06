"""Framework Plugin Architecture.

This module provides the Plugin interfaces required to make the QA-Agent-Network
framework-agnostic. Adapters for specific frameworks (like Playwright, Selenium)
must implement these interfaces.
"""

from shared.frameworks.base import (
    CodeEngine,
    DiagnosticEngine,
    FrameworkPlugin,
    MCPProvider,
    TelemetryParser,
    TestRunner,
)
from shared.frameworks.playwright_plugin import PlaywrightPlugin
from shared.frameworks.selenium_plugin import SeleniumPlugin

_ACTIVE_PLUGIN = None


def get_active_plugin() -> FrameworkPlugin:
    """Return the active framework plugin based on configuration."""
    global _ACTIVE_PLUGIN
    if not _ACTIVE_PLUGIN:
        import os

        # Currently defaults to Playwright. In the future, this can read from repo-map.json or env.
        framework = os.environ.get("AUTOMATION_FRAMEWORK", "playwright").lower()
        if framework == "playwright":
            _ACTIVE_PLUGIN = PlaywrightPlugin()
        elif framework == "selenium":
            _ACTIVE_PLUGIN = SeleniumPlugin()
        else:
            raise ValueError(f"Unsupported AUTOMATION_FRAMEWORK: {framework}")
    return _ACTIVE_PLUGIN


__all__ = [
    "FrameworkPlugin",
    "TelemetryParser",
    "TestRunner",
    "DiagnosticEngine",
    "CodeEngine",
    "MCPProvider",
    "get_active_plugin",
]
