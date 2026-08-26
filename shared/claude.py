"""Shared Claude CLI invocation for all QA-Agent-Network steps.

Uses subprocess.Popen + two daemon threads to stream stdout/stderr in real time
rather than buffering until Claude exits.

Two entry points:
  call_claude(...)     — legacy string API. Returns stdout, or "" on any failure.
  call_claude_ex(...)  — returns a ClaudeResult carrying *why* a call produced no
                         output, plus any partial output recovered from a timeout.

Optional kwargs: on_output, system_prompt_file, log_dir, allowed_tools, add_dir,
stream_json, partial_on_timeout.
"""

import json
import os
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple


# ── Result type ────────────────────────────────────────────────────────────────

class ClaudeResult(NamedTuple):
    """Outcome of one `claude -p` invocation.

    status distinguishes the three very different reasons stdout can come back
    empty, which the bare-string API cannot express:
      "ok"      — exited 0 with usable output
      "timeout" — killed at the `timeout` mark; `stdout` holds partial output
      "error"   — exited non-zero (see returncode / stderr)
      "empty"   — exited 0 but produced nothing at all
    """
    stdout:     str
    stderr:     str
    returncode: int
    status:     str
    timed_out:  bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def describe(self) -> str:
        """One-line explanation suitable for logging when output is missing/short."""
        if self.status == "ok":
            return f"completed in {self.duration_s:.0f}s"
        if self.status == "timeout":
            return (f"TIMED OUT after {self.duration_s:.0f}s — "
                    f"recovered {len(self.stdout)} chars of partial output")
        if self.status == "error":
            tail = ""
            if self.stderr.strip():
                tail = self.stderr.strip().splitlines()[-1][:200]
            return (f"exited {self.returncode} after {self.duration_s:.0f}s"
                    + (f" — {tail}" if tail else " with no stderr"))
        return f"exited 0 after {self.duration_s:.0f}s but produced no output"


# ── stream-json decoding ───────────────────────────────────────────────────────

def _describe_tool_use(block: dict) -> str:
    """Compact one-line label for a tool_use event, for live progress output."""
    name = block.get("name") or "tool"
    inp = block.get("input")
    if isinstance(inp, dict):
        for key in ("url", "element", "selector", "text", "ref", "command"):
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                return f"{name} — {val.strip()[:90]}"
    return name


class _StreamJsonDecoder:
    """Turns `--output-format stream-json` JSONL events back into assistant text.

    Markers such as STEP_PASSED: are ordinary assistant text, so every downstream
    parser keeps working unchanged — it just receives the reconstructed text
    instead of the CLI's plain text output.
    """

    def __init__(self):
        self.text_parts:  list = []
        self.result_text: str = ""

    def feed(self, raw_line: str) -> list:
        """Consume one JSONL line. Returns progress lines to surface to the caller."""
        line = raw_line.strip()
        if not line:
            return []

        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            # Not JSON (CLI banner, node warning, …) — keep it as plain text so
            # nothing is silently dropped.
            self.text_parts.append(line)
            return [line]

        if not isinstance(ev, dict):
            return []

        etype = ev.get("type")
        progress: list = []

        if etype == "assistant":
            content = (ev.get("message") or {}).get("content") or []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text") or ""
                    if text.strip():
                        self.text_parts.append(text)
                        progress.extend(text.splitlines())
                elif block.get("type") == "tool_use":
                    progress.append(f"→ {_describe_tool_use(block)}")

        elif etype == "result":
            if isinstance(ev.get("result"), str):
                self.result_text = ev["result"]

        elif etype == "system" and ev.get("subtype") == "init":
            # Surfacing MCP connection state here is what makes a genuine
            # "MCP server unavailable" failure distinguishable from a timeout.
            for srv in ev.get("mcp_servers") or []:
                if isinstance(srv, dict):
                    progress.append(
                        f"MCP server '{srv.get('name')}': {srv.get('status')}"
                    )

        return progress

    def text(self) -> str:
        if self.text_parts:
            return "\n".join(self.text_parts)
        # Fall back to the final result blob only when no assistant text arrived.
        # `result` normally repeats the last assistant message, and counting both
        # would duplicate every STEP_PASSED / STEP_FAILED marker downstream.
        return self.result_text


# ── Pipe streaming ─────────────────────────────────────────────────────────────

def _stream_pipe(pipe, label: str, chunks: list, log_file, on_output=None,
                 decoder=None) -> None:
    """Read lines from pipe, append to chunks, log, and call on_output callback."""
    for line in iter(pipe.readline, ""):
        chunks.append(line)
        if log_file:
            log_file.write(f"[{label}] {line}")
            log_file.flush()
        if decoder is not None and label == "stdout":
            for progress_line in decoder.feed(line):
                if on_output:
                    on_output(label, progress_line)
        elif on_output:
            on_output(label, line.rstrip())
    pipe.close()


# ── Entry points ───────────────────────────────────────────────────────────────

def call_claude_ex(
    prompt: str,
    model: str,
    cwd: str,
    timeout: int = 300,
    on_output=None,
    system_prompt_file=None,
    log_dir=None,
    allowed_tools: list = None,
    add_dir: str = None,
    stream_json: bool = False,
    mcp_config=None,
    strict_mcp_config: bool = False,
) -> ClaudeResult:
    """Call `claude -p <prompt> --model <model>` and report the full outcome.

    Unlike call_claude() this never discards work: a run killed at the timeout
    still returns everything Claude produced beforehand, with status="timeout".

    Args beyond the legacy set:
      stream_json       — pass --output-format stream-json so output arrives event
                          by event instead of being buffered until exit. Enables
                          live progress via on_output on long browser-driving runs.
      mcp_config        — path to a .mcp.json to load MCP servers from, passed as
                          --mcp-config.
      strict_mcp_config — ignore user/global MCP configuration and use only the
                          servers in mcp_config. Without mcp_config this loads NO
                          servers at all, so the two are passed together.
    """
    claude_cli = os.environ.get("CLAUDE_CLI_PATH", "claude")
    cmd = [claude_cli, "-p", prompt, "--model", model]
    if system_prompt_file:
        cmd.extend(["--system-prompt-file", str(system_prompt_file)])
    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])
    if add_dir:
        # Adds a working directory. Verified empirically: this is additive, not
        # restrictive — it does NOT confine a granted tool to that directory, and
        # Read reaches absolute paths outside it either way. Do not treat passing
        # this as a sandbox.
        cmd.extend(["--add-dir", str(add_dir)])
    if mcp_config:
        cmd.extend(["--mcp-config", str(mcp_config)])
    if strict_mcp_config:
        # Without this the subprocess also inherits the user's global MCP servers
        # (Google Drive, etc.), paying connection and tool-registry cost on every
        # step that only ever needs the browser.
        cmd.append("--strict-mcp-config")
    if stream_json:
        # --verbose is required by the CLI whenever stream-json is combined with -p.
        cmd.extend(["--output-format", "stream-json", "--verbose"])

    # Resolve log path if requested
    log_path = None
    if log_dir:
        log_dir_path = Path(log_dir)
        log_dir_path.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        log_path = log_dir_path / f"claude-{ts}.log"

    decoder = _StreamJsonDecoder() if stream_json else None
    stdout_chunks: list = []
    stderr_chunks: list = []
    timed_out = {"hit": False}
    started = time.monotonic()

    def _run(log_file):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
            bufsize=1,
            # Own process group so a timeout can take down the MCP servers Claude
            # spawned as well — killing only `claude` leaves them orphaned, holding
            # browsers open across runs.
            start_new_session=True,
        )
        if log_file:
            log_file.write(f"cwd: {cwd}\ncommand: {shlex.join(cmd)}\n\n")
            log_file.flush()

        def _kill_group(sig=signal.SIGKILL):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass

        # Because the child sits in its own session, a killpg() aimed at *our*
        # process group — which is exactly how the server cancels a run — sails
        # straight past it, orphaning Claude, its MCP servers and their browsers
        # onto PID 1. Forward termination signals down to the child group by hand.
        _prev_handlers: dict = {}

        def _forward_signal(signum, _frame):
            _kill_group(signal.SIGTERM)
            deadline = time.monotonic() + 2
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                _kill_group(signal.SIGKILL)
            # Re-raise under the original disposition so our exit status still
            # reflects the signal the supervisor sent.
            signal.signal(signum, _prev_handlers.get(signum, signal.SIG_DFL))
            os.kill(os.getpid(), signum)

        for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                _prev_handlers[_sig] = signal.signal(_sig, _forward_signal)
            except (ValueError, OSError, AttributeError):
                # Not the main thread, or the platform lacks the signal — the
                # finally-block sweep below is still in force.
                pass

        t_out = threading.Thread(
            target=_stream_pipe,
            args=(proc.stdout, "stdout", stdout_chunks, log_file, on_output, decoder),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_stream_pipe,
            args=(proc.stderr, "stderr", stderr_chunks, log_file, on_output, None),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out["hit"] = True
                _kill_group()
                proc.wait()
            except BaseException:
                # Ctrl-C, or anything else unwinding this frame: never leave the
                # child group running behind us.
                _kill_group()
                proc.wait()
                raise

            # Give the readers a moment to drain whatever is still buffered, so
            # partial output survives a timeout kill.
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            return proc.returncode
        finally:
            for _s, _prev in _prev_handlers.items():
                try:
                    signal.signal(_s, _prev)
                except (ValueError, OSError):
                    pass
            # Last line of defence — a child still alive on any exit path here is
            # an orphan in the making.
            if proc.poll() is None:
                _kill_group()

    if log_path:
        with log_path.open("w", encoding="utf-8") as log_file:
            returncode = _run(log_file)
    else:
        returncode = _run(None)

    stdout = decoder.text() if decoder is not None else "".join(stdout_chunks)
    stderr = "".join(stderr_chunks)
    duration = time.monotonic() - started

    if timed_out["hit"]:
        status = "timeout"
    elif returncode != 0:
        status = "error"
    elif not stdout.strip():
        status = "empty"
    else:
        status = "ok"

    return ClaudeResult(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode if returncode is not None else -1,
        status=status,
        timed_out=timed_out["hit"],
        duration_s=duration,
    )


def call_claude(
    prompt: str,
    model: str,
    cwd: str,
    timeout: int = 300,
    on_output=None,
    system_prompt_file=None,
    log_dir=None,
    allowed_tools: list = None,
    add_dir: str = None,
    stream_json: bool = False,
    mcp_config=None,
    strict_mcp_config: bool = False,
    partial_on_timeout: bool = False,
) -> str:
    """Call `claude -p <prompt> --model <model>` as a subprocess.

    Returns stdout on success, empty string on error.
    cwd should be the repo root so relative paths in prompts resolve correctly.

    Optional args (all default to None/False for full backward compatibility):
      on_output(label, line)  — called for each streamed output line
      system_prompt_file      — path passed as --system-prompt-file to claude CLI
      log_dir                 — directory to write a timestamped claude-*.log file
      allowed_tools           — list of tool names/patterns passed as --allowedTools
                                (e.g. ["mcp__playwright__*"] to enable browser control)
      stream_json             — stream events as they happen instead of buffering
      partial_on_timeout      — return whatever arrived before a timeout instead of
                                discarding it. Off by default: callers that
                                json.loads() the output are better served by an
                                obvious empty string than by truncated JSON.

    Callers that need to know *why* the result was empty should use
    call_claude_ex(), which returns a ClaudeResult with a status field.
    """
    result = call_claude_ex(
        prompt=prompt,
        model=model,
        cwd=cwd,
        timeout=timeout,
        on_output=on_output,
        system_prompt_file=system_prompt_file,
        log_dir=log_dir,
        allowed_tools=allowed_tools,
        add_dir=add_dir,
        stream_json=stream_json,
        mcp_config=mcp_config,
        strict_mcp_config=strict_mcp_config,
    )
    if result.status == "ok":
        return result.stdout
    if result.status == "timeout" and partial_on_timeout:
        return result.stdout
    return ""
