"""Shared Claude CLI invocation for all QA-Agent-Network steps.

Upgrade over the original: uses subprocess.Popen + two daemon threads to stream
stdout/stderr in real time rather than buffering until Claude exits.

Backward-compatible: existing callers (prompt, model, cwd, timeout=N) need no changes.
New optional kwargs: on_output, system_prompt_file, log_dir.
"""

import os
import shlex
import subprocess
import threading
from datetime import datetime
from pathlib import Path


def _stream_pipe(pipe, label: str, chunks: list, log_file, on_output=None) -> None:
    """Read lines from pipe, append to chunks, log, and call on_output callback."""
    for line in iter(pipe.readline, ""):
        chunks.append(line)
        if log_file:
            log_file.write(f"[{label}] {line}")
            log_file.flush()
        if on_output:
            on_output(label, line.rstrip())
    pipe.close()


def call_claude(
    prompt: str,
    model: str,
    cwd: str,
    timeout: int = 300,
    on_output=None,
    system_prompt_file=None,
    log_dir=None,
) -> str:
    """Call `claude -p <prompt> --model <model>` as a subprocess.

    Returns stdout on success, empty string on error.
    cwd should be the repo root so relative paths in prompts resolve correctly.

    Optional args (all default to None for full backward compatibility):
      on_output(label, line)  — called for each streamed output line
      system_prompt_file      — path passed as --system-prompt-file to claude CLI
      log_dir                 — directory to write a timestamped claude-*.log file
    """
    claude_cli = os.environ.get("CLAUDE_CLI_PATH", "claude")
    cmd = [claude_cli, "-p", prompt, "--model", model]
    if system_prompt_file:
        cmd.extend(["--system-prompt-file", str(system_prompt_file)])

    # Resolve log path if requested
    log_path = None
    if log_dir:
        log_dir_path = Path(log_dir)
        log_dir_path.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        log_path = log_dir_path / f"claude-{ts}.log"

    stdout_chunks: list = []
    stderr_chunks: list = []

    def _run(log_file):
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd),
            bufsize=1,
        )
        if log_file:
            log_file.write(f"cwd: {cwd}\ncommand: {shlex.join(cmd)}\n\n")
            log_file.flush()

        t_out = threading.Thread(
            target=_stream_pipe,
            args=(proc.stdout, "stdout", stdout_chunks, log_file, on_output),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_stream_pipe,
            args=(proc.stderr, "stderr", stderr_chunks, log_file, on_output),
            daemon=True,
        )
        t_out.start()
        t_err.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        t_out.join(timeout=2)
        t_err.join(timeout=2)
        return proc.returncode

    if log_path:
        with log_path.open("w", encoding="utf-8") as log_file:
            returncode = _run(log_file)
    else:
        returncode = _run(None)

    stdout = "".join(stdout_chunks)
    if returncode != 0:
        return ""
    if not stdout.strip():
        return ""
    return stdout
