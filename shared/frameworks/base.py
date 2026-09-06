import abc
from pathlib import Path
from typing import Dict, List, Optional, Tuple

class TelemetryParser(abc.ABC):
    """Parses framework-specific execution artifacts (traces, logs) into standard structures."""
    
    @abc.abstractmethod
    def read_actions(self, trace_path: Path) -> List[Dict]:
        """Return the ordered actions in a trace, each with its selector and error.
        
        Must return [] if the artifact is missing or malformed.
        """
        pass

    @abc.abstractmethod
    def failing_action(self, actions: List[Dict]) -> Optional[Dict]:
        """Find the final failed action in the trace."""
        pass


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
    
    @abc.abstractmethod
    def is_ambiguous_locator(self, error_message: str) -> bool:
        """Return True if the error indicates multiple elements matched a locator."""
        pass


class CodeEngine(abc.ABC):
    """Handles framework-specific code generation and parsing rules."""
    
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


class MCPProvider(abc.ABC):
    """Provides configuration for the framework's Live Browser Exploration MCP server."""
    
    @abc.abstractmethod
    def get_server_config(self, project_root: Path, headless: Optional[bool] = None, cdp_endpoint: Optional[str] = None, storage_state: Optional[str] = None) -> Dict:
        """Return the JSON-serializable MCP server configuration dictionary."""
        pass

    @abc.abstractmethod
    def allowed_tools(self) -> List[str]:
        """Return the list of MCP tools the LLM is allowed to call for this framework (e.g., ['mcp__playwright__*'])."""
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

    @property
    @abc.abstractmethod
    def mcp(self) -> MCPProvider:
        pass
