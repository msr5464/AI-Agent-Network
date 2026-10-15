"""A stalled stream is sent again rather than waited out (restart_if_slow).

Observed: single calls that normally stream at ~107 tokens/s ran at 3-13 tokens/s
for five to eleven minutes, and the same request sent again ran normally.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared import claude  # noqa: E402

FAKE_CLI = """#!{python}
import json, os, sys, time
def out(ev):
    print(json.dumps(ev), flush=True)
out({{"type": "system", "subtype": "init", "model": "m", "tools": []}})
marker = os.environ["FAKE_MARKER"]
first = not os.path.exists(marker)
open(marker, "a").write("x")
if os.environ["FAKE_MODE"] == "stall" and first:
    time.sleep(30)
    sys.exit(0)
if os.environ["FAKE_MODE"] == "steady":
    for _ in range(40):
        out({{"type": "stream_event", "event": {{"type": "content_block_delta",
             "delta": {{"type": "text_delta", "text": "x" * 100}}}}}})
        time.sleep(0.1)
out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "done"}}]}}}})
out({{"type": "result", "result": "done", "num_turns": 1}})
"""


def _call(tmp_path, monkeypatch, mode):
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI.format(python=sys.executable))
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CLI_PATH", str(cli))
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "calls"))
    monkeypatch.setenv("FAKE_MODE", mode)
    monkeypatch.setattr(claude, "SLOW_WINDOW_S", 2)
    seen = []
    began = time.monotonic()
    result = claude.call_claude_ex(prompt="p", model="m", cwd=str(tmp_path), timeout=20,
                                   stream_json=True, restart_if_slow=True,
                                   on_output=lambda label, line: seen.append(line))
    return result, seen, time.monotonic() - began, (tmp_path / "calls").read_text()


def test_a_stalled_stream_is_sent_again_once(tmp_path, monkeypatch):
    result, seen, took, calls = _call(tmp_path, monkeypatch, "stall")
    assert result.stdout == "done" and result.status == "ok"
    assert result.slow_restarts == 1 and calls == "xx"
    assert any(line.startswith("API retry — stream too slow") for line in seen)
    assert took < 15, "the stall is cut short, not waited out"


def test_a_steady_stream_is_left_alone(tmp_path, monkeypatch):
    result, _, _, calls = _call(tmp_path, monkeypatch, "steady")
    assert result.stdout == "done" and result.slow_restarts == 0 and calls == "x"


def test_each_call_asks_for_exactly_one_output_format(tmp_path, monkeypatch):
    """Observed: adding the watchdog's flag attached the json branch to it, so a
    streamed call without the watchdog was sent `--output-format stream-json` AND
    `--output-format json`. The last one won, the browser step's events arrived as
    one JSON array the stream decoder could not read, and a run that drove all 17
    steps was reported as never having touched the browser."""
    from unittest import mock
    import io
    for stream, watched, fmt in ((True, True, "stream-json"), (True, False, "stream-json"),
                                 (False, False, "json")):
        with mock.patch("shared.claude.subprocess.Popen") as popen:
            popen.return_value.stdout = io.StringIO()
            popen.return_value.stderr = io.StringIO()
            popen.return_value.wait.return_value = 0
            popen.return_value.poll.return_value = 0
            popen.return_value.returncode = 0
            claude.call_claude_ex(prompt="p", model="m", cwd=".", timeout=1,
                                  stream_json=stream, restart_if_slow=watched)
            cmd = popen.call_args[0][0]
            formats = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--output-format"]
            assert formats == [fmt], (stream, watched, formats)
            assert ("--include-partial-messages" in cmd) is (stream and watched)
