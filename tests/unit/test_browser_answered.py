"""A browser step that no browser ever answered is a stop, not a short run.

A Playwright MCP server can connect and still be unable to launch its browser —
one whose browser build is not installed fails every call with "is not installed".
The explorer then reported "0 step(s), status empty", closed the step with a ✓
and moved the change note to processed/. It also had the built-in tools loaded,
so it spent its turns trying to install the browser from Bash.
"""

import ast
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.claude import ClaudeResult, _StreamJsonDecoder, call_claude_ex  # noqa: E402

NOT_INSTALLED = ('Error: Browser "chrome-for-testing" is not installed; expected '
                 'executable at /x/chromium-1247. Run `npx @playwright/mcp '
                 'install-browser chrome-for-testing` to install')


def _use(tool_id, name):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": {}}]}})


def _result(tool_id, content, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
    if is_error:
        block["is_error"] = True
    return json.dumps({"type": "user", "message": {"content": [block]}})


def _decode(*lines):
    decoder = _StreamJsonDecoder()
    for line in lines:
        decoder.feed(line)
    return decoder


class TestDecoder:
    def test_a_browser_that_cannot_launch(self):
        d = _decode(
            _use("t1", "ToolSearch"), _result("t1", [{"type": "tool_reference"}]),
            _use("t2", "mcp__playwright__browser_run_code_unsafe"),
            _result("t2", "### Error\n" + NOT_INSTALLED, is_error=True),
            _use("t3", "Bash"), _result("t3", "This command requires approval", is_error=True))
        assert (d.browser_calls, d.browser_ok) == (1, 0)
        assert d.browser_error.startswith('Error: Browser "chrome-for-testing" is not installed')

    def test_a_browser_that_answered(self):
        d = _decode(
            _use("t1", "mcp__playwright__browser_navigate"),
            _result("t1", [{"type": "text", "text": "### Page\n- Page URL: https://x/"}]),
            # An action that fails inside page.qa.step still came back from the browser.
            _use("t2", "mcp__playwright__browser_click"),
            _result("t2", "### Error\nref e12 not found", is_error=True))
        assert (d.browser_calls, d.browser_ok) == (2, 1)
        assert d.browser_error == "ref e12 not found"

    def test_only_browser_tools_count(self):
        d = _decode(_use("t1", "ToolSearch"), _result("t1", "ok"),
                    _use("t2", "Read"), _result("t2", "file"))
        assert (d.browser_calls, d.browser_ok) == (0, 0)

    def test_error_text_in_blocks(self):
        d = _decode(_use("t1", "mcp__pw__browser_snapshot"),
                    _result("t1", [{"type": "text", "text": "### Error\nboom"}], is_error=True))
        assert d.browser_error == "boom"


def _claude(**kw):
    return ClaudeResult(stdout="", stderr="", returncode=0, status="ok",
                        timed_out=False, duration_s=1.0, **kw)


class TestBrowserUnavailable:
    def test_not_counted_is_not_a_verdict(self):
        assert _claude().browser_unavailable() == ""

    def test_never_called(self):
        assert "never called a browser tool" in _claude(
            browser_calls=0, browser_ok=0).browser_unavailable()

    def test_every_call_failed(self):
        reason = _claude(browser_calls=3, browser_ok=0,
                         browser_error=NOT_INSTALLED).browser_unavailable()
        assert reason.startswith("all 3 browser call(s) failed")
        assert "install-browser" in reason

    def test_one_answer_is_enough(self):
        assert _claude(browser_calls=5, browser_ok=1).browser_unavailable() == ""


def test_counts_reach_the_result():
    """Through call_claude_ex itself, as a streaming run produces them."""
    stream = "\n".join([
        _use("t1", "mcp__playwright__browser_run_code_unsafe"),
        _result("t1", "### Error\n" + NOT_INSTALLED, is_error=True),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I cannot proceed."}]}}),
    ]) + "\n"
    with mock.patch("shared.claude.subprocess.Popen") as popen:
        popen.return_value.stdout = io.StringIO(stream)
        popen.return_value.stderr = io.StringIO()
        popen.return_value.wait.return_value = 0
        popen.return_value.poll.return_value = 0
        popen.return_value.returncode = 0
        result = call_claude_ex(prompt="p", model="m", cwd=".", timeout=5, stream_json=True)
    assert (result.browser_calls, result.browser_ok) == (1, 0)
    assert "is not installed" in result.browser_unavailable()


def _load_explore(tmp_path, monkeypatch):
    """Load 03_explore_web.py by path, leaving sys.path and `lib` as they were
    (see test_adapt_prompt._load_adapt)."""
    monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    is_lib = lambda n: n == "lib" or n.startswith("lib.")
    for name in [n for n in sys.modules if is_lib(n)]:
        monkeypatch.delitem(sys.modules, name)
    spec = importlib.util.spec_from_file_location(
        "explore_web_03", ROOT / "agents/test-adaptation-agent/actions/03_explore_web.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in [n for n in sys.modules if is_lib(n)]:
        del sys.modules[name]
    return module


def test_explorer_stops_as_infra_when_no_browser_answered(tmp_path, monkeypatch):
    explore = _load_explore(tmp_path, monkeypatch)
    (tmp_path / "01-parse-change.json").write_text(json.dumps(
        {"type": "web", "module": "shop", "items": []}))
    (tmp_path / "02-scope.json").write_text(json.dumps(
        {"workspace": str(tmp_path), "entry_path": {"mode": "none"}}))
    calls = []

    def run_attempt(*args, **kwargs):
        calls.append(1)
        return ({"steps": [], "pages": {}, "status": "empty"},
                _claude(browser_calls=1, browser_ok=0, browser_error=NOT_INSTALLED))

    monkeypatch.setattr(explore, "run_attempt", run_attempt)
    with pytest.raises(SystemExit) as stop:
        explore.main()
    assert stop.value.code == 1
    assert len(calls) == 1                        # the same browser would fail a retry
    written = json.loads((tmp_path / "03-explore-web.json").read_text())
    assert written["status"] == "skipped" and written["ran"] is False
    assert "is not installed" in written["reason"]
    assert (tmp_path / ".skip-reason").read_text() == "infra"


def _mcp_calls():
    """Every call in an agent step that hands the model an MCP server."""
    for path in sorted(ROOT.glob("agents/*/actions/*.py")):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Call) and any(k.arg == "mcp_config" for k in node.keywords):
                yield path.relative_to(ROOT), node


def test_every_browser_call_loads_no_builtin_tools():
    """allowed_tools only gates permission; the user's own allow rules still let
    Read and Bash through. A browser step gets the browser and nothing else."""
    found = list(_mcp_calls())
    assert len(found) >= 4, "the browser calls moved — update this guard"
    for path, node in found:
        kw = {k.arg: k.value for k in node.keywords}
        where = f"{path}:{node.lineno}"
        assert isinstance(kw.get("tools"), ast.Constant) and kw["tools"].value == "", where
        assert (isinstance(kw.get("disable_slash_commands"), ast.Constant)
                and kw["disable_slash_commands"].value is True), where
