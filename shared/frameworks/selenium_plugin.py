import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from shared.frameworks.base import (
    CodeEngine,
    DiagnosticEngine,
    FrameworkPlugin,
    TelemetryParser,
    TestRunner,
)


class SeleniumTelemetryParser(TelemetryParser):
    """Reads the JSONL action log a Selenium repo's WebDriverListener writes.

    Selenium has no native trace format, so the target repository has to produce
    one — see docs/FRAMEWORK_INTEGRATION.md for the listener contract.
    """

    # Names a repo might reasonably give that log. Checked in order.
    _LOG_PATTERNS = ("telemetry/{method}_*.jsonl", "telemetry/{method}.jsonl",
                     "traces/{method}_*.jsonl", "logs/{method}_*.jsonl",
                     "{method}_*.jsonl")

    def discover(self, results_dir: Path, method_name: str) -> List[Path]:
        """Find this method's action log.

        Without this, every caller globbed traces/<method>_*.zip themselves —
        Playwright's layout — so this parser, which only accepts .jsonl, could
        never be handed a path it would accept. Selenium telemetry was
        unreachable by construction, not by bug.
        """
        results_dir = Path(results_dir)
        if not results_dir.is_dir():
            return []
        found: List[Path] = []
        for pattern in self._LOG_PATTERNS:
            found.extend(p for p in results_dir.rglob(pattern.format(method=method_name))
                         if p.is_file())
        # rglob patterns overlap; keep first-seen order without duplicates.
        return list(dict.fromkeys(found))

    def read_actions(self, trace_path: Path) -> List[Dict]:
        """Parse the JSONL log into the shared action schema.

        Records are normalised rather than returned raw: consumers index
        "action", "selector" and "url" directly, so raw log records — whose keys
        are whatever the repo's listener happened to write — raised KeyError in
        the prompt builder.
        """
        trace_path = Path(trace_path)
        if not trace_path.exists():
            return []

        if trace_path.is_dir():
            log_files = sorted(trace_path.glob("*.jsonl"))
            if not log_files:
                return []
            trace_path = log_files[0]

        if not trace_path.name.endswith(".jsonl"):
            return []

        actions: List[Dict] = []
        try:
            with open(trace_path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        actions.append(self._to_action(record))
        except OSError:
            return []
        return actions

    # Field names a listener plausibly uses, mapped onto the shared schema.
    _ALIASES = {
        "action": ("action", "command", "event", "method", "name"),
        "selector": ("selector", "locator", "target", "by", "element"),
        "url": ("url", "currentUrl", "page"),
        "value": ("value", "text", "input", "args"),
        "error": ("error", "exception", "message", "failure"),
    }

    def _to_action(self, record: Dict) -> Dict:
        flattened = {}
        for key, aliases in self._ALIASES.items():
            for alias in aliases:
                if record.get(alias) not in (None, ""):
                    flattened[key] = record[alias]
                    break
        return self.normalise(flattened)

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
    # A null handed to sendKeys().
    NULL_VALUE_SIGNALS = ("keys to send should be a not null charsequence",)

    # Phrases that actually appear in Selenium/WebDriver failures. The previous
    # implementation matched "multiple elements matched", which no Selenium
    # binding emits, so this returned False for every input and the
    # AMBIGUOUS_LOCATOR verdict could never fire on a Selenium repo.
    _AMBIGUOUS_SIGNALS = (
        "multiple elements",           # custom wrappers that assert uniqueness
        "more than one element",
        "matched 2 elements", "matched more than",
    )
    # Selenium reports "not found" and "found but unusable" as distinct
    # exceptions; both mean the locator no longer identifies what it meant to.
    _RESOLUTION_SIGNALS = (
        "nosuchelementexception",
        "staleelementreferenceexception",
        "elementnotinteractableexception",
        "elementclickinterceptedexception",
        "unabletolocateelement",
    )

    def is_ambiguous_locator(self, error_message: str) -> bool:
        text = (error_message or "").lower()
        return any(signal in text for signal in self._AMBIGUOUS_SIGNALS)

    def is_locator_resolution_failure(self, error_message: str) -> bool:
        text = (error_message or "").lower().replace(" ", "")
        return any(signal in text for signal in self._RESOLUTION_SIGNALS)


class SeleniumCodeEngine(CodeEngine):
    ELEMENT_TYPES = ("WebElement", "MobileElement", "By")
    LOCATOR_CALLS = ("cssSelector",)
    RAW_DRIVER_CALLS = (
        (re.compile(r"\bdriver\s*\.\s*findElement"), "driver.findElement"),
        (re.compile(r"\.\s*sendKeys\s*\("), ".sendKeys()"),
        (re.compile(r"\bnew\s+WebDriverWait\b"), "new WebDriverWait"),
        (re.compile(r"\bdriver\s*\.\s*get\s*\("), "driver.get()"),
    )

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
        """An XPath, not a CSS :contains().

        This returned `<anchor>:contains(...)` on the reasoning that
        BeautifulSoup understands it. Its only caller hands the result to a live
        browser locator, not BeautifulSoup — where :contains() is not a valid
        selector, so the call threw, uniqueness came back False, and the whole
        scoped-by-neighbor strategy was silently dead under Selenium.
        """
        return f"//{anchor or '*'}[contains(., {self._xq(text)})]//{tag or '*'}"

    # ARIA roles have no native Selenium accessor, but they are ordinary DOM
    # semantics: an explicit role attribute, or the implicit role of a tag.
    # Returning None for every role meant emit_locator had no branch to take and
    # handed back an empty snippet.
    _ROLE_TAGS = {
        "button": ("button", "input[@type='button' or @type='submit']"),
        "link": ("a",),
        "textbox": ("input[not(@type) or @type='text' or @type='email' or @type='password']",
                    "textarea"),
        "checkbox": ("input[@type='checkbox']",),
        "radio": ("input[@type='radio']",),
        "combobox": ("select",),
        "heading": ("h1", "h2", "h3", "h4", "h5", "h6"),
        "img": ("img",),
        "list": ("ul", "ol"),
        "listitem": ("li",),
        "table": ("table",),
    }

    @staticmethod
    def _xq(value: str) -> str:
        """Quote a literal for XPath, including values containing quotes."""
        text = value or ""
        if '"' not in text:
            return f'"{text}"'
        if "'" not in text:
            return f"'{text}'"
        parts = text.split('"')
        return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"

    def map_role(self, role: str) -> Optional[str]:
        return (role or "").strip().lower() or None

    def _role_xpath(self, role: str, name: str = "") -> str:
        """XPath matching an explicit role attribute or the tags that imply it."""
        role = (role or "").strip().lower()
        branches = [f"//*[@role={self._xq(role)}]"]
        for tag in self._ROLE_TAGS.get(role, ()):  # implicit roles
            branches.append(f"//{tag}" if "[" not in tag else f"//{tag}")
        if not name:
            return " | ".join(branches)
        return " | ".join(f"{b}[normalize-space(.)={self._xq(name)}]" for b in branches)

    def emit_locator(self, **kwargs) -> Dict[str, str]:
        """Native Selenium code for a locator.

        Attribute values go through quote_css_value rather than being
        interpolated bare: `[data-testid=my testid]` is not valid CSS, and any
        value with a space, quote or leading digit produced exactly that.

        `findby` is emitted alongside because the target repo defines locators
        as @FindBy PageFactory fields, not inline driver.findElement calls —
        emitting only the latter would have contradicted the repo's own
        conventions on every fix.
        """
        def _q(s: str) -> str:
            return '"' + (s or "").replace('\\', '\\\\').replace('"', '\\"') + '"'

        def css(selector: str) -> Dict[str, str]:
            return {
                "python": f"driver.find_element(By.CSS_SELECTOR, {_q(selector)})",
                "java": f"driver.findElement(By.cssSelector({_q(selector)}))",
                "findby": f"@FindBy(css = {_q(selector)})",
            }

        def xpath(expression: str) -> Dict[str, str]:
            return {
                "python": f"driver.find_element(By.XPATH, {_q(expression)})",
                "java": f"driver.findElement(By.xpath({_q(expression)}))",
                "findby": f"@FindBy(xpath = {_q(expression)})",
            }

        # role first, and deliberately so: "name" is overloaded. Alongside a role
        # it is the ACCESSIBLE name, on its own it is the HTML name attribute.
        # Checking the attribute list first turned every role+name request into
        # a [name=...] attribute selector.
        if "role" in kwargs:
            return xpath(self._role_xpath(kwargs["role"], kwargs.get("name", "")))

        for attribute, key in (("data-testid", "testid"), ("placeholder", "placeholder"),
                               ("aria-label", "label"), ("alt", "alt"), ("title", "title"),
                               ("name", "name")):
            if key in kwargs:
                return css(f"[{attribute}={self.quote_css_value(kwargs[key])}]")

        if "text" in kwargs:
            text = kwargs["text"]
            if kwargs.get("exact", False):
                return xpath(f"//*[normalize-space(text())={self._xq(text)}]")
            return xpath(f"//*[contains(text(), {self._xq(text)})]")

        if "selector" in kwargs:
            sel = kwargs["selector"]
            if sel.startswith("/") or sel.startswith("("):
                return xpath(sel)
            if sel.startswith("#") and " " not in sel and "." not in sel[1:]:
                return {
                    "python": f"driver.find_element(By.ID, {_q(sel[1:])})",
                    "java": f"driver.findElement(By.id({_q(sel[1:])}))",
                    "findby": f"@FindBy(id = {_q(sel[1:])})",
                }
            return css(sel)

        return {"python": "", "java": "", "findby": ""}


class SeleniumPlugin(FrameworkPlugin):
    def __init__(self):
        self._telemetry = SeleniumTelemetryParser()
        self._runner = SeleniumTestRunner()
        self._diagnostics = SeleniumDiagnosticEngine()
        self._code = SeleniumCodeEngine()

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

