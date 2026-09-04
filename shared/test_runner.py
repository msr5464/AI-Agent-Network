"""Run a single test in the automation repo.

Shared so the reproduce step and the fix step can never disagree about how a
test is invoked — if they did, a fix could be "verified" by a different command
than the one that produced the failure, and the verification would prove nothing.

The three-state return is the important part. "unverified" means no runner could
be found: the change was applied but nothing executed it, and that must never be
reported as a pass.
"""

import os
import re
import signal
import time
import subprocess
import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from shared import browser_mode, narration

DEFAULT_TIMEOUT_S = int(os.environ.get("AUTOFIX_TEST_TIMEOUT_S", "300"))

# Never treated as a candidate module root when looking one level down.
_NON_MODULE_DIRS = {"target", "build", "node_modules", "test-output", "venv", ".venv"}

NO_RUNNER_MESSAGE = (
    "No test runner detected in the workspace and TEST_RUNNER_CMD is not set — "
    "the change was applied but could NOT be verified. Set TEST_RUNNER_CMD to "
    "enable verification."
)


def split_test_name(test_name: str) -> Tuple[str, str, str]:
    """Split a test name into (fully_qualified_class, simple_class, method).

    Accepts `pkg.Class.method`, `Class.method`, `pkg.Class#method`,
    `Class#method`, and a bare `Class` (method comes back empty).
    """
    name = (test_name or "").strip()
    if "#" in name:
        class_part, _, method = name.partition("#")
        class_part = class_part.strip()
        return class_part, class_part.split(".")[-1], method.strip()

    parts = [p for p in name.split(".") if p]
    if not parts:
        return "", "", ""

    # A trailing segment starting lowercase is a method; `pkg.Class` is not.
    if len(parts) >= 2 and parts[-1][:1].islower():
        full_class = ".".join(parts[:-1])
        return full_class, full_class.split(".")[-1], parts[-1]

    full_class = ".".join(parts)
    return full_class, parts[-1], ""


def _as_properties(extra_properties: Optional[Dict[str, str]]) -> List[str]:
    return [f"-D{key}={value}" for key, value in (extra_properties or {}).items()]


def _apply_browser_mode(cmd: List[str],
                        properties: Dict[str, str]) -> Tuple[List[str], Dict[str, str]]:
    """Make PLAYWRIGHT_HEADLESS reach the browser this runner is about to launch.

    The test run is where most of a session's browser time is actually spent —
    reproduce, verify, probe — and none of it honoured the switch before, so
    setting it headed showed you the DOM-inspection browser and hid every run
    that mattered.

    How it is expressed depends on the runner: a JVM build takes `-Dheadless`,
    which the framework reads ahead of parameters/config.properties, while
    `npx playwright test` has no such property and spells it `--headed`. When
    the switch is unset, nothing is added and each runner keeps the default it
    had before — for Maven that is the framework's own config file, which is
    the one place a sensible answer already lives.
    """
    decided = browser_mode.configured()
    if decided is None or "headless" in properties:
        return cmd, properties          # unset, or the caller was explicit
    runner = " ".join(cmd[:3]).lower()
    if any(tool in runner for tool in ("mvn", "maven", "gradle")):
        properties = {**properties, **browser_mode.maven_properties()}
    elif "playwright" in runner and not decided and "--headed" not in cmd:
        cmd = cmd + ["--headed"]
    return cmd, properties


def detect_test_command(workspace: Path, class_simple: str, method: str,
                        log: Callable[[str], None] = lambda _m: None) -> List[str]:
    """Find a runner at the repo root or one level down (multi-module layouts)."""
    # With no method, run the whole class.
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

    # Multi-module repo: the build file may live one directory down. Without
    # this, such a layout silently reported every fix as verified.
    try:
        children = sorted(p for p in workspace.iterdir() if p.is_dir())
    except OSError:
        return []
    for child in children:
        if child.name.startswith(".") or child.name in _NON_MODULE_DIRS:
            continue
        cmd = build_cmd(child)
        if cmd:
            log(f"  Test runner found in submodule {child.name}")
            return cmd
    return []


def run_test(test_name: str, workspace: Path,
             extra_properties: Optional[Dict[str, str]] = None,
             timeout_s: int = DEFAULT_TIMEOUT_S,
             log: Callable[[str], None] = lambda _m: None) -> Tuple[str, str]:
    """Run one test (or a whole class). Returns (status, output).

    status is "passed", "failed" or "unverified".

    extra_properties become -Dkey=value flags. The framework's
    Config.getRunTimeProperty checks System.getProperty first, so this is how the
    reproduce step turns on tracing and repair mode for the run it triggers.

    `headless` is added from PLAYWRIGHT_HEADLESS unless the caller passed its own
    — see `_apply_browser_mode`.
    """
    full_class, class_simple, method = split_test_name(test_name)

    test_runner_cmd = os.environ.get("TEST_RUNNER_CMD", "")
    if test_runner_cmd:
        expanded = (test_runner_cmd
                    .replace("{test_name}", test_name)
                    .replace("{class}", full_class)
                    .replace("{class_simple}", class_simple)
                    .replace("{method}", method))
        cmd = expanded.split()
    else:
        cmd = detect_test_command(workspace, class_simple, method, log)

    if not cmd:
        return "unverified", NO_RUNNER_MESSAGE

    cmd, properties = _apply_browser_mode(cmd, dict(extra_properties or {}))
    cmd = cmd + _as_properties(properties)

    try:
        return _run_streaming(cmd, workspace, timeout_s, log)
    except FileNotFoundError:
        return "unverified", f"Test runner not found on PATH: {cmd[0]}"
    except Exception as e:
        return "failed", f"Test runner error: {e}"


# Maven's own scaffolding. Dropping -q so the live console shows a running build
# also poured this into the captured output — and that output is what classifies
# the failure and what the model is shown (prev_test_output is the FIRST 1500
# chars of it), so the real stack trace would have been pushed out by
# "Scanning for projects...". Streamed in full, filtered out of what we keep.
_BUILD_NOISE = re.compile(
    r"^\[INFO\]\s*(-{3,}|={3,}|$)"                      # separators / blank
    r"|^\[INFO\]\s*(Scanning for projects|Building |BUILD |Total time|Finished at"
    r"|Downloading |Downloaded |Copying |Using .platform encoding|skip non existing"
    r"|Nothing to compile|Compiling |Changes detected|--- .* ---|Reactor Summary)"
    r"|^\[INFO\]\s*T E S T S"
    r"|^Picked up JAVA_TOOL_OPTIONS"
)


def _keep_for_capture(line: str) -> bool:
    """Is this line worth keeping in the output we classify and prompt with?"""
    return not _BUILD_NOISE.match(line)


def _run_streaming(cmd: List[str], workspace: Path, timeout_s: int,
                   log: Callable[[str], None]) -> Tuple[str, str]:
    """Run cmd, emitting each line through `log` as it arrives.

    Buffering the whole build and printing it at the end meant the live console
    showed "Running the test…" and then, minutes later, the verdict — with the
    entire maven run invisible in between. The output is still returned in full
    (tail-capped) for the prompt and the audit trail.
    """
    # Own process group: maven forks a JVM for surefire, and that child inherits
    # the stdout pipe. Killing only the maven process leaves the pipe open, so the
    # read loop below blocks long past the timeout — measured at the full sleep
    # duration rather than the 2s budget. Killing the group closes it.
    proc = subprocess.Popen(
        cmd, cwd=str(workspace),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True,
    )

    # A build that hangs without printing anything would never reach a deadline
    # check inside the read loop, so the timeout is enforced from the outside.
    timed_out = threading.Event()

    def _kill_child_group(sig=signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except OSError:
                pass

    def _kill_on_timeout():
        timed_out.set()
        _kill_child_group()

    watchdog = threading.Timer(timeout_s, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()

    # The build runs in its OWN process group (above), which means a signal sent
    # to THIS step's group — what the server does when it shuts a run down —
    # would not reach maven, leaving the build and its JVM orphaned. So relay it:
    # when we are asked to stop, take the build down with us before exiting.
    def _relay(signum, _frame):
        _kill_child_group()
        signal.signal(signum, prior.get(signum, signal.SIG_DFL))
        os.kill(os.getpid(), signum)

    prior = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prior[sig] = signal.getsignal(sig)
            signal.signal(sig, _relay)
        except ValueError:
            pass        # not the main thread; the watchdog still covers timeouts

    # Bracket the build so a viewer can fold it. Everything is still streamed —
    # the markers only say where the build starts and ends, so a UI can collapse
    # it by default and a plain terminal still reads fine.
    started = time.time()
    log(f"[build:start] {' '.join(cmd)}")

    lines: List[str] = []
    try:
        for line in proc.stdout:
            # The framework decorates some lines with HTML for the Studio UI.
            # Only that one consumer renders it; a terminal and a model prompt
            # both get raw markup, so render it here instead — once, for
            # everyone, before the line is shown or kept.
            line = narration.plain(line.rstrip("\n"))
            # Everything is shown live; only the signal is retained.
            log(line)
            if _keep_for_capture(line):
                lines.append(line)
    finally:
        proc.stdout.close()
        returncode = proc.wait()
        watchdog.cancel()
        for sig, handler in prior.items():
            try:
                signal.signal(sig, handler)
            except ValueError:
                pass

    output = "\n".join(lines)[-8000:]
    verdict = "timed out" if timed_out.is_set() else ("passed" if returncode == 0 else "failed")
    _elapsed = time.time() - started
    log(f"[build:end] {verdict} in {int(_elapsed)}s")
    try:
        from shared import metrics
        metrics.record_tool("build", " ".join(cmd), _elapsed, verdict)
    except Exception:
        pass
    if timed_out.is_set():
        return "failed", f"Test timed out after {timeout_s}s\n{output}"
    return ("passed" if returncode == 0 else "failed"), output
