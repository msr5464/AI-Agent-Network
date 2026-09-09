"""Framework Plugin Architecture.

The agents couple to an automation framework in two entirely separate ways, and
keeping them separate is what makes this tractable:

  * As the target repo's CONVENTION — the syntax its tests are written in
    (`page.locator(...)` vs `@FindBy(id=...)`), how its tests are invoked, what
    failure artefacts it leaves, what its errors mean. This is what the plugins
    below abstract, and it is the whole job.

  * As the agents' own INSTRUMENT — driving a live browser to inspect the DOM,
    score candidate locators and verify a fix (shared/locator_verify.py,
    shared/locator_candidates.py, shared/locator_resolve.py, the MCP server).
    This is deliberately always Playwright, whatever the target repo uses,
    because it talks CDP to a browser and a browser does not know or care which
    framework drove it there. That is also why there is no MCPProvider here: the
    Selenium plugin's "MCP provider" returned the Playwright MCP server, because
    that is the correct answer rather than a workaround.

Which plugin is active is DERIVED FROM THE REPOSITORY (see detect.py), not
configured. A repo either is or is not a Playwright repo, so a setting can only
agree or be wrong — and it was wrong, silently, for the whole target repo.
"""

import threading

from shared.frameworks import detect
from shared.frameworks.base import (
    CodeEngine,
    DiagnosticEngine,
    FrameworkPlugin,
    TelemetryParser,
    TestRunner,
)
from shared.frameworks.playwright_plugin import PlaywrightPlugin
from shared.frameworks.selenium_plugin import SeleniumPlugin

_BUILDERS = {
    detect.PLAYWRIGHT: PlaywrightPlugin,
    detect.SELENIUM: SeleniumPlugin,
}

# Plugins are stateless, so one instance per framework is reused. Keyed by
# framework rather than memoised on first call: the previous single global was
# resolved once per process and never invalidated, so a settings change could
# not affect an already-running server, and two repos could never differ.
_INSTANCES = {}
_LOCK = threading.Lock()


def get_plugin(framework: str) -> FrameworkPlugin:
    """The plugin for a named framework."""
    key = (framework or "").strip().lower()
    if key not in _BUILDERS:
        raise ValueError(f"Unsupported automation framework: {framework!r}. "
                         f"Supported: {', '.join(sorted(_BUILDERS))}")
    with _LOCK:
        if key not in _INSTANCES:
            _INSTANCES[key] = _BUILDERS[key]()
        return _INSTANCES[key]


def _default_workspace() -> str:
    """The checkout this process is working against, if it can be determined.

    FRAMEWORK_DIR is set per run to that run's isolated worktree, so resolving
    through it makes the framework per-run without any extra plumbing.
    """
    import os
    direct = (os.environ.get("FRAMEWORK_DIR") or "").strip()
    if direct:
        return direct
    workspace = (os.environ.get("WORKSPACE_DIR") or "").strip()
    repo = (os.environ.get("GITHUB_REPO_AUTOMATION") or "").strip()
    if workspace and repo:
        return str(__import__("pathlib").Path(workspace) / repo)
    return ""


def active_framework(workspace=None, repo_name: str = "") -> str:
    """The framework name for a checkout, derived from what the repo contains."""
    framework, _why = detect.resolve(workspace or _default_workspace(), repo_name)
    return framework


def get_active_plugin(workspace=None, repo_name: str = "") -> FrameworkPlugin:
    """The plugin for the checkout in play.

    Callers that already know their workspace should pass it; the rest resolve
    through FRAMEWORK_DIR, which the runner points at the run's own worktree.
    """
    return get_plugin(active_framework(workspace, repo_name))


__all__ = [
    "FrameworkPlugin",
    "TelemetryParser",
    "TestRunner",
    "DiagnosticEngine",
    "CodeEngine",
    "active_framework",
    "get_active_plugin",
    "get_plugin",
]
