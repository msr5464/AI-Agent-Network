import json
import os
import re
import zipfile
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


class PlaywrightTelemetryParser(TelemetryParser):
    
    # Actions that say nothing about locators; noise in a timeline.
    _UNINTERESTING = {"BrowserContext.newPage", "Frame.content", "BrowserContext.close",
                      "Browser.close", "Page.close", "Tracing.start", "Tracing.stop"}

    def read_actions(self, trace_path: Path) -> List[Dict]:
        trace_path = Path(trace_path)
        if not trace_path.exists():
            return []

        try:
            with zipfile.ZipFile(trace_path) as archive:
                names = [n for n in archive.namelist() if n.endswith("trace.trace")]
                if not names:
                    return []
                raw = archive.read(names[0]).decode("utf-8", errors="ignore")
        except (zipfile.BadZipFile, OSError, KeyError):
            return []

        events = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue

        outcomes: Dict[str, Dict] = {}
        for event in events:
            if event.get("type") == "after" and event.get("callId"):
                outcomes[event["callId"]] = event

        actions: List[Dict] = []
        for event in events:
            if event.get("type") != "before" or not event.get("class"):
                continue
            name = f"{event['class']}.{event['method']}"
            params = event.get("params") or {}
            after = outcomes.get(event.get("callId"), {})
            error = (after.get("error") or {}).get("message", "")
            duration = ""
            if after.get("endTime") and event.get("startTime"):
                duration = f"{after['endTime'] - event['startTime']:.0f}ms"
            actions.append({
                "action": name,
                "selector": params.get("selector", ""),
                "url": params.get("url", ""),
                "value": str(params.get("value", ""))[:60],
                "error": error.splitlines()[0] if error else "",
                "duration": duration,
            })
        return actions

    def failing_action(self, actions: List[Dict]) -> Optional[Dict]:
        errored = next((a for a in actions if a.get("error")), None)
        if errored:
            return errored
        return self._polled_to_death(actions)

    def _polled_to_death(self, actions: List[Dict], min_repeats: int = 3) -> Optional[Dict]:
        tail = [a for a in actions if a.get("selector")]
        if not tail:
            return None
        last_selector = tail[-1]["selector"]
        repeats = 0
        for action in reversed(tail):
            if action["selector"] != last_selector:
                break
            repeats += 1
        if repeats < min_repeats:
            return None
        return {**tail[-1],
                "error": f"polled {repeats}x without ever becoming visible",
                "inferred": True}


class PlaywrightTestRunner(TestRunner):
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
                return ["mvn", "test", f"-Dtest={maven_filter}", "--no-transfer-progress"]
            if (root / "package.json").exists():
                return ["npx", "playwright", "test", "--grep", method or class_simple, "-x"]
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
        elif "playwright" in runner and not decided and "--headed" not in cmd:
            cmd = cmd + ["--headed"]
        return cmd, properties


class PlaywrightDiagnosticEngine(DiagnosticEngine):
    def is_ambiguous_locator(self, error_message: str) -> bool:
        return "strict mode violation" in (error_message or "").lower()


class PlaywrightCodeEngine(CodeEngine):
    _HAS_TEXT_RE = re.compile(r':has-text\([^)]+\)')
    
    _NARROWING_SUFFIX = re.compile(r"\s*>>\s*(nth=-?\d+|first|last|visible=(true|false))\s*$", re.IGNORECASE)
    _UNEVALUABLE_PREFIX = ("xpath=", "text=", "//", "(//", "..", "id=", "data-testid=")
    _UNEVALUABLE_TOKEN = (">>", ":has-text(", ":text(", ":text-is(", ":visible",
                          ":near(", ":right-of(", ":left-of(", ":above(", ":below(")
    _LOCATOR_PATTERNS = (
        re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*locator\s*\(\s*(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
        re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?By\s*\.\s*(?:cssSelector|id)\s*\(\s*(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
        re.compile(r"""@FindBy\s*\(\s*(?:css|id)\s*=\s*(?P<q>["'])(?P<sel>(?:\\.|(?!(?P=q)).)*)(?P=q)"""),
    )
    _FINDBY_FIELD = re.compile(r"\b(?:WebElement|MobileElement|Locator|By)\s+(\w+)")
    _ACCESSOR_NAME = re.compile(r"\b(?:Locator|WebElement|MobileElement|By)\s+(\w+)\s*\([^)]*\)\s*\{(?:[^{}]|\{[^{}]*\})*$")
    
    _GETBY_PATTERNS = (
        (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByRole\s*\(\s*(?:[\w.]*AriaRole\s*\.\s*)?(?P<role>\w+)"""), "role"),
        (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByTestId\s*\(\s*(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "testid"),
        (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByPlaceholder\s*\(\s*(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "placeholder"),
        (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByText\s*\(\s*(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "text"),
        (re.compile(r"""(?:(?P<name>\w+)\s*=\s*)?(?:page|this\.page)\s*\.\s*getByLabel\s*\(\s*(?P<q>["'])(?P<val>(?:\\.|(?!(?P=q)).)*)(?P=q)"""), "label"),
    )
    _SET_NAME = re.compile(r"""setName\s*\(\s*(["'])(?P<val>(?:\\.|(?!\1).)*)\1""")
    _ARIA_REF = re.compile(r"(?:^|[\[\s])(?:aria-)?ref\s*=", re.I)
    _BARE_REF = re.compile(r"^(?=.*\d)[a-f][0-9a-f]{2,}$", re.I)
    _TEXT_ATTR = re.compile(r"\[\s*text\s*=", re.I)

    _ROLE_TO_JAVA = {
        "button": "BUTTON", "link": "LINK", "textbox": "TEXTBOX", "checkbox": "CHECKBOX",
        "radio": "RADIO", "combobox": "COMBOBOX", "listbox": "LISTBOX", "heading": "HEADING",
        "img": "IMG", "option": "OPTION", "tab": "TAB", "dialog": "DIALOG", "list": "LIST",
        "listitem": "LISTITEM", "searchbox": "SEARCHBOX", "slider": "SLIDER",
        "spinbutton": "SPINBUTTON", "table": "TABLE", "menuitem": "MENUITEM",
    }

    _ROLE_TAGS = {
        "LINK": "a[href], [role='link']",
        "BUTTON": "button, [role='button'], input[type='submit'], input[type='button']",
        "TEXTBOX": "input[type='text'], input[type='email'], input[type='search'], "
                   "input[type='password'], input:not([type]), textarea, [role='textbox']",
        "CHECKBOX": "input[type='checkbox'], [role='checkbox']",
        "RADIO": "input[type='radio'], [role='radio']",
        "HEADING": "h1, h2, h3, h4, h5, h6, [role='heading']",
        "IMG": "img, [role='img']",
        "LISTITEM": "li, [role='listitem']",
        "COMBOBOX": "select, [role='combobox']",
        "TAB": "[role='tab']",
        "DIALOG": "[role='dialog'], dialog",
        "ALERT": "[role='alert']",
    }

    def remove_framework_suffixes(self, selector: str) -> str:
        return self._HAS_TEXT_RE.sub('', selector)

    def is_dom_selector(self, raw: str) -> bool:
        if not raw or not raw.strip():
            return False
        selector = raw.strip()
        if self._ARIA_REF.search(selector):
            return False
        if self._TEXT_ATTR.search(selector):
            return False
        if self._BARE_REF.match(selector):
            return False
        return True

    def normalize_selector(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        selector = raw.strip()
        if selector.startswith("Locator@"):
            selector = selector[len("Locator@"):].strip()
        while True:
            stripped = self._NARROWING_SUFFIX.sub("", selector)
            if stripped == selector:
                break
            selector = stripped.strip()
        if not selector:
            return None
        lowered = selector.lower()
        if lowered.startswith(self._UNEVALUABLE_PREFIX):
            return None
        if any(token in lowered for token in self._UNEVALUABLE_TOKEN):
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
            is_findby = index == 2
            for match in pattern.finditer(source):
                raw = _unescape(match.group("sel"))
                if not raw or raw in seen:
                    continue
                seen.add(raw)
                name = match.groupdict().get("name") or ""
                if not name and is_findby:
                    tail = source[match.end():match.end() + 200]
                    field = self._FINDBY_FIELD.search(tail)
                    if field:
                        name = field.group(1)
                if not name:
                    head = self._ACCESSOR_NAME.search(source[:match.start()])
                    if head:
                        name = head.group(1)
                found.append({"name": name, "raw": raw, "kind": "css",
                              "value": "", "approx": False,
                              "selector": self.normalize_selector(raw) or ""})

        for pattern, kind in self._GETBY_PATTERNS:
            for match in pattern.finditer(source):
                groups = match.groupdict()
                value = groups.get("val") or groups.get("role") or ""
                if not value:
                    continue
                name = groups.get("name") or ""
                if not name:
                    field = self._FINDBY_FIELD.search(source[max(0, match.start() - 200):match.start()])
                    if field:
                        name = field.group(1)
                entry = {"name": name, "kind": kind, "value": value,
                         "approx": kind in ("role", "text", "label"),
                         "selector": "", "accessible_name": ""}
                if kind == "role":
                    tail = source[match.end():match.end() + 300]
                    name_match = self._SET_NAME.search(tail)
                    entry["accessible_name"] = _unescape(name_match.group("val")) if name_match else ""
                    entry["selector"] = self._ROLE_TAGS.get(value.upper(), "")
                    entry["raw"] = (f"getByRole({value}"
                                    + (f', name="{entry["accessible_name"]}"' if entry["accessible_name"] else "")
                                    + ")")
                elif kind == "testid":
                    entry["selector"] = f"[data-testid='{value}']"
                    entry["raw"] = f'getByTestId("{value}")'
                elif kind == "placeholder":
                    entry["selector"] = f"[placeholder='{value}']"
                    entry["raw"] = f'getByPlaceholder("{value}")'
                else:
                    entry["accessible_name"] = value
                    entry["raw"] = f'getBy{kind.capitalize()}("{value}")'
                
                if entry["raw"] in seen:
                    continue
                seen.add(entry["raw"])
                found.append(entry)
        return found

    def quote_css_value(self, value: str) -> str:
        text = value or ""
        if "'" not in text:
            return "'" + text + "'"
        return '"' + text.replace('"', '\\"') + '"'

    def build_has_text_selector(self, anchor: str, text: str, tag: str) -> str:
        return f'{anchor}:has-text({self.quote_css_value(text)}) {tag}'

    def map_role(self, role: str) -> Optional[str]:
        return self._ROLE_TO_JAVA.get((role or "").lower())

    def emit_locator(self, **kwargs) -> Dict[str, str]:
        def _q(s: str) -> str:
            return '"' + (s or "").replace('\\', '\\\\').replace('"', '\\"') + '"'

        if "testid" in kwargs:
            return {
                "python": f"page.get_by_test_id({_q(kwargs['testid'])})",
                "java": f"page.getByTestId({_q(kwargs['testid'])})"
            }
        if "role" in kwargs:
            jrole = kwargs["role"]
            name = kwargs.get("name", "")
            exact = kwargs.get("exact", False)
            if exact:
                return {
                    "python": f"page.get_by_role({_q(jrole.lower())}, name={_q(name)}, exact=True)",
                    "java": (f"page.getByRole(AriaRole.{jrole}, new Page.GetByRoleOptions()"
                             f".setName({_q(name)}).setExact(true))")
                }
            return {
                "python": f"page.get_by_role({_q(jrole.lower())}, name={_q(name)})",
                "java": f"page.getByRole(AriaRole.{jrole}, new Page.GetByRoleOptions().setName({_q(name)}))"
            }
        if "placeholder" in kwargs:
            return {
                "python": f"page.get_by_placeholder({_q(kwargs['placeholder'])})",
                "java": f"page.getByPlaceholder({_q(kwargs['placeholder'])})"
            }
        if "label" in kwargs:
            return {
                "python": f"page.get_by_label({_q(kwargs['label'])})",
                "java": f"page.getByLabel({_q(kwargs['label'])})"
            }
        if "text" in kwargs:
            text = kwargs["text"]
            exact = kwargs.get("exact", False)
            if exact:
                return {
                    "python": f"page.get_by_text({_q(text)}, exact=True)",
                    "java": f"page.getByText({_q(text)}, new Page.GetByTextOptions().setExact(true))"
                }
            return {
                "python": f"page.get_by_text({_q(text)})",
                "java": f"page.getByText({_q(text)})"
            }
        if "selector" in kwargs:
            return {
                "python": f"page.locator({_q(kwargs['selector'])})",
                "java": f"page.locator({_q(kwargs['selector'])})"
            }
        return {"python": "", "java": ""}


class PlaywrightMCPProvider(MCPProvider):
    def get_server_config(self, project_root: Path, headless: Optional[bool] = None, cdp_endpoint: Optional[str] = None, storage_state: Optional[str] = None) -> Dict:
        version = os.environ.get("PLAYWRIGHT_MCP_VERSION", "0.0.79")
        command = f"@playwright/mcp@{version}"
        
        if cdp_endpoint:
            args = [command, "--cdp-endpoint", str(cdp_endpoint)]
            return {"mcpServers": {"playwright": {"command": "npx", "args": args}}}
            
        args = [command, "--isolated", "--viewport-size=1920,1080"]
        
        from shared import browser_mode
        if headless is None:
            headless = browser_mode.headless()
        if headless:
            args.append("--headless")
        if storage_state:
            args.extend(["--storage-state", str(storage_state)])
            
        return {
            "mcpServers": {
                "playwright": {
                    "command": "npx",
                    "args": args,
                }
            }
        }

    def allowed_tools(self) -> List[str]:
        return ["mcp__playwright__*"]


class PlaywrightPlugin(FrameworkPlugin):
    def __init__(self):
        self._telemetry = PlaywrightTelemetryParser()
        self._runner = PlaywrightTestRunner()
        self._diagnostics = PlaywrightDiagnosticEngine()
        self._code = PlaywrightCodeEngine()
        self._mcp = PlaywrightMCPProvider()

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
