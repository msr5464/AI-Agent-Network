import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shared.frameworks.base import (
    CodeEngine,
    DiagnosticEngine,
    FrameworkPlugin,
    MCPProvider,
    TelemetryParser,
    TestRunner,
)


class SeleniumTelemetryParser(TelemetryParser):
    def read_actions(self, trace_path: Path) -> List[Dict]:
        """
        Selenium does not have native .zip traces.
        We assume the target automation repo implements a WebDriverEventListener
        that writes a JSONL file (e.g., selenium-actions.jsonl).
        """
        trace_path = Path(trace_path)
        if not trace_path.exists():
            return []
            
        # Try to find a jsonl log
        if trace_path.is_dir():
            log_files = list(trace_path.glob("*.jsonl"))
            if not log_files:
                return []
            trace_path = log_files[0]
            
        if not trace_path.name.endswith(".jsonl"):
            return []
            
        events = []
        try:
            with open(trace_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
        except Exception:
            return []
            
        return events

    def failing_action(self, actions: List[Dict]) -> Optional[Dict]:
        return next((a for a in actions if a.get("error")), None)


class SeleniumTestRunner(TestRunner):
    _NON_MODULE_DIRS = {"target", "build", "node_modules", "test-output", "venv", ".venv"}

    def detect_command(self, workspace: Path, class_simple: str, method: str) -> Optional[List[str]]:
        gradle_filter = f"*.{class_simple}.{method}" if method else f"*.{class_simple}"
        maven_filter = f"{class_simple}#{method}" if method else class_simple

        def build_cmd(root: Path) -> Optional[List[str]]:
            if (root / "gradlew").exists():
                return ["./gradlew", "test", "--tests", gradle_filter, "-q", "--rerun-tasks"]
            if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
                return ["gradle", "test", "--tests", gradle_filter, "-q"]
            if (root / "pom.xml").exists():
                return ["mvn", "test", f"-Dtest={maven_filter}"]
            return None

        cmd = build_cmd(workspace)
        if cmd:
            return cmd

        try:
            children = sorted(p for p in workspace.iterdir() if p.is_dir())
        except OSError:
            return None
        for child in children:
            if child.name.startswith(".") or child.name in self._NON_MODULE_DIRS:
                continue
            cmd = build_cmd(child)
            if cmd:
                return cmd
        return None

    def apply_browser_mode(self, cmd: List[str], properties: Dict[str, str]) -> Tuple[List[str], Dict[str, str]]:
        from shared import browser_mode
        decided = browser_mode.configured()
        if decided is None or "headless" in properties:
            return cmd, properties
            
        runner = " ".join(cmd[:3]).lower()
        if any(tool in runner for tool in ("mvn", "maven", "gradle")):
            properties = {**properties, **browser_mode.maven_properties()}
            
        return cmd, properties


class SeleniumDiagnosticEngine(DiagnosticEngine):
    def is_ambiguous_locator(self, error_message: str) -> bool:
        # Selenium typically uses driver.findElement, which returns the first element
        # and doesn't throw if there are multiple. If someone uses findElements and
        # throws a custom exception, we could detect it here.
        return "multiple elements matched" in (error_message or "").lower()


class SeleniumCodeEngine(CodeEngine):
    _LOCATOR_PATTERNS = (
        re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?driver\.findElement\s*\(\s*By\s*\.\s*(?P<by>cssSelector|id|xpath|className|name)\s*\(\s*(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
        re.compile(r"""@FindBy\s*\(\s*(?P<by>css|id|xpath|className|name)\s*=\s*(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
    )
    _FINDBY_FIELD = re.compile(r"\b(?:WebElement|List<WebElement>)\s+(\w+)")

    def remove_framework_suffixes(self, selector: str) -> str:
        return selector

    def is_dom_selector(self, raw: str) -> bool:
        if not raw or not raw.strip():
            return False
        return True

    def normalize_selector(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        selector = raw.strip()
        # If it's an XPath, BeautifulSoup can't parse it
        if selector.startswith("/") or selector.startswith("("):
            return None
        return selector

    def extract_locators(self, source: str) -> List[Dict[str, str]]:
        found = []
        seen = set()
        if not source:
            return found

        def _unescape(sel: str) -> str:
            return sel.replace('\\"', '"').replace("\\'", "'").replace("\\\\", "\\")

        for index, pattern in enumerate(self._LOCATOR_PATTERNS):
            is_findby = index == 1
            for match in pattern.finditer(source):
                raw = _unescape(match.group("sel"))
                by = match.group("by")
                if not raw or raw in seen:
                    continue
                seen.add(raw)
                
                # Convert ID to CSS for coverage
                css_selector = raw
                if by.lower() == "id":
                    css_selector = f"#{raw}"
                elif by.lower() == "classname":
                    css_selector = f".{raw.replace(' ', '.')}"
                elif by.lower() == "xpath":
                    css_selector = "" # Cannot normalize XPath easily to CSS
                elif by.lower() == "name":
                    css_selector = f"[name='{raw}']"
                    
                name = match.groupdict().get("name") or ""
                if not name and is_findby:
                    tail = source[match.end():match.end() + 200]
                    field = self._FINDBY_FIELD.search(tail)
                    if field:
                        name = field.group(1)
                        
                found.append({"name": name, "raw": raw, "kind": "css" if by != "xpath" else "xpath",
                              "value": "", "approx": False,
                              "selector": self.normalize_selector(css_selector) or ""})
        return found

    def quote_css_value(self, value: str) -> str:
        text = value or ""
        if "'" not in text:
            return "'" + text + "'"
        return '"' + text.replace('"', '\\"') + '"'

    def build_has_text_selector(self, anchor: str, text: str, tag: str) -> str:
        # BeautifulSoup supports :contains() which simulates searching for text
        return f'{anchor}:contains({self.quote_css_value(text)}) {tag}'

    def map_role(self, role: str) -> Optional[str]:
        # Selenium has no native getByRole
        return None

    def emit_locator(self, **kwargs) -> Dict[str, str]:
        def _q(s: str) -> str:
            return '"' + (s or "").replace('\\', '\\\\').replace('"', '\\"') + '"'

        if "testid" in kwargs:
            return {
                "python": f"driver.find_element(By.CSS_SELECTOR, {_q(f'[data-testid={kwargs['testid']}]')})",
                "java": f"driver.findElement(By.cssSelector({_q(f'[data-testid={kwargs['testid']}]')}))"
            }
        if "placeholder" in kwargs:
            return {
                "python": f"driver.find_element(By.CSS_SELECTOR, {_q(f'[placeholder={kwargs['placeholder']}]')})",
                "java": f"driver.findElement(By.cssSelector({_q(f'[placeholder={kwargs['placeholder']}]')}))"
            }
        if "label" in kwargs:
            return {
                "python": f"driver.find_element(By.CSS_SELECTOR, {_q(f'[aria-label={kwargs['label']}]')})",
                "java": f"driver.findElement(By.cssSelector({_q(f'[aria-label={kwargs['label']}]')}))"
            }
        if "text" in kwargs:
            text = kwargs["text"]
            exact = kwargs.get("exact", False)
            if exact:
                xpath = f"//*[text()={_q(text)}]"
            else:
                xpath = f"//*[contains(text(), {_q(text)})]"
            return {
                "python": f"driver.find_element(By.XPATH, {_q(xpath)})",
                "java": f"driver.findElement(By.xpath({_q(xpath)}))"
            }
        if "selector" in kwargs:
            sel = kwargs["selector"]
            if sel.startswith("#") and " " not in sel and "." not in sel[1:]:
                return {
                    "python": f"driver.find_element(By.ID, {_q(sel[1:])})",
                    "java": f"driver.findElement(By.id({_q(sel[1:])}))"
                }
            return {
                "python": f"driver.find_element(By.CSS_SELECTOR, {_q(sel)})",
                "java": f"driver.findElement(By.cssSelector({_q(sel)}))"
            }
        return {"python": "", "java": ""}


class SeleniumMCPProvider(MCPProvider):
    def get_server_config(self, project_root: Path, headless: Optional[bool] = None, cdp_endpoint: Optional[str] = None, storage_state: Optional[str] = None) -> Dict:
        # Fallback to Playwright MCP for DOM exploration since DOM structure is identical
        # and Selenium lacks a robust open-source MCP server for navigation right now.
        #
        # If parked repair mode is used (cdp_endpoint is present), this relies on the
        # target automation repository using Selenium 4+ and exposing the underlying
        # Chrome DevTools Protocol (CDP) port. Because CDP is a browser-level protocol,
        # @playwright/mcp can attach to a browser that was originally launched by Selenium.
        version = os.environ.get("PLAYWRIGHT_MCP_VERSION", "0.0.79")
        command = f"@playwright/mcp@{version}"
        
        if cdp_endpoint:
            args = [command, "--cdp-endpoint", str(cdp_endpoint)]
            return {"mcpServers": {"playwright": {"command": "npx", "args": ["-y"] + args}}}
            
        args = [command, "--isolated", "--viewport-size=1920,1080"]
        
        from shared import browser_mode
        if headless is None:
            headless = browser_mode.headless()
        if headless:
            args.append("--headless")
            
        return {
            "mcpServers": {
                # We name it 'playwright' so the allowed_tools prompt constraint natively picks it up
                "playwright": {
                    "command": "npx",
                    "args": ["-y"] + args,
                }
            }
        }

    def allowed_tools(self) -> List[str]:
        return ["mcp__playwright__*"]


class SeleniumPlugin(FrameworkPlugin):
    def __init__(self):
        self._telemetry = SeleniumTelemetryParser()
        self._runner = SeleniumTestRunner()
        self._diagnostics = SeleniumDiagnosticEngine()
        self._code = SeleniumCodeEngine()
        self._mcp = SeleniumMCPProvider()

    @property
    def telemetry(self) -> TelemetryParser:
        return self._telemetry

    @property
    def runner(self) -> TestRunner:
        return self._runner

    @property
    def diagnostics(self) -> DiagnosticEngine:
        return self._diagnostics

    @property
    def code(self) -> CodeEngine:
        return self._code

    @property
    def mcp(self) -> MCPProvider:
        return self._mcp
