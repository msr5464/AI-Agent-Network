"""An MCP-driving call must keep ToolSearch, or the model gets zero usable tools.

MCP tools arrive deferred — the turn carries their names, not their schemas —
so `--tools ''` alongside an --mcp-config leaves the model unable to call
anything, and it narrates a fabricated session instead. See shared/claude.py.
"""
import io
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.claude import call_claude_ex  # noqa: E402


def _cmd_for(**kwargs) -> list:
    with mock.patch("shared.claude.subprocess.Popen") as popen:
        popen.return_value.stdout = io.StringIO()
        popen.return_value.stderr = io.StringIO()
        popen.return_value.wait.return_value = 0
        popen.return_value.poll.return_value = 0
        popen.return_value.returncode = 0
        call_claude_ex(prompt="p", model="m", cwd=".", timeout=1, **kwargs)
        return popen.call_args[0][0]


def _tools_arg(cmd: list) -> str:
    return cmd[cmd.index("--tools") + 1]


def test_mcp_run_keeps_toolsearch():
    assert _tools_arg(_cmd_for(tools="", mcp_config="/x/.mcp.json")) == "ToolSearch"


def test_mcp_run_does_not_duplicate_toolsearch():
    cmd = _cmd_for(tools="ToolSearch,Read", mcp_config="/x/.mcp.json")
    assert _tools_arg(cmd).split(",").count("ToolSearch") == 1


def test_non_mcp_run_still_loads_no_builtins():
    assert _tools_arg(_cmd_for(tools="")) == ""


if __name__ == "__main__":
    test_mcp_run_keeps_toolsearch()
    test_mcp_run_does_not_duplicate_toolsearch()
    test_non_mcp_run_still_loads_no_builtins()
    print("ok")
