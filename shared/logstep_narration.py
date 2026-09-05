"""Whether a generated test narrates its steps, or summarises them in one line.

`logStep` is the only thing that turns a test run into a readable report: the
report shows one line per step, and a failure is located by reading the last step
that printed. A test that opens with

    config.logStep("Login to Naukri, toggle the trailing dot in Profile Summary,
                    save the change, and verify it persists after page reload");

satisfies every existing check — logStep is present, it is in a test class, the
text is plain English — and still produces a one-line report for a four-step
scenario. When it fails, the report says the whole test failed and nothing about
where. The derived intent contract has the same problem: it is built from these
strings, so one run-on sentence collapses four verifiable steps into one blob
that the next adaptation cannot check anything against.

So presence is not the property worth checking; *granularity* is. Two signals
bound it from opposite sides:

  * the plan already enumerates this method's steps, and the narration should not
    be coarser than the plan the test was generated from;
  * a method cannot narrate more groups than it has statements that actually do
    something, so the count of acting statements caps what can fairly be asked.

The expectation is the smaller of the two. Asking for more than either would be
a guard that fires on correct code, which is worse than not checking at all.
"""

from __future__ import annotations

import re
from typing import Dict, List

from shared.code_analyzer import split_class_members, without_comments

_TEST_ANNOTATION = re.compile(r"@Test\b")

# Both call shapes the repos use: `logStep(testConfig, "…")` as CONVENTIONS.md
# writes it, and `config.logStep("…")` as the Playwright framework does.
LOG_STEP = re.compile(
    r"\blogStep\s*\(\s*(?:\w+\s*,\s*)?\"((?:\\.|[^\"\\])*)\"")

# Any logging call. A log line is narration *about* work, never the work itself,
# so it must not count towards the acting statements that justify narration.
_LOG_CALL = re.compile(
    r"\blog(?:Step|Comment|Pass|Fail\w*|Warning|Exception|Info)\s*\(")

_ASSERT_CALL = re.compile(
    r"\b(?:AssertHelper\.\w+|assert[A-Z]\w*|verify[A-Z]\w*)\s*\(")

_CALL_ON_RECEIVER = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*(\w+)\s*\(")

# Receivers whose calls are plumbing, not steps: reading a property, waiting for
# the page to settle, formatting a string. Narrating these would produce a report
# full of lines nobody reads, which is the opposite failure to the one this
# module exists for.
_PLUMBING_RECEIVERS = {
    "config", "testConfig", "cfg", "conf",
    "WaitHelper", "System", "Objects", "String", "Integer", "Long", "Double",
    "Boolean", "Math", "Arrays", "Collections", "List", "Map", "Optional",
    "Thread", "Files", "Paths", "Duration", "LocalDate", "LocalDateTime",
}

# A plan step that only sets the test up. It has nothing to show in a report —
# no user did it, and no failure of it is interesting on its own — so it neither
# needs a logStep nor counts towards the expected number.
_SETUP_STEP = re.compile(
    r"^(?:allocate|build|construct|instantiate|initiali[sz]e|prepare|declare"
    r"|set\s*auth\s*token|setauthtoken|read|load|resolve|obtain|get)\b"
    r"|^\s*create\s+\w*data\b",
    re.IGNORECASE)

# `... assertEquals status PENDING  [source: user]` — provenance tagging added by
# 01_parse.py, not part of the step text.
_SOURCE_TAG = re.compile(r"\s*\[source:[^\]]*\]\s*$", re.IGNORECASE)

# Below two steps there is nothing to split, and "one logStep for a one-step
# test" is the correct shape rather than a finding.
MIN_EXPECTED = 2


def _blank_strings(text: str) -> str:
    """String literals blanked to spaces, keeping length so offsets still line up."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if text[i] == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if text[i] == quote:
                    out.append(" ")
                    i += 1
                    break
                out.append(" " if text[i] != "\n" else "\n")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _body_of(member_text: str) -> str:
    """The statements inside a method, without its signature or annotations."""
    open_brace = member_text.find("{")
    if open_brace < 0:
        return ""
    close = member_text.rfind("}")
    return member_text[open_brace + 1:close if close > open_brace else None]


def test_bodies(source: str) -> Dict[str, str]:
    """{method name: body} for every @Test method in one Java source, in order."""
    bodies: Dict[str, str] = {}
    for member in split_class_members(source or ""):
        if member["kind"] != "method" or not member["name"]:
            continue
        text = without_comments(member["text"])
        if _TEST_ANNOTATION.search(text):
            bodies[member["name"]] = _body_of(text)
    return bodies


def log_steps(body: str) -> List[str]:
    """The logStep narration in a method body, in call order."""
    return LOG_STEP.findall(body or "")


def acting_statements(body: str) -> List[str]:
    """Statements that drive the app or check it — the things worth narrating.

    Reading a property, constructing a helper and slicing a returned array are
    not among them: they are how the test is wired, not what it does.
    """
    blanked = _blank_strings(body or "")
    acting = []
    for start, end in _statement_spans(blanked):
        skeleton = blanked[start:end]
        if _LOG_CALL.search(skeleton):
            continue
        if _ASSERT_CALL.search(skeleton):
            acting.append(body[start:end].strip())
            continue
        for receiver, _callee in _CALL_ON_RECEIVER.findall(skeleton):
            if receiver not in _PLUMBING_RECEIVERS:
                acting.append(body[start:end].strip())
                break
    return acting


def _statement_spans(blanked: str) -> List[tuple]:
    """(start, end) of each `;`-terminated statement, braces included as breaks.

    Runs over the string-blanked copy, so a `;` inside a literal cannot split a
    statement in half. Control-flow braces end a statement too, which keeps an
    `if (…) { click(…); }` from reading as one giant statement.
    """
    spans = []
    start = 0
    for i, ch in enumerate(blanked):
        if ch in ";{}":
            if blanked[start:i].strip():
                spans.append((start, i))
            start = i + 1
    if blanked[start:].strip():
        spans.append((start, len(blanked)))
    return spans


def narratable_steps(steps: List) -> List[str]:
    """Plan steps that a report reader would expect to see, setup dropped."""
    out = []
    for step in steps or []:
        text = step.get("description", "") if isinstance(step, dict) else str(step)
        text = _SOURCE_TAG.sub("", text or "").strip()
        if not text or _SETUP_STEP.search(text):
            continue
        out.append(text)
    return out


def expected_from_plan(plan: dict) -> Dict[str, List[str]]:
    """{test method name: the steps it should narrate}, from the parse plan.

    Covers all three shapes 01_parse.py can produce: separate api/web method
    lists, and the single interleaved method whose steps live in their own key.
    """
    expected: Dict[str, List[str]] = {}
    for key in ("api_test_methods", "web_test_methods"):
        for method in plan.get(key) or []:
            name = (method or {}).get("method_name")
            if name:
                expected[name] = narratable_steps(method.get("steps"))
    if plan.get("flow_style") == "interleaved":
        name = plan.get("interleaved_test_method_name")
        if name:
            expected[name] = narratable_steps(plan.get("interleaved_steps"))
    return expected


def audit(source: str, expected: Dict[str, List[str]]) -> Dict[str, dict]:
    """Test methods narrated more coarsely than their own plan and code allow.

    Returns {method: {"log_steps", "expected", "acting", "steps", "narration"}}
    for the methods that fall short — empty when the file is fine.
    """
    findings: Dict[str, dict] = {}
    for name, body in test_bodies(source).items():
        narration = log_steps(body)
        acting = acting_statements(body)
        steps = expected.get(name)
        # No plan entry (a method the model named differently, or an existing
        # method being extended): fall back to the floor — one logStep cannot
        # narrate several acting statements.
        want = min(len(steps), len(acting)) if steps else min(len(acting), MIN_EXPECTED)
        if want < MIN_EXPECTED or len(narration) >= want:
            continue
        findings[name] = {
            "log_steps": len(narration),
            "expected": want,
            "acting": len(acting),
            "steps": steps or [],
            "narration": narration,
        }
    return findings
