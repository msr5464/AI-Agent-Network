import abc
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

class TelemetryParser(abc.ABC):
    """Reads a framework's execution artifacts into one shared action shape.

    ACTION SCHEMA — every parser must return dicts of this shape, and consumers
    may rely on nothing else:

        {"action":   str,          # "click", "fill", "navigate", ...
         "selector": str,          # "" when the action names no element
         "url":      str,          # "" when unknown
         "value":    str,          # "" when the action carries no value
         "error":    str,          # "" for actions that succeeded
         "inferred": bool}         # True when derived, not directly recorded

    Keys are REQUIRED, values may be empty. This was previously unspecified, and
    the shared prompt formatter indexed a["action"], a["selector"] and a["url"]
    directly — so a parser returning its raw log records (as the Selenium one
    did) crashed the prompt builder with a KeyError on the first real trace.
    """

    #: Action names that say nothing about locators; dropped from the prompt timeline.
    NOISE_ACTIONS: frozenset = frozenset()

    #: Normalised keys, with the empty value each defaults to.
    ACTION_KEYS = {"action": "", "selector": "", "url": "", "value": "",
                   "error": "", "inferred": False}

    @classmethod
    def normalise(cls, raw: Dict) -> Dict:
        """Coerce one parsed record into the shared schema. Never raises."""
        record = dict(cls.ACTION_KEYS)
        for key, default in cls.ACTION_KEYS.items():
            value = raw.get(key, default)
            record[key] = bool(value) if isinstance(default, bool) else (
                "" if value is None else str(value))
        return record

    @abc.abstractmethod
    def discover(self, results_dir: Path, method_name: str) -> List[Path]:
        """Every telemetry artifact this framework wrote for one test method.

        Order does not matter; callers pick by mtime. Discovery belongs here
        because the layout is the framework's own: Playwright writes
        traces/<method>_*.zip, another framework may write a JSONL log or
        nothing at all. Callers used to glob for *.zip themselves, which meant a
        non-Playwright parser could never be handed a path it accepted — its
        telemetry was unreachable however correct the parser was.
        """
        pass

    @abc.abstractmethod
    def read_actions(self, trace_path: Path) -> List[Dict]:
        """The ordered actions in one artifact, each matching ACTION_KEYS.

        Must return [] if the artifact is missing or malformed.
        """
        pass

    @abc.abstractmethod
    def failing_action(self, actions: List[Dict]) -> Optional[Dict]:
        """Find the final failed action in the trace."""
        pass

    def read_network(self, trace_path: Path) -> List[Dict]:
        """HAR-shaped network records from one artifact, [] when it has none.

        Optional: most frameworks record no network log, and losing that
        evidence channel is the honest outcome for them.
        """
        return []


class TestRunner(abc.ABC):
    """Handles framework-specific execution commands and arguments."""
    
    @abc.abstractmethod
    def detect_command(self, workspace: Path, class_simple: str, method: str) -> Optional[List[str]]:
        """Find a runner command for this framework in the given workspace."""
        pass

    @abc.abstractmethod
    def apply_browser_mode(self, cmd: List[str], properties: Dict[str, str]) -> Tuple[List[str], Dict[str, str]]:
        """Ensure global headed/headless states reach the framework runner."""
        pass


class DiagnosticEngine(abc.ABC):
    """Interprets framework-specific error messages and semantics."""

    #: Lower-case error text meaning a null value reached an interaction — almost
    #: always an unset property (a missing credential), not a locator problem.
    NULL_VALUE_SIGNALS: Tuple[str, ...] = ()
    
    @abc.abstractmethod
    def is_ambiguous_locator(self, error_message: str) -> bool:
        """Return True if the error indicates multiple elements matched a locator."""
        pass

    @abc.abstractmethod
    def is_locator_resolution_failure(self, error_message: str) -> bool:
        """Return True if a locator matched nothing usable: not found, stale, not interactable."""
        pass


class CodeEngine(abc.ABC):
    """Handles framework-specific code generation and parsing rules."""

    #: Type names a page object declares its elements with (`Locator foo;`).
    ELEMENT_TYPES: Tuple[str, ...] = ()

    #: Method names whose first string argument is a selector (`locator("#id")`).
    LOCATOR_CALLS: Tuple[str, ...] = ()

    #: (pattern, label) for calls that drive the browser directly instead of
    #: through the repo's wrappers. Edits that add one are rejected.
    RAW_DRIVER_CALLS: Tuple[Tuple["re.Pattern", str], ...] = ()

    @abc.abstractmethod
    def remove_framework_suffixes(self, selector: str) -> str:
        """Strip framework-specific pseudo-classes (e.g. :has-text) from a CSS selector."""
        pass

    @abc.abstractmethod
    def is_dom_selector(self, raw: str) -> bool:
        """Whether a recorded locator can actually match in a real browser run."""
        pass

    @abc.abstractmethod
    def normalize_selector(self, raw: str) -> Optional[str]:
        """Reduce a recorded locator to plain CSS, or None if it cannot be evaluated."""
        pass

    @abc.abstractmethod
    def extract_locators(self, source: str) -> List[Dict[str, str]]:
        """Extract all locators declared in a page object source file."""
        pass

    @abc.abstractmethod
    def quote_css_value(self, value: str) -> str:
        """Quote an attribute VALUE inside a CSS selector."""
        pass

    @abc.abstractmethod
    def build_has_text_selector(self, anchor: str, text: str, tag: str) -> str:
        """Build a selector that anchors on an element containing specific text."""
        pass

    @abc.abstractmethod
    def map_role(self, role: str) -> Optional[str]:
        """Map an ARIA role to the framework's native role enum."""
        pass

    @abc.abstractmethod
    def emit_locator(self, **kwargs) -> Dict[str, str]:
        """Emit the native code snippet for a synthesized locator."""
        pass


class FrameworkPlugin(abc.ABC):
    """The central registry for a framework's capabilities."""
    
    @property
    @abc.abstractmethod
    def telemetry(self) -> TelemetryParser:
        pass

    @property
    @abc.abstractmethod
    def runner(self) -> TestRunner:
        pass

    @property
    @abc.abstractmethod
    def diagnostics(self) -> DiagnosticEngine:
        pass

    @property
    @abc.abstractmethod
    def code(self) -> CodeEngine:
        pass

    # There is deliberately no `mcp` here. Browser inspection is the agents' own
    # instrument rather than a target-repo convention, it speaks CDP, and CDP is
    # a browser-level protocol — so it is always Playwright and lives in
    # shared/mcp_config.py. The interface this replaced had two implementations
    # that returned the same server and the same allowed-tools list.
