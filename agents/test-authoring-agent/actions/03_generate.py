#!/usr/bin/env python3
"""
Step 03 — Generate
Uses Claude to generate all required Java files for the feature module and
writes them directly into the automation repository.

For new modules: creates Data, Builder, Helper, Api enum, option Enums, Page objects,
Test classes. For existing modules: reuses what exists, changes existing methods only
in the small ways the plan's reuse ledger names, and adds what is new.

When plan["flow_style"] == "interleaved" (set by 01_parse.py when a test_type=="both"
input describes ONE sequence mixing real API and web actions, rather than two
independent flows), generates a single combined test class following
plan["interleaved_steps"]'s order instead of separate Api/Web test classes.

Reads:  $AUDIT_DIR/01-parse.json
        $AUDIT_DIR/02-validate-web.json
        $AUDIT_DIR/02-validate-api.json (if present — API validation hints)
Writes: Java files into the automation repo
        $AUDIT_DIR/pre-run/ (existing files as they were)
        $AUDIT_DIR/03-generate.json
        $AUDIT_DIR/03-generate.md
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*

from shared import workspace as workspace_helper
from shared import module_index
from shared.logstep_narration import BODY_LINES_GUIDELINE
from shared.repo_config import load_repo_config

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
AUTOMATION_FRAMEWORK_DIR    = workspace_helper.resolve(
    WORKSPACE_DIR, os.environ.get("GITHUB_REPO_AUTOMATION", ""),
    exclude=REPO_ROOT)

# Set in config/.env, no default here: run.sh stops the run when it is missing.
MODEL = os.environ.get("AUTHORING_MODEL", "")
# Wall-clock budget per codegen call. Batching (below) keeps each call short, so
# this is a per-batch budget rather than one for the whole step.
GENERATE_TIMEOUT = int(os.environ.get("GENERATE_TIMEOUT_S", "900"))
# Max files requested per Claude call. `claude -p` returns ONE assistant message,
# so asking for every file at once makes the step a single all-or-nothing response
# that takes as long as all files combined — and anything that interrupts it (the
# timeout, or an exhausted 529 retry chain, which restarts generation from the top)
# discards every file. Small batches turn that into short, independently
# retryable calls. 0 = no batching, request everything in one call.
GENERATE_BATCH_SIZE = int(os.environ.get("GENERATE_BATCH_SIZE", "2"))
# Thinking effort for every codegen call, set in config/.env. Left unset, `claude -p`
# inherits the runner's own effortLevel ("high"), and hidden thinking became ~90% of
# the step: the Helper batch spent 18k thinking tokens to write 1.8k tokens of code.
GENERATE_EFFORT = os.environ.get("GENERATE_EFFORT") or None
# Diff budget for the URL repair pass. Swapping a literal for a property lookup is
# a handful of lines per URL; anything past this is the model rewriting a file it
# was asked only to de-hardcode.
URL_REPAIR_MAX_DIFF_LINES = int(os.environ.get("URL_REPAIR_MAX_DIFF_LINES", "60"))
# Diff budget for the step-narration repair pass. Splitting one summary logStep
# into a line per step is a few lines per step — larger than the URL swap, still
# nowhere near a rewrite. The repair may not touch a call (see
# _repair_step_narration), so this only ever buys narration.
NARRATION_REPAIR_MAX_DIFF_LINES = int(
    os.environ.get("NARRATION_REPAIR_MAX_DIFF_LINES", "120"))
# Compile what was just written, before step 04 spends a maven run, a browser
# launch and a fix attempt discovering it does not build. The framework's own
# CLAUDE.md has always made compiling step 1 of its mandatory self-test; this is
# the agent finally doing it. Set false for a non-Maven framework plugin.
COMPILE_CHECK = os.environ.get("GENERATE_COMPILE_CHECK", "true").lower() != "false"
COMPILE_TIMEOUT_S = int(os.environ.get("GENERATE_COMPILE_TIMEOUT_S", "180"))
# Diff budget for the compile repair pass. A wrong import, a missing one, a bad
# symbol: each is a line. Anything past this is a rewrite wearing a fix's clothes.
COMPILE_REPAIR_MAX_DIFF_LINES = int(
    os.environ.get("COMPILE_REPAIR_MAX_DIFF_LINES", "60"))
# Diff budget for the untraced-expected-value repair pass. Reading an expected
# value from where the check contract says it comes from, instead of a literal,
# is a few lines per check.
VALUE_REPAIR_MAX_DIFF_LINES = int(
    os.environ.get("VALUE_REPAIR_MAX_DIFF_LINES", "60"))

# ── Helpers ───────────────────────────────────────────────────────────────────

from shared.log import log as _log
def log(msg: str) -> None: _log("03-generate", msg)

from shared.claude import call_claude_ex as _call_claude_ex
# The static half of every codegen prompt, written by main() before the first call.
SYSTEM_PROMPT_FILE = AUDIT_DIR / "03-system-prompt.txt"
def call_claude(prompt: str, label: str = "") -> str:
    """Run one codegen call, reporting *why* it produced nothing when it does.

    The legacy call_claude() collapses timeout / non-zero exit / genuinely-empty
    into the same empty string, which is how a 900s timeout and a CLI error both
    surfaced as "returned empty response" with no raw output kept to tell them
    apart afterwards.
    """
    # The decoder turns a finished text block into one progress line per line of
    # text, and this step's text block IS the files map — echoing it would dump
    # every generated Java file into the run console. Surface only the events that
    # say something about progress: retries and tool use.
    _PROGRESS_PREFIXES = ("API retry", "MCP server", "→ ")

    def _on_output(_label: str, line: str) -> None:
        if _label == "stdout" and line.startswith(_PROGRESS_PREFIXES):
            log(f"  {line[:200]}")

    result = _call_claude_ex(
        prompt=prompt,
        model=MODEL,
        cwd=str(REPO_ROOT),
        timeout=GENERATE_TIMEOUT,
        on_output=_on_output,
        log_dir=str(AUDIT_DIR),   # raw transcript survives for post-mortem
        stream_json=True,
        # Codegen is pure text-in/text-out — it needs no MCP server at all. Without
        # this the subprocess inherits the user's global config and pays startup
        # and tool-registry cost connecting Playwright and Google Drive on every
        # single batch. Passing strict without an mcp_config loads zero servers.
        strict_mcp_config=True,
        # No built-in tools and no slash commands: codegen reads its whole context from
        # the prompt, and every tool definition is system-prompt tokens paid per call.
        tools="",
        disable_slash_commands=True,
        effort=GENERATE_EFFORT,
        # No user settings: with no tools, their permission allows buy nothing, and
        # their plugins' SessionStart hooks were injecting a persona into codegen.
        setting_sources="project,local",
        # A batch whose stream stalls is sent again rather than waited out: two
        # batches ran at 3 and 13 tokens/s for six and seven minutes (shared/claude.py).
        restart_if_slow=True,
        # Conventions, references and rules are identical for every call in this run,
        # so main() writes them once and every batch and repair sends that file as the
        # system prompt. Picked up here rather than passed in, so no call site — and no
        # test fake of this function — has to know the file exists.
        system_prompt_file=(str(SYSTEM_PROMPT_FILE) if SYSTEM_PROMPT_FILE.is_file() else None),
    )
    if not result.ok:
        log(f"ERROR: Claude call{label} {result.describe()}")
        # A timeout still carries whatever arrived before the kill; handing it back
        # lets extract_json() salvage a complete object when the model had already
        # finished and was only idling on the wire.
        return result.stdout if result.status == "timeout" else ""
    return result.stdout


# Shared: tolerates an unclosed ```json fence and braces in the prose before the object.
from shared.json_extract import extract_json  # noqa: E402


def read_reference_files() -> dict:
    """The worked examples config/repo-map.json lists for this repo, keyed by path.

    Which files make good examples is a property of the target repo, so it lives
    in config rather than here. Missing files are skipped; no list means none.
    """
    from shared.repo_config import load_repo_config
    ref_paths = load_repo_config().get("reference_files") or []
    if not ref_paths:
        log("No reference_files in config/repo-map.json for this repo — "
            "generating from its CLAUDE.md conventions alone")
    refs = {}
    for rel in ref_paths:
        full = AUTOMATION_FRAMEWORK_DIR / rel
        if full.exists():
            try:
                refs[rel] = full.read_text()
            except Exception:
                pass
    return refs


def read_existing_file(rel_path: str) -> str:
    """Read an existing file from the automation framework repo if it exists."""
    full = AUTOMATION_FRAMEWORK_DIR / rel_path
    return full.read_text() if full.exists() else ""


def read_existing_files_context(files_to_generate: list) -> str:
    """
    For each file in files_to_generate that already exists on disk, read its
    current content and return a formatted context block.

    This lets Claude ADD methods rather than rewrite the file from scratch,
    avoiding loss of existing JavaDoc, fields, and methods.
    """
    sections = []
    for rel_path in files_to_generate:
        content = read_existing_file(rel_path)
        if content.strip():
            sections.append(f"\n--- EXISTING: {rel_path} ---\n{content}\n")
    if not sections:
        return ""
    return (
        "\n\n<existing_file_contents>\n"
        "The files below ALREADY EXIST in the repo. Keep every existing member, field, import, "
        "annotation and JavaDoc exactly as it is. Change an existing method only as an \"extend\" "
        "entry of generation_plan[\"reuse\"] says (rule 11); everything else you add goes at the "
        "end of its section.\n"
        + "".join(sections)
        + "</existing_file_contents>"
    )


_LOCATOR_ARG = re.compile(r"""locator\s*\(\s*(["'])(?P<sel>(?:\\.|(?!\1).)*)\1""")


def unverified_selectors(selectors: dict, match_counts: dict) -> list:
    """Selectors step 02 never measured a match count for.

    A missing count is not the same as a count of 1: it means nobody checked, so
    the selector may match several elements and fail at runtime with a strict mode
    violation. Reported rather than dropped — a validation run predating the count
    protocol would otherwise empty the selector map and abort codegen entirely.
    """
    return sorted(n for n in (selectors or {}) if (match_counts or {}).get(n) is None)


_NAV_CALL = re.compile(r"\b(?:navigateTo|page\s*\.\s*navigate)\s*\(")
_ACTION_CALL = re.compile(r"\b(?:click|clickOn|submit|pressEnter|selectBy\w*)\s*\(")
_WAIT_CALL = re.compile(r"\bWaitHelper\s*\.\s*\w+\s*\(|\bwaitFor\w*\s*\(")
_COMMENT = ("//", "*", "/*")


def unsettled_navigations(content: str, lookback: int = 5) -> list:
    """Navigations issued while a previous one is probably still in flight.

    Clicking Login/Submit starts a navigation; navigating again before it settles
    makes Playwright abort the first one — `net::ERR_ABORTED` — which is the most
    common runtime failure in freshly generated web code. Codegen rule 6c asks for
    a wait in between; this reports when the generated code did not include one,
    because a rule the model can silently skip is not a guarantee.

    Returns (action_line_no, action_text, nav_line_no, nav_text) tuples.
    """
    lines = content.splitlines()
    flagged = []
    for i, line in enumerate(lines):
        if not _NAV_CALL.search(line):
            continue
        for j in range(i - 1, max(-1, i - 1 - lookback), -1):
            prev = lines[j].strip()
            if not prev or prev.startswith(_COMMENT):
                continue
            if _WAIT_CALL.search(prev):
                break                     # settled before navigating — fine
            if _ACTION_CALL.search(prev):
                flagged.append((j + 1, prev, i + 1, line.strip()))
                break
    return flagged


def unusable_locators(content: str) -> list:
    """Selectors in generated code that cannot match in a real browser run.

    Steps 02 and 03 both filter their inputs, so reaching here means the model
    invented a ref rather than being handed one — rare, but silent if unchecked,
    and the resulting page object fails in a way that blames the page.
    """
    return [m.group("sel") for m in _LOCATOR_ARG.finditer(content)
            if not is_dom_selector(m.group("sel"))]


def write_file(rel_path: str, content: str) -> None:
    """Write a file into Thanos-pw, creating parent directories as needed."""
    full = AUTOMATION_FRAMEWORK_DIR / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    log(f"  Wrote: {rel_path}")


# write_credential_property() lives in shared/credential_properties.py — 04_run_and_fix.py
# reuses the exact same function as a defensive re-check before diagnosing a
# CODE_ERROR failure, so the logic (and the file-location/key-naming rules it
# encodes) exists in exactly one place.
from shared.credential_properties import write_credential_property
from shared.credential_extraction import (credentials_from_plan, secret_columns,  # noqa: E402
                                          secret_property_key, take_secret_columns)
from shared.page_identity import is_dom_selector  # noqa: E402
from shared.test_catalog import test_methods_in  # noqa: E402
# URLs are the same story as credentials — one place decides the property file and
# the key names — except that URLs are not secrets, so 05_ship.py commits them.
from shared import properties_file, url_properties  # noqa: E402
from shared.edit_guards import validate_fix  # noqa: E402
from shared import check_provenance, test_case  # noqa: E402
from shared import logstep_narration  # noqa: E402
from shared import assertion_graph, flow_map, frames, value_match  # noqa: E402
from shared.code_analyzer import split_class_members, without_comments  # noqa: E402


# ── Guards ────────────────────────────────────────────────────────────────────

# Escape hatch for the rare case where generating against inferred locators really
# is what you want (e.g. the site is unreachable and you only need the scaffolding).
ALLOW_MISSING_SELECTORS = os.environ.get("ALLOW_MISSING_SELECTORS", "false").lower() == "true"


def _guard_web_validation(test_type, web_data, selectors, page_elements,
                          interaction_hints) -> None:
    """Refuse to generate a web module when step 02 confirmed nothing.

    Without this, a failed validation is silent: step 02 still reports ✓, and
    step 03 happily writes page objects full of guessed locators that only fail
    much later in step 04 — or worse, land in a PR.
    """
    if test_type not in ("web", "both"):
        return
    if selectors or page_elements or interaction_hints:
        return

    status = web_data.get("status", "unknown")
    reason = web_data.get("reason") or "no reason recorded"

    # A deliberate skip (API-only run, no web steps in the plan) is not a failure.
    if web_data.get("skipped") and status == "skipped":
        log(f"Web validation was skipped ({reason}) — generating with inferred locators")
        return

    log("ERROR: web validation produced zero confirmed selectors, page elements, "
        "and interaction hints.")
    log(f"  step 02 outcome: {status} — {reason}")
    log("  Generating now would write page objects against guessed locators.")

    if ALLOW_MISSING_SELECTORS:
        log("  ALLOW_MISSING_SELECTORS=true — proceeding anyway with inferred locators.")
        return

    log("  → FIX: re-run step 02 (see its warning above for the specific cause).")
    log("  → Or set ALLOW_MISSING_SELECTORS=true to generate against inferred locators.")
    sys.exit(1)


def _read_raw_input() -> str:
    """The user's own words, for tracing which checks came from them.

    Best-effort: INPUT_FILE has usually been moved to queue/processed/ by the
    time a resumed run reaches step 03, and an unreadable input must not break
    codegen. An empty string makes check_provenance answer USER for everything,
    which keeps assertions rather than dropping them — the safe way to be wrong.
    """
    raw = os.environ.get("INPUT_FILE", "")
    for candidate in ([Path(raw)] if raw else []) + [
            AGENT_DIR / "queue" / "processed" / Path(raw).name if raw else None]:
        try:
            if candidate and candidate.exists():
                return candidate.read_text()
        except OSError:
            continue
    log("NOTE: could not read the original input file — every check will be "
        "treated as user-requested, so none will be dropped.")
    return ""


def prune_unverified_checks(plan: dict, web_data: dict, raw_input: str) -> dict:
    """Drop the checks nobody asked for that the browser could not confirm.

    The matrix, for a verification step step 02 came back UNVERIFIED on:

      · the user asked for it  → keep everything. The assertion is generated at
        full strength and the test fails on purpose. The product does not do what
        they asked for, and that is a finding, not a codegen problem to smooth over.
      · the pipeline invented it → drop the locator, the accessor and the
        assertion. A check nobody asked for, against an element that does not
        exist, has no business failing a test — and a failing check with no owner
        is exactly what gets "fixed" by deleting it.

    Only ever drops. An unverified check the user DID ask for is left completely
    alone, because the point is that the test still proves what they wanted.

    Returns {"dropped": [...], "kept_unverified": [...], "kept_unmeasured": [...]}
    for the audit trail. kept_unmeasured is the part of kept_unverified that step 02
    reported as passed without measuring a selector for it.
    """
    unverified = web_data.get("steps_unverified") or []
    if not unverified:
        return {"dropped": [], "kept_unverified": []}

    dropped, kept, unmeasured = [], [], []
    for entry in unverified:
        step = entry.split("|", 1)[0].strip()
        if check_provenance.droppable(step, raw_input):
            dropped.append(step)
            continue
        kept.append(step)
        # Seen but never measured is a gap in step 02's evidence, not a finding
        # about the product, and must not be reported as one.
        if check_provenance.UNMEASURED in entry:
            unmeasured.append(step)
            log(f"UNVERIFIED but asked for — keeping the assertion for {step!r}. Step "
                f"02 reported it as passed but measured no selector for it, so its "
                f"locator is a guess: if the test fails here, suspect the locator "
                f"before the product.")
        else:
            log(f"UNVERIFIED but asked for — keeping the assertion for {step!r}. The "
                f"generated test WILL fail here: the product did not do this.")

    if not dropped:
        return {"dropped": [], "kept_unverified": kept, "kept_unmeasured": unmeasured}

    # What to remove: the locator names and accessor names whose subject matches a
    # dropped check. `successToast` and `isSuccessToastVisible` both share "toast"
    # with "Verify a success confirmation toast appears".
    subjects = [check_provenance.subject_words(s) for s in dropped]
    # Page-qualified (`IssuingBankPage.amountDisplay`, see step 02) or not.
    confirmed = {n.rsplit(".", 1)[-1] for n in web_data.get("selectors") or {}}

    def serves_dropped(name: str) -> bool:
        # A name backed by a confirmed selector is real whatever it is called.
        if name in confirmed:
            return False
        words = check_provenance.subject_words(name)
        return bool(words) and any(words & subj for subj in subjects)

    removed_locators, removed_actions, removed_steps = [], [], []
    for page in plan.get("web_pages") or []:
        for key, sink in (("locators_needed", removed_locators),
                          ("actions_needed", removed_actions)):
            names = page.get(key) or []
            keep = [n for n in names if not serves_dropped(n)]
            if len(keep) != len(names):
                sink.extend(n for n in names if n not in keep)
                page[key] = keep

    def invented(check: str) -> bool:
        return (check_provenance.shape(check) == check_provenance.VERIFICATION
                and any(check_provenance.subject_words(check) & subj for subj in subjects))

    for method in plan.get("web_test_methods") or []:
        steps = method.get("steps") or []
        keep = []
        for step in steps:
            if isinstance(step, dict):
                # A business step carries its action and its checks together.
                # Dropping an invented check must not drop the action with it.
                checks = [c for c in step.get("checks") or [] if isinstance(c, str)]
                removed_steps.extend(c for c in checks if invented(c))
                step["checks"] = [c for c in checks if not invented(c)]
                keep.append(step)
                continue
            if invented(step):
                removed_steps.append(step)
                continue
            keep.append(step)
        method["steps"] = keep

    log(f"Dropped {len(dropped)} unverified check(s) the input never asked for:")
    for step in dropped:
        log(f"  - {step}")
    if removed_locators:
        log(f"  locators removed: {', '.join(removed_locators)}")
    if removed_actions:
        log(f"  accessors removed: {', '.join(removed_actions)}")
    if removed_steps:
        log(f"  test steps removed: {len(removed_steps)}")
    log("  Nothing asked for this and the browser never saw it — generating an "
        "assertion against it would produce a test that fails for a reason no "
        "one owns.")

    return {"dropped": dropped, "kept_unverified": kept, "kept_unmeasured": unmeasured,
            "removed_locators": removed_locators,
            "removed_actions": removed_actions,
            "removed_steps": removed_steps}


def unconfirmed_locators(web_pages, selectors, interaction_hints, mechanisms) -> dict:
    """Locators the plan asks for that nothing confirmed. Named one by one.

    The rung missing between _guard_web_validation (fires only when a run
    confirmed NOTHING) and _warn_page_coverage (fires only when a whole PAGE has
    zero coverage). A run that confirms five of six locators passes both, and the
    sixth is silently guessed at codegen — which is how `successToast` became
    `page.locator("[class*='toast'], [class*='snackBar'], [class*='msgBlock']")`
    and cost a fix attempt and an assertion.
    """
    confirmed = set(selectors) | {h["name"] for h in interaction_hints if h.get("name")}
    covered = confirmed | set(mechanisms or {})
    gaps = {}
    for page in web_pages:
        cls = page.get("class_name", "?")
        missing = [n for n in (page.get("locators_needed") or [])
                   if n not in covered and f"{cls}.{n}" not in covered]
        if missing:
            gaps[page.get("class_name", "?")] = missing
    if gaps:
        log("WARNING: the plan asks for locators that step 02 never confirmed. "
            "Step 03 will infer these from naming conventions alone, and an "
            "inferred locator that turns out not to exist fails in step 04 as a "
            "timeout, not as a missing element:")
        for class_name, missing in gaps.items():
            log(f"  - {class_name}: {', '.join(missing)}")
    return gaps


def _evidence_readings(rows: list) -> dict:
    """{selector: [reading, ...]}: every count step 02's helpers took of it, a click's
    or a keystroke's own count included (marked with `action`)."""
    seen: dict = {}
    for row in rows or []:
        for sel, c in (row.get("checks") or {}).items():
            if isinstance(c, dict) and "total" in c:
                seen.setdefault(sel, []).append(c)
        for e in row.get("known") or []:
            if isinstance(e, dict) and e.get("selector") and "total" in e:
                seen.setdefault(e["selector"], []).append(e)
        for kind in ("clicked", "typed"):
            a = row.get(kind)
            if isinstance(a, dict) and a.get("sel") and a.get("total") is not None:
                seen.setdefault(a["sel"], []).append({**a, "action": kind})
    return seen


def _measured(readings: list) -> str:
    """What step 02's counts say of one selector: `unique` (1/1 visible, never more
    than one match), `ambiguous` (more than one match somewhere), `hidden` (found,
    never one visible) or `unmeasured`."""
    if not readings:
        return "unmeasured"
    if any((c.get("total") or 0) > 1 for c in readings):
        return "ambiguous"
    if any(c.get("total") == 1 and c.get("visible") == 1 for c in readings):
        return "unique"
    return "hidden"


def guessed_locators(files_map: dict, gaps: dict, rows: list) -> list:
    """Each locator the plan asked for that step 02 never confirmed, as the
    generated page object wrote it, held to what step 02's helpers counted.

    unconfirmed_locators names the gap before codegen; this measures what codegen
    put in it. A guess step 02 happened to count 1/1 (a header amount it read
    under another name) is confirmed by that count. One it never counted is a
    pure guess: a run wrote an attribute selector for a bank page's code field
    that matched nothing, while step 02 had typed into that very field.

    [{"path", "page", "name", "selector", "measured"}]
    """
    try:
        from shared.frameworks import get_active_plugin
        code = get_active_plugin().code
    except Exception:
        return []
    readings = _evidence_readings(rows)
    out = []
    for path, content in files_map.items():
        page = Path(path).stem
        names = gaps.get(page) or []
        if not names or not path.endswith(".java") or not content:
            continue
        for loc in code.extract_locators(content):
            if loc.get("name") not in names or loc.get("approx") or not loc.get("raw"):
                continue
            out.append({"path": path, "page": page, "name": loc["name"], "selector": loc["raw"],
                        "measured": _measured(readings.get(loc["raw"]) or [])})
    return out


def _locator_candidates(guess: dict, rows: list, limit: int = 25) -> list:
    """Selectors step 02 counted at exactly one visible element, never more, in the
    guessed locator's frame: what the element it means can be picked from. The
    ones sharing a word with the name come first, and among those the ones the
    flow typed into or clicked: a field's label shares its words too."""
    readings = _evidence_readings(rows)
    frame = frames.split(guess["selector"])[0]
    words = flow_map.naming_words(guess["name"])
    out = []
    for sel, taken in readings.items():
        if frames.split(sel)[0] != frame or _measured(taken) != "unique":
            continue
        tags = {c.get("tag") for c in taken if c.get("tag")}
        texts = [c.get("text") for c in taken if c.get("text")]
        acts = sorted({"typed into" if c.get("action") == "typed" else "clicked"
                       for c in taken if c.get("action")})
        about = ", ".join([*sorted(tags), *([f"showed {texts[-1][:40]!r}"] if texts else []), *acts])
        shared = len(words & flow_map.naming_words(frames.split(sel)[1] + " " + " ".join(texts)))
        out.append((shared, bool(acts), sel, about))
    out.sort(key=lambda c: (-c[0], not c[1]))
    return [(sel, about) for _shared, _acted, sel, about in out[:limit]]


def _repair_guessed_locators(files_map: dict, guesses: list, rows: list) -> tuple:
    """Point a guessed locator step 02 never counted at an element it did.

    One repair pass over the page objects concerned, offering only selectors step
    02 counted at exactly one visible element in the same frame. A replacement is
    kept only if it is one of those, and the file passes validate_fix; anything
    else stays as generated and is reported. Returns (files_map, [guess, ...]) with
    each guess's outcome in "result".
    """
    needs = [g for g in guesses if g["measured"] != "unique"]
    for g in guesses:
        if g["measured"] == "unique":
            g["result"] = "confirmed by step 02's counts"
    if not needs:
        return files_map, guesses
    log(f"GUARD: {len(needs)} locator(s) step 02 never confirmed were written from a guess "
        f"it did not count at one visible element — repairing:")
    offered, lines = {}, []
    for g in needs:
        cands = _locator_candidates(g, rows)
        offered[(g["path"], g["name"])] = {sel for sel, _ in cands}
        log(f"  {g['page']}.{g['name']} = {g['selector']} ({g['measured']})")
        lines.append(f"  - {g['page']}.{g['name']} in {g['path']} is {g['selector']} — "
                     + {"unmeasured": "step 02 never counted it",
                        "ambiguous": "step 02 counted more than one match for it",
                        "hidden": "step 02 never found it visible"}[g["measured"]] + ".\n"
                     + ("    Counted at exactly one visible element in its frame:\n"
                        + "".join(f"      {sel}   ({about})\n" for sel, about in cands)
                        if cands else "    Step 02 counted nothing unique in its frame.\n"))
    paths = sorted({g["path"] for g in needs})
    files = "".join(f"\n--- {p} ---\n{files_map[p]}\n" for p in paths)
    prompt = f"""These page-object locators were written from a guess. The browser run that
validated this flow never confirmed a selector for them, and the guess is not one
it counted at exactly one visible element:
{''.join(lines)}
For each, if one of the listed selectors is the element the locator's name means,
rewrite that locator to it, in the locator syntax the file already uses (an
iframe hop `A >> internal:control=enter-frame >> B` is a frame locator for A
holding B). If none of them is that element, leave the locator exactly as it is.
Change NOTHING else: same fields, methods, signatures and comments.
{files}
Return ONLY a JSON object mapping each file path you changed to its complete new
contents. No prose.
"""
    repaired = extract_json(call_claude(prompt, label=" [locator-repair]")) or {}
    for path, content in repaired.items():
        if path not in paths or not (content or "").strip():
            continue
        ok, reason = validate_fix(files_map[path], content, Path(path).name,
                                  URL_REPAIR_MAX_DIFF_LINES)
        if not ok:
            log(f"  locator-repair REJECTED for {Path(path).name} — {reason}")
            continue
        after = {g["name"]: g["selector"] for g in guessed_locators(
            {path: content}, {Path(path).stem: [g["name"] for g in needs if g["path"] == path]}, rows)}
        mine = [g for g in needs if g["path"] == path]
        if any(after.get(g["name"], g["selector"]) not in offered[(path, g["name"])] | {g["selector"]}
               for g in mine):
            log(f"  locator-repair REJECTED for {Path(path).name} — it wrote a selector "
                f"step 02 did not offer")
            continue
        files_map[path] = content
        for g in mine:
            if after.get(g["name"], g["selector"]) != g["selector"]:
                g["result"] = f"repaired to {after[g['name']]}"
                log(f"  locator-repair: {g['page']}.{g['name']} = {after[g['name']]}")
    for g in needs:
        g.setdefault("result", "still a guess")
    return files_map, guesses


def _warn_page_coverage(web_pages, selectors, interaction_hints) -> list:
    """Flag individual pages that step 02 never confirmed a single locator for.

    The guard above only catches a run that came back completely empty. A
    partial run — e.g. login validated fine but every page past it got zero
    coverage — passes that guard silently (selectors is non-empty overall), so
    step 03 quietly infers 100% of a specific page's locators without that
    being visible anywhere. Surface it per page instead.

    Returns the list of (class_name, needed_locators) pairs with zero coverage,
    so the caller can persist it into the durable 03-generate.json audit trail
    instead of it existing only as a console line that scrolls away.
    """
    # Both SELECTOR_FOUND (selectors) and INTERACTION_HINT (interaction_hints)
    # are live-DOM-confirmed data step 03's own codegen prompt treats as equally
    # authoritative — crediting only one under-counts real coverage.
    confirmed = set(selectors.keys()) | {h["name"] for h in interaction_hints if h.get("name")}
    uncovered = []
    for page_def in web_pages:
        needed = page_def.get("locators_needed", [])
        cls = page_def.get("class_name", "?")
        if needed and not (confirmed & ({*needed} | {f"{cls}.{n}" for n in needed})):
            uncovered.append((page_def.get("class_name", "?"), needed))

    if uncovered:
        log("WARNING: the following pages have ZERO confirmed selectors — step 03 "
            "will infer ALL locators for them from naming conventions alone. "
            "(Note: this check is name-based across the whole flow — if a page "
            "reuses a locator name that was only confirmed on a DIFFERENT page, "
            "it may be under- or over-reported here.)")
        for class_name, needed in uncovered:
            log(f"  - {class_name}: needs {needed}")

    return uncovered


def _repair_hardcoded_urls(files_map: dict, url_props: dict, feature: str,
                           props_file_name: str) -> tuple:
    """Move literal URLs out of generated code and into property lookups.

    Rule 16 in the prompt tells the model not to write them; this is what happens
    when it does anyway. One targeted pass over only the offending files, guarded
    by validate_fix so a "repair" cannot quietly drop half a class, and accepted
    per-file only if it actually removed violations.

    Returns (files_map, {rel_path: [url, ...]}) — the second value is what is
    STILL hardcoded afterwards, for the audit and for step 04 to see.

    Only URLs this run added count. One already in an existing file is not this
    run's to move: repairing it rewrote code the PR had no business touching, and
    paid a model call on every run that extended that file. Step 04's
    url_properties.no_hardcoded_url draws the same line.
    """
    def added_urls(path, content):
        before = set(url_properties.hardcoded_urls(read_existing_file(path)))
        return [url for url in url_properties.hardcoded_urls(content) if url not in before]

    violations = {path: found for path, content in files_map.items()
                  if path.endswith(".java") and content
                  and (found := added_urls(path, content))}
    if not violations:
        return files_map, {}

    log(f"GUARD: {len(violations)} generated file(s) hardcode a URL — repairing:")
    for path, urls in violations.items():
        log(f"  {Path(path).name}: {', '.join(urls)}")

    # A URL the model invented has no key yet, and the repair needs one to point
    # at. Name and write it now so the property exists before the test runs.
    keys = dict(url_props)
    by_url = {v: k for k, v in keys.items()}
    for urls in violations.values():
        for url in urls:
            normalized = url_properties.normalize(url)
            if normalized and normalized not in by_url:
                key = url_properties.derive_key(feature.lower(), normalized, keys)
                keys[key] = normalized
                by_url[normalized] = key
    if len(keys) > len(url_props):
        url_properties.write_url_properties(
            AUTOMATION_FRAMEWORK_DIR, keys, feature.lower(), log=log)

    key_table = "".join(f'  "{k}" = {v}\n' for k, v in keys.items())
    offending = "".join(
        f"\n--- {path} (move: {', '.join(violations[path])}) ---\n{files_map[path]}\n"
        for path in violations)
    prompt = f"""These generated Java files hardcode URLs. Every URL below is already a
property in parameters/{props_file_name}:

{key_table}
Rewrite each file so the URLs named after "move:" in its header no longer appear as
literals in the code, reading each from its property instead. Leave any other URL
in the file exactly as it is — it was there before this change:
  - In a super(...) call:  super(config, config.getRunTimeProperty("<key>"))
    Inline it there — a `static final` constant cannot read config, and an instance
    field cannot be referenced before the supertype constructor has run.
  - Anywhere else:         private final String loginUrl = config.getRunTimeProperty("<key>");
    An INSTANCE field, never `static`.
  - Delete any constant that becomes unused, and keep a URL that only appears in a
    comment or JavaDoc exactly as it is.

Change NOTHING else: same methods, same signatures, same locators, same comments.

{offending}
Return ONLY a JSON object mapping each file path above to its complete corrected
contents. No prose.
"""
    response = call_claude(prompt, label=" [url-repair]")
    repaired = extract_json(response) or {}
    if not repaired:
        log("  url-repair returned nothing — leaving the files as generated")
        return files_map, violations

    remaining = dict(violations)
    for path, content in repaired.items():
        if path not in violations or not (content or "").strip():
            continue
        still = added_urls(path, content)
        if len(still) >= len(violations[path]):
            log(f"  url-repair did not fix {Path(path).name} — keeping the original")
            continue
        ok, reason = validate_fix(files_map[path], content, Path(path).name,
                                  URL_REPAIR_MAX_DIFF_LINES)
        if not ok:
            log(f"  url-repair REJECTED for {Path(path).name} — {reason}")
            continue
        files_map[path] = content
        log(f"  url-repair applied to {Path(path).name}")
        if still:
            remaining[path] = still
        else:
            remaining.pop(path, None)
    return files_map, remaining


def _repair_copied_methods(files_map: dict, feature_class: str) -> tuple:
    """Keep one copy of a method this run wrote into several classes.

    Rule 5f asks for it, but page objects are generated a batch at a time, and a run
    still wrote the same normalizeAmount() into four of them. One repair pass moves
    it into the Helper as a public static method, and each page object calls that one
    copy where it called its own. All or nothing: half a move leaves a page calling a
    copy that is gone. Only into a Helper this run generated: an existing one is
    somebody's shipped code, and a move into it is for a reviewer.

    Returns (files_map, [copied group, ...]) — what is still copied afterwards.
    """
    sources = {p: c for p, c in files_map.items()
               if p.startswith("src/main/") and p.endswith(".java") and c}

    def copies(contents: dict) -> list:
        new = {p: (module_index.changed_methods(read_existing_file(p), c)["added"]
                   if read_existing_file(p) else module_index.method_keys(c))
               for p, c in contents.items()}
        return module_index.copied_methods(contents, only=new)

    groups = copies(sources)
    if not groups:
        return files_map, []
    described = [module_index.describe_duplicates(
        {"methods": [i.split("::", 1)[1] for i in g], "ratio": 1.0}) for g in groups]
    log(f"GUARD: {len(groups)} method(s) written into several classes — keeping one copy:")
    for line in described:
        log(f"  {line.replace('`', '')}")
    helper = next((p for p in sources if p.endswith(f"/{feature_class}Helper.java")), None)
    if not helper or read_existing_file(helper):
        log("  no Helper generated by this run to move it into — left for review")
        return files_map, groups

    involved = sorted({i.split("::", 1)[0] for g in groups for i in g} | {helper})
    files = "".join(f"\n--- {p} ---\n{files_map[p]}\n" for p in involved)
    copied = "".join(f"  - {line}\n" for line in described)
    prompt = f"""These generated classes each carry their own copy of the same method:
{copied}
Keep ONE copy, as a public static method of {Path(helper).stem}. Remove the copies from
the page objects, and make each place that called its own copy call
{Path(helper).stem}.<method>(...) instead, so every value a page getter or an operation
returns stays what it is now and the tests do not change.

Change NOTHING else: same public methods and signatures, same locators, same comments.
{files}
Return ONLY a JSON object mapping each file path you changed to its complete new
contents. No prose.
"""
    repaired = extract_json(call_claude(prompt, label=" [copy-repair]")) or {}
    moving = {}
    for group in groups:
        for place in group:
            path, key = place.split("::", 1)
            moving.setdefault(path, set()).add(key.split(".", 1)[1].split("(", 1)[0])
    candidate = dict(files_map)
    for path, content in repaired.items():
        if path not in involved or not (content or "").strip():
            continue
        # The same budget as the URL repair: one method moved, its callers repointed.
        ok, reason = validate_fix(files_map[path], content, Path(path).name,
                                  URL_REPAIR_MAX_DIFF_LINES, may_remove=moving.get(path, ()))
        if not ok:
            log(f"  copy-repair REJECTED for {Path(path).name} — {reason}; "
                f"keeping every file as generated")
            return files_map, groups
        candidate[path] = content
    left = copies({p: candidate[p] for p in sources})
    if not repaired or left:
        log("  copy-repair did not leave one copy — keeping every file as generated")
        return files_map, groups
    log(f"  copy-repair applied: one copy, in {Path(helper).name}")
    return candidate, []


# ── What step 02 typed and compared ───────────────────────────────────────────

def _lower_camel(name: str) -> str:
    return (name[:1].lower() + name[1:]) if name else "option"


def option_sets_hint(web_data: dict, plan: dict) -> str:
    """OPTION SETS: what each declared choice offered, as the browser measured it.

    Only for the controls generation_plan["option_enums"] names; a set found
    beside any other locator is a fact nobody asked to act on. Rendered in this
    repo's locator syntax with the key spliced in, so rule 18's selection method
    is the confirmed selector with only the key replaced, and the value the flow
    used rebuilds the measured selector byte for byte.
    """
    enums = [e for e in plan.get("option_enums") or [] if isinstance(e, dict) and e.get("control")]
    if not enums:
        return ""
    from shared.locator_emit import code_for
    sets = web_data.get("option_sets") or {}
    lines = ["", "", "OPTION SETS — every option the page offered at each choice (rule 18):"]
    for enum in enums:
        name, control, page = enum.get("name") or "Option", enum["control"], enum.get("page") or ""
        found = sets.get(f"{page}.{control}") or sets.get(control)
        if not found:
            exercised = ", ".join(enum.get("exercised") or []) or "the exercised value"
            lines.append(f"  {name} ({page}.{control}): no alternatives were recorded — list only "
                         f"{exercised}, and say so in the enum's Javadoc.")
            continue
        lines.append(f"  {name} ({page}.{control}), keyed by `{found['attribute']}`; "
                     f"the flow used `{found['chosen']}`:")
        for option in found["options"]:
            note = (f" — shown {option['occurrences']} times on that page, so its locator is not unique"
                    if option["occurrences"] > 1 else "")
            lines.append(f"    key \"{option['key']}\", label \"{option['label']}\"{note}")
        if found.get("truncated"):
            lines.append("    … the page offered more; these are the first ones.")
        if found.get("template"):
            code = code_for(found["template"])
            java = code.get("java") or code.get("findby") or ""
            spliced = java.replace("{key}", '" + ' + _lower_camel(name) + '.getKey() + "')
            lines.append(f"    select any value with: {spliced}")
    return "\n".join(lines)


def _typed_shape(value) -> str:
    """A typed value's shape, said precisely enough to generate another like it:
    `1 word: 10 digits, nothing else`, `2 words: letters, punctuation`."""
    text = str(value)
    words = len(text.split())
    head = f"{words} word{'' if words == 1 else 's'}"
    if text.isdigit():
        return f"{head}: {len(text)} digits, nothing else"
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.\w+", text):
        return f"{head}: an email address"
    kinds = [kind for kind, pattern in (("letters", r"[A-Za-z]"), ("digits", r"\d"),
                                        ("punctuation", r"[^\w\s]")) if re.search(pattern, text)]
    return f"{head}: {', '.join(kinds)}" if kinds else head


def value_contracts_hint(web_data: dict, raw_input: str = "") -> str:
    """The prompt section built from what step 02 typed and compared.

    "" when it recorded neither — a run from before the markers existed
    generates exactly as it always did.

    A typed value the test case itself states is the test's data, used as it is.
    Only the rest was made up, and is randomised inside its shape: told to
    randomise every field, a run replaced the card number and address the test
    case gave with generated ones.
    """
    inputs = web_data.get("inputs_used") or {}
    checks = [c for c in web_data.get("value_checks") or []
              if c.get("relation") or c.get("order")]
    given = {f for f, v in inputs.items() if test_case.is_given(v, raw_input)}
    out = ""
    if inputs:
        out += ("\n\nVALIDATED INPUTS — step 02 typed exactly these, and the checks below "
                "held for them:\n")
        for field, value in inputs.items():
            out += (f"  {field} = {value!r}  ({_typed_shape(value)})"
                    + ("  GIVEN BY THE TEST CASE" if field in given else "") + "\n")
        if given:
            out += ("A value marked GIVEN BY THE TEST CASE is the test's own data: make it "
                    "that field's default in the Builder or data file exactly as written, "
                    "never randomised and never swapped for another value.\n")
        out += (("Test data for the other fields" if given else "Test data for these fields")
                + " keeps that SHAPE: EXACTLY the same number of "
                "words, the same kinds of characters, the same prefix. Randomise only "
                "inside it, composing the value from single-word parts — a random first "
                "name + ' ' + a random last name for a two-word name. Never one token "
                "such as a prefix plus random letters, and never a whole-name generator: "
                "those add titles and suffixes ('Dr.', 'MD'), and a product that keeps "
                "two words showed 'Alica Bednar' for 'Alica Bednar MD'. A shared data "
                "generator whose output you have not seen is not that shape either: a "
                "phone-number generator gave '(305) 203-8102' for a field typed "
                "'9876543210', and the page showed '(305)2038102'. Build such a value "
                "from its parts, random digits of that length for a digits-only field. "
                "Data of another shape is a flow step 02 never saw.\n")
    if checks:
        out += ("\n\nCHECK CONTRACTS — how the live page rendered each compared value. "
                "Assert every one of these checks with exactly the comparison given; it "
                "replaces the comparison the plan's wording implies, which was chosen "
                "before anything was observed:\n")
        for c in checks:
            how = (value_match.ASSERT_WITH[c["relation"]] if c.get("relation")
                   else value_match.ORDER_ASSERT_WITH[c["order"]])
            out += (f"  - {c['check']}\n"
                    f"      {c['element']} showed {c['rendered'][:120]!r}; the other side "
                    f"({c['source']}) was {c['expected'][:120]!r}\n"
                    f"      → assert with {how}\n")
        out += ("The expected side always comes from the source named: `input:<field>` "
                "is the test-data value the test typed into that field, `element:<name>` "
                "is a value the test reads from that element earlier in the flow, and "
                "`literal` is text quoted in the test case. Never write a new literal as "
                "an expected value.\n")
        if any(c.get("order") and c["source"].startswith("element:") for c in checks):
            out += ("For a LESS/GREATER check, read the other side from exactly the element "
                    "named, at the point in the flow where that element is on screen, and "
                    "keep it in a variable until the comparison. Never substitute another "
                    "element that shows a similar amount later: a page can change that "
                    "amount on its own in between (see UPDATES THAT FOLLOW AN ACTION), and "
                    "a run read 'the total before the promo' after the card number had "
                    "already lowered it, then compared the amount with itself.\n")
    return out


def delayed_updates_hint(web_data: dict, shared_index: str = "") -> str:
    """The prompt section for what step 02 saw the page do after an action
    returned. "" when it saw nothing.

    Step 02's browser helper waits after every action until the page settles, so
    the flow it validated never acted mid-update. Generated code acts at once
    unless told: a promo clicked in the same second the card number was typed was
    lost to the redraw the card number set off.
    """
    updates = web_data.get("delayed_updates") or []
    if not updates:
        return ""
    wait = ("WaitHelper.waitForPageToSettle(config)" if "waitForPageToSettle(" in (shared_index or "")
            else "a WaitHelper wait from <shared_code_index> that holds until the changed "
                 "element shows its new value")
    out = ("\n\nUPDATES THAT FOLLOW AN ACTION — step 02 saw the page keep working after "
           "these actions returned, on requests of its own, and change what is listed. It "
           "waited for that before its next action; generated code must too:\n")
    for u in updates:
        changed = "; ".join(
            f"{'/'.join(c.get('names') or []) or c['element']} {c['before']!r} → {c['after']!r}"
            for c in u.get("changed") or [])
        out += (f"  - after {', '.join(u.get('after') or [])}: {changed} "
                f"(settled after {u.get('settled_ms')}ms, {u.get('requests')} request(s))\n")
    out += (f"The page method that performs such an action and stays on the same page ends "
            f"with {wait}, so the next action is not taken mid-update — a click then is "
            f"lost — and a value read next is the updated one. A method that returns another "
            f"page needs nothing more: that page's load check waits. Never "
            f"WaitHelper.waitForNetworkIdle for this: it returns at once on a page that has "
            f"already loaded. A value shown BEFORE such an action (the left side above) can "
            f"only be read before it, in an earlier step.\n")
    return out


def _observed_texts(raw_input: str, web_data: dict) -> str:
    """Everything an expected value may legitimately come from: the test case,
    and what step 02 typed, read and reported."""
    parts = [raw_input or ""]
    parts += [str(v) for v in (web_data.get("inputs_used") or {}).values()]
    for c in web_data.get("value_checks") or []:
        parts += [c.get("rendered", ""), c.get("expected", "")]
    parts += [str(h.get("text", "")) for h in web_data.get("interaction_hints") or []]
    parts += [str(s) for s in web_data.get("steps_passed") or []]
    return "\n".join(p for p in parts if p)


def expected_literals(files_map: dict) -> dict:
    """{path: [value, ...]} — each string a generated file compares against.

    An assertion's expected argument when it is a whole string literal (never one
    nested in a call: `testData.get("amount")` names a column, not a value), and
    every cell of a CSV column whose header starts with `expected`.
    """
    found = {}
    for path, content in files_map.items():
        values = []
        if path.endswith(".csv"):
            rows = list(csv.reader(io.StringIO(content or "")))
            columns = [i for i, header in enumerate(rows[0] if rows else [])
                       if header.strip().lower().startswith("expected")]
            values = [row[i].strip() for row in rows[1:] for i in columns
                      if i < len(row) and row[i].strip()]
        elif path.endswith(".java"):
            for info in assertion_graph.asserts_in(without_comments(content or ""), path):
                parts = assertion_graph.check_parts(info)
                values += [shown[1:-1] for shown, top in zip(parts["display"], parts["top"])
                           if top and shown.startswith('"') and len(shown) > 2]
        if values:
            found[path] = values
    return found


def untraced_expected_values(files_map: dict, raw_input: str, web_data: dict) -> dict:
    """{path: [value, ...]} — expected values the test case never states and step 02
    never saw. `Rp 490.909` was one: a javadoc example in the helper batch that the
    next batch reused as the CSV's expected amount, while the page showed Rp20.000."""
    observed = _observed_texts(raw_input, web_data)
    out = {}
    for path, values in expected_literals(files_map).items():
        # A value the file already had before this run traces to the input that
        # wrote it, not to this one. Judging it against this run's input asked the
        # repair to "fix" an existing, passing test — and it weakened one.
        already = set(expected_literals({path: read_existing_file(path)}).get(path, []))
        missing = [v for v in values if v not in already
                   and not value_match.appears_in(v, observed)]
        if missing:
            out[path] = missing
    return out


def _repair_untraced_expected_values(files_map: dict, raw_input: str,
                                     web_data: dict) -> tuple:
    """One targeted pass that takes invented expected values out of the code.

    The generated test classes go in with the offending files: a CSV value is read
    by a test, and replacing it with a value captured on the page is an edit to
    that test as much as to the CSV. The pass is kept only if it leaves fewer
    untraced values than it found, with every changed file inside validate_fix.
    Returns (files_map, still untraced).
    """
    violations = untraced_expected_values(files_map, raw_input, web_data)
    if not violations:
        return files_map, {}

    log(f"GUARD: {len(violations)} generated file(s) expect a value that neither the "
        f"test case nor step 02 ever showed — repairing:")
    for path, values in violations.items():
        log(f"  {Path(path).name}: {', '.join(repr(v) for v in values)}")

    editable = list(violations) + [p for p in files_map if p not in violations
                                   and p.endswith(("Test.java", "Tests.java"))]
    offending = "".join(
        f"\n--- {path}"
        + (f" (untraced: {', '.join(repr(v) for v in violations[path])})"
           if path in violations else " (reads the values above)")
        + f" ---\n{files_map[path]}\n" for path in editable)
    prompt = f"""These generated files compare against expected values that appear nowhere in
the test case and were never seen on the live page. They were invented, and a test
built on one fails on a value nobody asked for.

The test case:
{raw_input or "(unavailable)"}
{value_contracts_hint(web_data, raw_input) or chr(10) + "(step 02 recorded no compared values)" + chr(10)}
Rewrite the files so every value named after "untraced:" is gone. Take each expected
side from where it really comes from: the value the test typed, a value the test reads
from the page earlier in the flow, or text quoted in the test case. If a CSV column
existed only to hold an invented value, remove the column and the code that reads it.

Change NOTHING else: same methods, same signatures, same locators, same comments.
{offending}
Return ONLY a JSON object mapping each file path you changed to its complete corrected
contents. No prose.
"""
    repaired = extract_json(call_claude(prompt, label=" [value-repair]")) or {}
    candidate = dict(files_map)
    for path, content in repaired.items():
        if path not in editable or not (content or "").strip():
            continue
        ok, reason = validate_fix(files_map[path], content, Path(path).name,
                                  VALUE_REPAIR_MAX_DIFF_LINES)
        if not ok:
            log(f"  value-repair REJECTED for {Path(path).name} — {reason}")
            return files_map, violations
        candidate[path] = content

    remaining = untraced_expected_values(candidate, raw_input, web_data)
    before = sum(map(len, violations.values()))
    after = sum(map(len, remaining.values()))
    if after >= before:
        log("  value-repair removed no untraced value — keeping the files as generated")
        return files_map, violations
    log(f"  value-repair applied to {', '.join(Path(p).name for p in repaired if p in editable)}")
    return candidate, remaining


# ── The compile gate ──────────────────────────────────────────────────────────

# javac through maven: "[ERROR] /abs/path/File.java:[11,38] cannot find symbol"
_JAVAC_ERROR = re.compile(r"^\[ERROR\]\s+(?P<path>/\S+?\.java):\[(?P<line>\d+),\d+\]\s*(?P<msg>.*)$")


def compile_errors(output: str, root: Path) -> dict:
    """{repo-relative path: ["line: message", ...]} from a maven compile failure.

    Maven repeats every error twice — once in the COMPILATION ERROR block and
    again in the "Failed to execute goal" summary — so entries are de-duplicated.
    """
    found: dict = {}
    for raw in (output or "").splitlines():
        m = _JAVAC_ERROR.match(raw.strip())
        if not m:
            continue
        try:
            rel = str(Path(m.group("path")).resolve().relative_to(Path(root).resolve()))
        except ValueError:
            rel = m.group("path")
        entry = f"{m.group('line')}: {m.group('msg')}".rstrip()
        if entry not in found.setdefault(rel, []):
            found[rel].append(entry)
    return found


def _core_class_index() -> str:
    """Every class under automation/core, as the exact import a file would write.

    The repair pass exists mostly to fix an invented package, so handing it the
    real ones is the whole job: a generated page object imported
    `automation.core.web.BasePage` because it sits in modules/<f>/web/ and assumed
    core mirrored that. core/ is flat, and this says so with names, not prose.
    """
    core = AUTOMATION_FRAMEWORK_DIR / "src/main/java/automation/core"
    if not core.is_dir():
        return ""
    names = sorted(
        "automation." + str(f.relative_to(core.parent).with_suffix("")).replace("/", ".")
        for f in core.rglob("*.java"))
    return "".join(f"  import {n};\n" for n in names)


def _run_test_compile() -> tuple:
    """(ok, output) for `mvn test-compile` in the framework checkout.

    test-compile, not compile: the generated test class is a source file too, and
    the import that broke the observed run could just as easily have been in it.
    """
    command = ["mvn", "-q", "test-compile", "--no-transfer-progress"]
    started = time.time()
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=COMPILE_TIMEOUT_S,
                                cwd=str(AUTOMATION_FRAMEWORK_DIR))
        output = (result.stdout or "") + (result.stderr or "")
        ok = result.returncode == 0
    except subprocess.TimeoutExpired:
        # No process-group dance needed here, unlike run_maven_test: a compile
        # forks no surefire JVM to outlive the maven process.
        output = f"compile timed out after {COMPILE_TIMEOUT_S}s"
        ok = None
    except OSError as exc:
        output = f"could not run maven: {exc}"
        ok = None
    try:
        from shared import metrics
        verdict = "error" if ok is None else ("pass" if ok else "fail")
        metrics.record_tool("compile", " ".join(command), time.time() - started, verdict)
    except Exception:
        pass
    return ok, output


def _repair_compile_errors(written_contents: dict, errors: dict) -> dict:
    """One targeted pass over the generated files javac rejected.

    Same shape as the URL and narration repairs: only the offending files, guarded
    by validate_fix so a "repair" cannot drop half a class, accepted per-file only.
    Returns the files it actually rewrote, {rel_path: content}.
    """
    offending = "".join(
        f"\n--- {path} ---\n{written_contents[path]}\n" for path in errors)
    error_table = "".join(
        f"  {path}\n" + "".join(f"    line {e}\n" for e in msgs)
        for path, msgs in errors.items())
    index = _core_class_index()
    index_block = (
        f"\nThese are the ONLY classes under automation.core — the package is flat, "
        f"there is no automation.core.web and no automation.modules.core:\n\n{index}"
        if index else "")

    prompt = f"""These generated Java files do not compile. javac said:

{error_table}{index_block}
Fix ONLY what the compiler complained about — a wrong or missing import, a symbol
that does not exist, a signature that does not match. Do not rename anything, do
not add or remove a method, do not touch a locator, an assertion, or a logStep,
and do not introduce a literal URL.

{offending}
Return ONLY a JSON object mapping each file path above to its complete corrected
contents. No prose.
"""
    response = call_claude(prompt, label=" [compile-repair]")
    repaired = extract_json(response) or {}
    if not repaired:
        log("  compile-repair returned nothing — leaving the files as generated")
        return {}

    applied = {}
    for path, content in repaired.items():
        if path not in errors or not (content or "").strip():
            continue
        ok, reason = validate_fix(written_contents[path], content, Path(path).name,
                                  COMPILE_REPAIR_MAX_DIFF_LINES)
        if not ok:
            log(f"  compile-repair REJECTED for {Path(path).name} — {reason}")
            continue
        write_file(path, content)
        applied[path] = content
        log(f"  compile-repair applied to {Path(path).name}")
    return applied


def _compile_check(written_contents: dict, pre_run: dict = None) -> dict:
    """Compile what was just written; repair once; abort if it still does not build.

    This is the cheapest guard in the pipeline and it did not exist. The observed
    run shipped `import automation.core.web.BasePage` — a package that has never
    existed — and paid for it with the whole initial maven run, a no-change
    re-run to rule out flakiness, and one of only two fix attempts. A compile is
    seconds, needs no browser, and cannot be flaky.

    `pre_run` holds the existing files this run changed, as they were before it.
    Errors only in files this run did not write then usually mean a change to one
    of them broke the code that calls it, and the log says which.

    Returns written_contents with any repaired file replaced.
    """
    if not COMPILE_CHECK:
        return written_contents
    if not (AUTOMATION_FRAMEWORK_DIR / "pom.xml").exists():
        log("Compile check: skipped — no pom.xml in the framework checkout")
        return written_contents

    log("Compile check: mvn test-compile ...")
    ok, output = _run_test_compile()
    if ok:
        log("Compile check: OK")
        return written_contents
    if ok is None:
        # Could not run maven at all. That is an infra problem, not generated code
        # being wrong, and failing the run on it would blame the wrong thing.
        log(f"Compile check: skipped — {output}")
        return written_contents

    errors = compile_errors(output, AUTOMATION_FRAMEWORK_DIR)
    ours = {p: msgs for p, msgs in errors.items() if p in written_contents}
    log(f"GUARD: generated code does not compile — {sum(len(m) for m in errors.values())} "
        f"error(s) in {len(errors)} file(s):")
    for path, msgs in errors.items():
        log(f"  {Path(path).name}: {msgs[0]}" + (f" (+{len(msgs) - 1} more)" if len(msgs) > 1 else ""))

    if not ours:
        # Every error is in a file this run did not write, so there is nothing here
        # to repair. Either the checkout was already broken, or this run changed an
        # existing file and something that calls it no longer compiles.
        changed = sorted(p for p in (pre_run or {}) if p in written_contents)
        if changed:
            log(f"ERROR: the compile failure is in files this run did not generate, but this "
                f"run changed {', '.join(Path(p).name for p in changed)} — a changed "
                f"signature breaks the code that calls it; keep the old one as an overload.")
        else:
            log("ERROR: the compile failure is entirely in files this run did not "
                "generate — the framework checkout does not build on its own.")
        (AUDIT_DIR / "03-generate.json").write_text(json.dumps({
            "error": "compile_failed",
            "compile_errors": errors,
            "files_written": sorted(written_contents),
        }, indent=2))
        sys.exit(1)

    applied = _repair_compile_errors(written_contents, ours)
    if applied:
        written_contents = {**written_contents, **applied}
        log("Compile check: re-compiling after repair ...")
        ok, output = _run_test_compile()
        if ok:
            log("Compile check: OK after repair")
            return written_contents
        errors = compile_errors(output, AUTOMATION_FRAMEWORK_DIR) or errors

    log("ERROR: generated code still does not compile after one repair pass — "
        "not handing step 04 a module that cannot build.")
    for path, msgs in errors.items():
        for entry in msgs:
            log(f"  {Path(path).name}:{entry}")
    (AUDIT_DIR / "03-generate.json").write_text(json.dumps({
        "error": "compile_failed",
        "compile_errors": errors,
        "repaired_files": sorted(applied),
        "files_written": sorted(written_contents),
    }, indent=2))
    sys.exit(1)


def _repair_step_narration(files_map: dict, plan: dict) -> tuple:
    """Split a one-line summary logStep back into a line per step.

    Rule 7b tells the model to narrate each step where it happens; this is what
    happens when it writes one run-on logStep at the top of the method instead.
    It matters beyond tidiness: the run report shows one line per logStep, so a
    four-step test narrated once fails with a report that cannot say which step
    broke — and the derived intent contract, built from these same strings, ends
    up with one blob where it needs four checkable claims.

    Returns (files_map, {rel_path: {method: finding}}) — the second value is what
    is STILL under-narrated afterwards, for the audit.
    """
    expected = logstep_narration.expected_from_plan(plan)
    findings = {}
    for path, content in files_map.items():
        if not content or "src/test/" not in path.replace("\\", "/"):
            continue
        under = logstep_narration.audit(content, expected)
        # Extending an existing class returns the whole file, old methods
        # included. Those are somebody's shipped tests: re-narrating them is not
        # this run's business, and a repair pass that rewrites them would be a
        # codegen step quietly editing code it was not asked to touch.
        prior = set(test_methods_in(read_existing_file(path)))
        under = {name: f for name, f in under.items() if name not in prior}
        if under:
            findings[path] = under
    if not findings:
        return files_map, {}

    log(f"GUARD: {len(findings)} generated test class(es) narrate a multi-step "
        f"scenario in one logStep — repairing:")
    for path, methods in findings.items():
        for name, f in methods.items():
            log(f"  {Path(path).name}#{name}: {f['log_steps']} logStep(s) for "
                f"{f['expected']}+ steps")

    # The repair rewrites narration and nothing else. It used to be handed the
    # helper and page objects and told to unpack a helper call into the page calls
    # behind it, so each plan step had a call to sit in front of — which turned a
    # test written against business operations back into a page-object script.
    # A step is now one operation, so a call that carries two plan steps keeps its
    # one call and its logStep names both; what the test does may not change.
    wanted = ""
    for path, methods in findings.items():
        for name, f in methods.items():
            wanted += f"\n{Path(path).name}#{name} — currently {f['log_steps']} logStep(s):\n"
            for text in f["narration"]:
                wanted += f'    existing: "{text}"\n'
            for step in f["steps"]:
                wanted += f"    plan step: {step}\n"
            if not f["steps"]:
                wanted += (f"    (no plan steps recorded — narrate the "
                           f"{f['acting']} acting statements this method already has)\n")

    offending = "".join(
        f"\n--- {path} ---\n{files_map[path]}\n" for path in findings)

    prompt = f"""These generated Java test classes narrate a multi-step scenario with a single
summary logStep. The run report prints one line per logStep, so as written the
report shows one line for the whole test and a failure cannot be located.

Methods to fix, with the steps each one is supposed to show:
{wanted}
Rewrite the NARRATION of each test method — the config.logStep(...) lines only:
  - Every step above gets its OWN config.logStep("...") stating the action AND the
    expected outcome, placed immediately BEFORE the call that carries it out, with a
    blank line separating each step group. A step's checks follow its call.
  - Split a run-on summary logStep into one per step; do not keep it as an extra line.
  - Never add, remove, reorder, split or replace a call or an assertion. A Helper
    operation stays one call even when it carries two steps: put one logStep before
    it naming both, rather than unpacking it into the page calls behind it.
  - Setup lines (reading properties or credentials, constructing the helper) get
    no logStep.

Change NOTHING else: same calls in the same order, same assertions with the same
strength, same method signatures, same annotations, same comments and JavaDoc.
{offending}
Return ONLY a JSON object mapping each test class path above to its complete
corrected contents. No prose.
"""
    response = call_claude(prompt, label=" [narration-repair]")
    repaired = extract_json(response) or {}
    if not repaired:
        log("  narration-repair returned nothing — leaving the files as generated")
        return files_map, findings

    remaining = dict(findings)
    for path, content in repaired.items():
        if path not in findings or not (content or "").strip():
            continue
        still = {name: f for name, f in logstep_narration.audit(content, expected).items()
                 if name in findings[path]}
        # A repair that narrates no more finely than what it replaced is not a
        # repair; keeping the original avoids paying a rewrite's risk for nothing.
        before_total = sum(f["log_steps"] for f in findings[path].values())
        after_total = sum(len(logstep_narration.log_steps(body))
                          for name, body in logstep_narration.test_bodies(content).items()
                          if name in findings[path])
        if after_total <= before_total:
            log(f"  narration-repair added no steps to {Path(path).name} — keeping the original")
            continue
        # The prompt forbids touching a call; this is what holds it to that. The
        # statements that drive or check the app, in order, must be the same
        # sequence before and after — only the narration between them may move.
        acted = {name: [" ".join(a.split()) for a in logstep_narration.acting_statements(body)]
                 for name, body in logstep_narration.test_bodies(files_map[path]).items()}
        if any(acted.get(name) != [" ".join(a.split())
                                   for a in logstep_narration.acting_statements(body)]
               for name, body in logstep_narration.test_bodies(content).items()
               if name in findings[path]):
            log(f"  narration-repair REJECTED for {Path(path).name} — it changed what the "
                f"test does, not only how it is narrated")
            continue
        ok, reason = validate_fix(files_map[path], content, Path(path).name,
                                  NARRATION_REPAIR_MAX_DIFF_LINES)
        if not ok:
            log(f"  narration-repair REJECTED for {Path(path).name} — {reason}")
            continue
        files_map[path] = content
        log(f"  narration-repair applied to {Path(path).name} "
            f"({before_total} -> {after_total} logStep calls)")
        if still:
            remaining[path] = still
        else:
            remaining.pop(path, None)
    return files_map, remaining


def _build_api_hint(test_type: str, api_data: dict) -> str:
    """Turn 02-validate-api.json into a codegen hint — confirmed auth status and
    real response shapes for endpoints that were actually called, mirroring what
    selector_hint/dom_context do for web (see module docstring)."""
    if test_type not in ("api", "both") or api_data.get("skipped"):
        return ""

    lines = ["\n\nAPI validation results (from a real pre-codegen call against the live API):"]

    auth = api_data.get("auth") or {}
    auth_status = auth.get("status")
    if auth_status == "ok":
        lines.append(f"  Auth: confirmed working — {auth.get('detail')}")
    elif auth_status and auth_status != "skipped":
        lines.append(
            f"  Auth: NOT confirmed ({auth_status} — {auth.get('detail')}). "
            "Generate the auth code from the plan's api_auth as usual, but note "
            "step 04's real `mvn test` run is what will actually prove it works."
        )

    for ep in api_data.get("endpoints_checked", []):
        if ep.get("error"):
            lines.append(f"  {ep['method']} {ep['path']}: call failed — {ep['error']}")
            continue
        if ep.get("body_sent") is False:
            # Sent with no body: the status says the route exists, not how it answers
            # a real request. Advising it as the expected status is how a generated
            # test ends up asserting a 400.
            lines.append(
                f"  {ep['method']} {ep['path']}: reachable (returned {ep['actual_status']} to a "
                f"request with no body — NOT its real status; keep the plan's expected_status "
                f"{ep.get('expected_status')})")
            continue
        mark = "matched expected status" if ep.get("matched_expected") else "DID NOT match expected status"
        lines.append(
            f"  {ep['method']} {ep['path']}: real call returned {ep['actual_status']} "
            f"(expected {ep.get('expected_status')}, {mark})"
            + (f", response JSON keys: {ep['response_keys']}" if ep.get("response_keys") else "")
        )
        if not ep.get("matched_expected"):
            lines.append(
                f"    → the plan's expected_status for this endpoint may be wrong; "
                f"prefer the real observed status ({ep['actual_status']}) when generating assertions."
            )

    for ep in api_data.get("endpoints_not_checked", []):
        lines.append(f"  {ep['method']} {ep['path']}: not independently checked — {ep['reason']}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _layer_of(rel_path: str) -> int:
    """Framework layer a file belongs to, lowest dependency first.

    Batches are generated in this order so each call can be shown the real
    contents of everything it depends on: the module's option enums first, since
    data, pages and operations all take them; then data types and page objects,
    then the Helper that orchestrates them, then the test class that calls both.
    """
    name = Path(rel_path).name
    if rel_path.startswith("src/test/"):
        return 4          # test classes call helpers, pages, builders
    if name.endswith("Helper.java"):
        return 3          # helpers orchestrate page objects
    if "/web/" in rel_path:
        return 2          # page objects: the framework's BasePage, and the enums
    if name.endswith("Enums.java"):
        return 0          # option enums depend on nothing
    return 1              # Data / Builder / Api enum — at most the enums


def _batch_by_layer(files: list, size: int) -> list:
    """Group files into dependency-ordered batches of at most `size` files."""
    if size <= 0:
        return [files]
    batches = []
    for layer in sorted({_layer_of(f) for f in files}):
        in_layer = [f for f in files if _layer_of(f) == layer]
        batches += [in_layer[i:i + size] for i in range(0, len(in_layer), size)]
    return batches


def _generated_context(files_map: dict) -> str:
    """Formatted block of files earlier batches already produced, for reuse."""
    if not files_map:
        return ""
    sections = "".join(
        f"\n--- ALREADY GENERATED: {rel} ---\n{content}\n"
        for rel, content in files_map.items()
    )
    return (
        "\n\n<already_generated_this_run>\n"
        "These files were generated earlier IN THIS RUN and are already written. "
        "Call their methods by the EXACT names shown — do not invent different "
        "method, field, or locator names, and do not re-emit these files.\n"
        + sections
        + "</already_generated_this_run>"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    plan = json.loads((AUDIT_DIR / "01-parse.json").read_text())
    web_data = json.loads((AUDIT_DIR / "02-validate-web.json").read_text())
    api_data_path = AUDIT_DIR / "02-validate-api.json"
    api_data = json.loads(api_data_path.read_text()) if api_data_path.exists() else {"skipped": True}
    # Read Jarvis/CLAUDE.md — single source of truth for framework conventions.
    fw_claude_md_path = AUTOMATION_FRAMEWORK_DIR / "CLAUDE.md"
    claude_md = fw_claude_md_path.read_text() if fw_claude_md_path.exists() else ""
    if not claude_md:
        log(f"WARNING: {fw_claude_md_path} not found — check FRAMEWORK_DIR, or "
            "WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")

    feature        = plan["feature_name"]
    feature_class  = plan["feature_class"]
    test_type      = plan["test_type"]
    existing       = plan.get("existing_module", False)
    pkg_main       = plan.get("package_main", f"automation.modules.{feature}")
    pkg_test       = plan.get("package_test", f"automation.{feature}")
    country        = plan.get("country", "SG")
    user_type      = plan.get("user_type", "Admin")
    feature_enum   = plan.get("feature_enum", "CARD")
    web_pages         = plan.get("web_pages", [])
    # `.get(key, {})` only supplies the default when the KEY is absent — an
    # explicit `null` value (key present) would pass the default through and
    # crash the first `.keys()`/`.items()` call downstream, so guard both cases.
    selectors         = web_data.get("selectors") or {}
    page_elements     = web_data.get("page_elements") or {}
    interaction_hints = web_data.get("interaction_hints") or []

    # Second line of defence behind step 02's own filter: a cached
    # 02-validate-web.json written before that filter existed still carries
    # Playwright-MCP refs, and a rerun from the step cache would feed them straight into
    # codegen. A locator like [ref='f2e585'] compiles and never matches, so the
    # cost of letting one through is a 30-second timeout in step 04 with a failure
    # message that points at the page, not at the selector.
    dropped = [f"{n}={sel!r}" for n, sel in selectors.items() if not is_dom_selector(sel)]
    if dropped:
        log(f"Dropped {len(dropped)} unusable selector(s) — not real DOM selectors:")
        for entry in dropped:
            log(f"  - {entry}")
        selectors = {n: sel for n, sel in selectors.items() if is_dom_selector(sel)}
    hints_before = len(interaction_hints)
    interaction_hints = [h for h in interaction_hints if is_dom_selector(h.get("selector", ""))]
    if len(interaction_hints) != hints_before:
        log(f"Dropped {hints_before - len(interaction_hints)} unusable interaction hint(s)")

    # Step 02 records, per selector, how many elements it matched in the live page.
    # A selector it never measured may match several, which compiles fine and then
    # dies at runtime with a strict mode violation — so name them here, where they
    # are about to become locators, rather than leaving it to a step 04 timeout.
    match_counts = web_data.get("selector_match_counts") or {}
    unverified = unverified_selectors(selectors, match_counts)
    if unverified:
        log(f"NOTE: {len(unverified)} of {len(selectors)} selector(s) were never "
            f"uniqueness-verified by the browser — they may match more than one "
            f"element: {', '.join(sorted(unverified))}")

    # Apply the unverified matrix BEFORE anything reads the plan: pruning after
    # the prompt is built would leave the dropped locator in the model's context.
    raw_input = _read_raw_input()
    pruned = prune_unverified_checks(plan, web_data, raw_input)

    log(f"Generating code for {feature_class} | type={test_type} | existing={existing}")

    _guard_web_validation(test_type, web_data, selectors, page_elements, interaction_hints)
    pages_with_zero_coverage = []
    locator_gaps = {}
    if test_type in ("web", "both"):
        pages_with_zero_coverage = _warn_page_coverage(web_pages, selectors, interaction_hints)
        locator_gaps = unconfirmed_locators(web_pages, selectors, interaction_hints,
                                            web_data.get("mechanisms") or {})

    api_hint = _build_api_hint(test_type, api_data)

    refs = read_reference_files()
    # The references predate rule 7's guardrails — SauceDemoApiTest builds request
    # bodies and hardcodes data inside @Test — and a model copies an example over a
    # rule, so the block says up front which one wins.
    ref_section = ("Use these for imports, structure and framework calls only. Where one differs "
                   "from the Rules below (building request bodies or hardcoding data inside @Test, "
                   "for example), the Rules win.\n") + "\n".join(
        f"\n--- {path} ---\n{content}\n" for path, content in refs.items()
    )

    # What already exists. Codegen has no tools, so a method it is not shown is a
    # method it writes again: the shared code goes into the static prompt (it is
    # the same for every batch), the module's own index into each batch.
    shared_index = module_index.describe_shared(
        AUTOMATION_FRAMEWORK_DIR, load_repo_config().get("shared_code") or [])
    shared_section = ("\n\n<shared_code_index>\nThe framework's shared code, one public member per "
                      "line. Call these instead of writing your own (rule 11).\n"
                      f"{shared_index}\n</shared_code_index>") if shared_index else ""
    module_section = ""
    if existing:
        module_idx = module_index.describe(AUTOMATION_FRAMEWORK_DIR, [
            f"src/main/java/automation/modules/{feature.lower()}",
            f"src/test/java/automation/{feature.lower()}"])
        if module_idx:
            module_section = ("\n\n<existing_module_index>\nEvery public member this module "
                              f"already has, before this run:\n{module_idx}\n</existing_module_index>")
    option_hint = option_sets_hint(web_data, plan)

    # Build selector hint for page objects
    selector_hint = ""
    if selectors:
        from shared.locator_emit import code_for
        selector_hint = "\n\nConfirmed DOM selectors from Playwright validation:\n"
        for name, sel in selectors.items():
            code = code_for(sel)
            selector_hint += f"  {name}: {code.get('findby') or code['java']}\n"
        selector_hint += ("\nUse these exact selectors in the page object locators where they match. "
                          "A name written Page.name is for that page object only.")
    else:
        selector_hint = "\n\nNo selectors were confirmed by Playwright validation. " \
                        "Infer locators using [data-cy='...'] attribute naming convention " \
                        "based on the locator names in the plan."

    # How an action actually takes effect, when it is not a plain click. Without
    # this a page whose editor autosaves gets a click on a Save button that step
    # 02 already established does not exist.
    mechanisms = web_data.get("mechanisms") or {}
    mechanism_hint = ""
    if mechanisms:
        mechanism_hint = (
            "\n\nDISCOVERED MECHANISMS — how these actions actually take effect on "
            "the live page. The browser confirmed each one. Implement the method "
            "this way; do NOT click a control that is not in the confirmed selector "
            "list above.\n")
        for name, m in mechanisms.items():
            mechanism_hint += f"  {name}: {m['kind']}"
            if m.get("trigger"):
                mechanism_hint += f" — trigger: {m['trigger']}"
            if m.get("settles_when"):
                mechanism_hint += f"; done when: {m['settles_when']}"
            mechanism_hint += "\n"
        mechanism_hint += (
            "  For `autosave` / `blur`: move focus off the field (click a neutral "
            "element or press Tab) and then WaitHelper until the settle condition "
            "holds. For `enter_key`: press Enter in the field. For `form_submit`: "
            "submit the form. Never Thread.sleep().\n")

    # What step 02 typed and how the page rendered each compared value. The plan
    # says "assertEquals" because English said "matches"; the page is what decides.
    value_hint = value_contracts_hint(web_data, raw_input)
    # What the page kept doing after an action returned: step 02 waited for it,
    # and the generated code has to as well.
    settle_hint = delayed_updates_hint(web_data, shared_index)

    # A check the user asked for that the browser could not confirm. It stays in
    # the test at full strength — the model needs to be told that on purpose, or
    # it will "helpfully" soften it.
    kept_unverified_hint = ""
    if pruned.get("kept_unverified"):
        unmeasured = set(pruned.get("kept_unmeasured") or [])
        kept_unverified_hint = (
            "\n\nCHECKS STEP 02 COULD NOT CONFIRM — the test input explicitly asked "
            "for them:\n"
            + "".join(f"  - {s}  " + ("[reported as passing, but no locator was "
                                       "measured: build it from the DOM context]"
                                       if s in unmeasured else
                                       "[never seen on the live page: expected to fail]")
                      + "\n" for s in pruned["kept_unverified"])
            + "Generate these assertions at FULL STRENGTH anyway. Do not soften "
              "them, do not wrap them in a condition, do not turn one into a log "
              "line or a warning, and do not leave one out. If one fails, a human "
              "decides whether the product or the locator is at fault; it is never "
              "a reason to weaken the assertion.\n")

    # Build rich DOM context from live page inspection. page_elements is keyed
    # by the STEP DESCRIPTION active when the snapshot was taken (usually the
    # step that failed), not a page name — label it generically to match.
    dom_context = ""
    if page_elements:
        dom_context += "\n\nConfirmed page elements from live DOM inspection:\n"
        for context_label, elements in page_elements.items():
            dom_context += f"\nAt '{context_label}':\n"
            for el in elements[:40]:  # cap at 40 per page to avoid prompt bloat
                tag = el.get("tag", "")
                # Build a concise element description with whatever identifiers are present
                attrs = []
                if el.get("data-cy"):
                    attrs.append(f"[data-cy='{el['data-cy']}']")
                if el.get("data-testid"):
                    attrs.append(f"[data-testid='{el['data-testid']}']")
                if el.get("id"):
                    attrs.append(f"[id='{el['id']}']")
                if el.get("name"):
                    attrs.append(f"[name='{el['name']}']")
                if el.get("aria-label"):
                    attrs.append(f"[aria-label='{el['aria-label']}']")
                if el.get("placeholder"):
                    attrs.append(f"placeholder='{el['placeholder']}'")
                if el.get("type"):
                    attrs.append(f"type={el['type']}")
                if el.get("text"):
                    attrs.append(f"text='{el['text'][:40]}'")
                hint = f"  [{tag}] " + " ".join(attrs) if attrs else f"  [{tag}]"
                dom_context += hint + "\n"

    if interaction_hints:
        dom_context += "\nInteraction patterns discovered from live DOM (use these EXACT selectors):\n"
        for h in interaction_hints:
            dom_context += f"  {h['type'].upper()}: '{h['text']}' → selector: {h['selector']}\n"
        dom_context += "\nCRITICAL rules for Quasar components:\n"
        dom_context += "  - Radio buttons: use [role='radio'][aria-label='<value>'] — NOT :has-text() on the container\n"
        dom_context += "  - Dropdown options: use the exact [data-cy='...'] from interaction_hints above\n"
        dom_context += "  - Click the dropdown to open it, then click the option by its data-cy selector\n"

    # Read available CSV roles — advisory only for NEW modules.
    # For existing modules Claude must match the credential pattern already in the existing test class.
    csv_roles_hint = ""
    feature_csv = AUTOMATION_FRAMEWORK_DIR / "src" / "test" / "resources" / feature.lower() / "csvFiles" / f"{feature.lower()}-users.csv"
    if feature_csv.exists() and not existing:
        try:
            import csv as _csv
            with feature_csv.open(newline="") as f:
                rows = list(_csv.DictReader(f))
            available_roles = sorted({r.get("role", "").strip() for r in rows if r.get("role")})
            if available_roles:
                csv_roles_hint = (
                    f"\n\nCSV credentials file (new module only): {feature_csv.relative_to(AUTOMATION_FRAMEWORK_DIR)}\n"
                    f"Available roles: {available_roles}\n"
                    f"Use role='{user_type.lower()}' if it exists, otherwise the closest match.\n"
                    f"NEVER use a role string that is not in this list — it will cause a runtime error."
                )
        except Exception:
            pass

    # For a NEW web module with no CSV file, the codegen prompt below (rule 7b)
    # instructs Claude to call config.getRunTimeProperty("{feature}.username"/
    # ".password") — the SAME condition used here. Write the actual property so
    # that call resolves to a real value instead of silently returning null.
    credential_property_status = "not applicable"
    if not existing and test_type in ("web", "both") and not csv_roles_hint:
        credential_property_status = write_credential_property(
            AUTOMATION_FRAMEWORK_DIR, feature.lower(), credentials_from_plan(plan), log=log
        )

    # Every URL this module touches becomes a property BEFORE codegen, so the
    # prompt below can hand Claude keys that already resolve. Without this the
    # model has nothing to reference and writes the literal instead — which is how
    # a shipped module ended up with `private static final String LOGIN_URL =
    # "https://www.naukri.com/nlogin/login"` and no naukari entry in the file.
    url_props = url_properties.collect_urls(plan, web_data)
    url_property_status = "nothing to write"
    if url_props:
        url_property_status = url_properties.write_url_properties(
            AUTOMATION_FRAMEWORK_DIR, url_props, feature.lower(), log=log)

    props_file_name = properties_file.properties_path(AUTOMATION_FRAMEWORK_DIR).name
    url_property_hint = ""
    if url_props:
        url_property_hint = (
            f"\n\nURL properties (already written to parameters/{props_file_name} — "
            "reference these keys, never the literal URL):\n"
            + "".join(f'  config.getRunTimeProperty("{k}")  ->  {v}\n'
                      for k, v in url_props.items()))

    # Determine which files to generate / update
    files_to_generate = _plan_files(plan, test_type, existing, pkg_main, pkg_test, feature_class,
                                    feature, test_case=raw_input)

    # Read current content of files that already exist so Claude can extend them
    existing_files_context = read_existing_files_context(files_to_generate)

    # Locator syntax comes from the active framework's CodeEngine rather than
    # being spelled out in the prompt. The rule used to say "using page.locator()",
    # which is Playwright's API and would have had the model write Playwright
    # calls into a Selenium repo. A worked example beats a description here: it
    # shows the shape of a real call, in this repo's language.
    try:
        from shared.frameworks import active_framework, get_active_plugin
        _engine = get_active_plugin().code
        _sample = _engine.emit_locator(selector="[data-cy='submit']")
        _example = _sample.get("findby") or _sample.get("java") or ""
        _LOCATOR_SYNTAX_HINT = (
            f"this repo uses {active_framework()}, so a locator looks like "
            f"`{_example}`" if _example else "match the surrounding page objects")
    except Exception:
        _LOCATOR_SYNTAX_HINT = "match the syntax the surrounding page objects already use"

    # Identical for every batch and repair in this run: written once and sent as the
    # system prompt, so only the per-batch half in build_prompt changes between calls.
    static_system_prompt = f"""You are a test automation code generator for the automation repository whose conventions follow.

<framework_conventions>
{claude_md}
</framework_conventions>

<reference_implementations>
{ref_section}
</reference_implementations>{shared_section}

Rules (MANDATORY — violations will cause compilation failures):
1. Every file must compile standalone — include all necessary imports.
2. Data POJO: use @Data @NoArgsConstructor @AllArgsConstructor @JsonInclude(NON_NULL).
   Each field needs @JsonProperty("snake_case_key").
3. Builder: fluent with*() methods returning `this`. withDefaults() sets null fields.
   build() calls withDefaults() then constructs the POJO.
4. API enum: implements ApiDetails. Include withPath(String param, String value) method.
4b. CURL INTEGRATION — when an endpoint in generation_plan["api_endpoints"] has a "curl", it is the
   author's exact request, so take the details from it:
   - Query parameters (e.g. `?currencyCode=USD`) go into that enum constant's path exactly as written.
   - The `-d`/`--data` JSON body decides the Data POJO: every key in it is a field, mapped with
     @JsonProperty to that exact key.
   - Non-secret custom headers (e.g. `-H "x-client: web"`) are sent from the Helper with
     executeRaw(api, body, headers), followed by an explicit AssertHelper status assertion.
   - NEVER copy an Authorization header, bearer token, cookie or API key from a curl into Java —
     auth comes only from plan["api_auth"] (rule 5b) and properties. A token in code is a leaked secret.
5. Helper: extends ApiHelper (import automation.core.api.ApiHelper). Pass customBaseUrl to super(config, BASE_URL).
   API methods call execute()/executeAndVerify()/executeRaw().
   WEB: the page objects chain and the Helper holds them. Every page action that leaves a page
   returns the next page object, and the Helper has ONE PUBLIC FIELD PER PAGE of the module,
   named for its class (`public PaymentMethodPage paymentMethodPage;`, no initialiser). The
   test stores each page it is handed on that field, so every step continues from the page
   the previous step returned. The Helper's operations are generation_plan["helper_web_methods"]:
     a) A "stage" is the entry operation: it navigates (rule 6b), constructs the first page —
        the ONLY page object a Helper ever constructs, right after navigating — stores it on
        its field, chains through the pages the operation crosses, storing each one, and
        returns the page it lands on.
     b) A "composed" operation continues from the pages already in the Helper's fields, runs
        the page actions the plan lists in order, storing each page it passes through, and
        returns the last one. It adds nothing of its own. Write every one the plan lists, even
        though this test calls the page actions: it is for the next test, which wants the
        whole run as one call. Never `new XPage(config)` mid-flow: the page the previous step
        returned is already in its field, and rebuilding it is the chain break this rule exists
        to prevent.
     c) Choices are parameters — an option enum (rule 18) or a Data field — never part of a
        method's name, and neither is what it returns: continueToBank(), not
        continueAndGetBankAmount().
     d) Operations and page actions never assert; the test asserts on the pages they return.
     e) No result types: a check reads its value from the page the step ended on, through that
        page's getter.
     f) A page getter returns what the page shows. When a check compares it in another form (an
        amount as plain number text, a phone as its digits), the getter converts it through the
        Helper's ONE `public static` converter — `return {feature_class}Helper.toPlainAmount(getText(
        totalDisplay, "Total"));` — written once in the Helper.
        Never a private converter copied into several page objects: page objects are generated
        a batch at a time, and a run wrote the same normalizeAmount() into four of them.
5b. API AUTH — source this ONLY from plan["api_auth"].type below; never invent a different auth
   mechanism or guess at field names not present in api_auth:
   a) type == "none": no auth headers at all — do not call setAuthToken or add any auth logic.
   b) type == "bearer_token": call api_auth.login_endpoint (method/path/body_fields) to obtain a
      token, extract it via api_auth.token_json_path, then apply it using api_auth.header_name /
      api_auth.header_prefix (defaults: "Authorization" / "Bearer "). If those are the defaults,
      the framework's ApiHelper.setAuthToken(token) after construction is the normal path (see
      <reference_implementations>). If api_auth specifies a NON-default header_name, look at
      ApiHelper's real methods in <reference_implementations> for how to set an arbitrary header —
      do not assume setAuthToken covers a non-"Authorization" header.
   c) type == "basic": send HTTP Basic auth (base64 of "username:password" from demo_credentials)
      on every request — do NOT run a login call or token flow for this type.
   d) type == "api_key": send demo_credentials.api_key as a static header named by
      api_auth.header_name on every request — no login call, no token.
   If api_hint below reports the auth as already confirmed working (step 02 pre-validated it via a
   real HTTP call), it's safe to assume the recipe itself is correct — any resulting 401/403 in the
   generated test points at how this code applies auth, not at the credentials or the API.
6. Page objects: extend BasePage. Define all locators in the constructor using the
   target framework's native locator syntax — {_LOCATOR_SYNTAX_HINT}. The one exception is
   an option control's selection method (rule 18c), which builds its locator from the key.
   End the constructor with the page-loaded check <framework_conventions> prescribes.
   All interactions use BasePage methods (click, fillText, getText, isElementDisplayed).
   Page objects chain: an action that leaves the page returns the next page object
   (`return new ReceiptPage(config);`), and an action that stays on it returns `this`. When the
   option chosen decides which page comes next, the selection method returns BasePage (rule
   18c) and the caller casts. A getter returns a value.
6b. NAVIGATION — never drive the browser's navigation API directly. Use
   BrowserHelper.navigateTo(config, url), which logs the action and waits for the
   page to load afterwards.
6c. NAVIGATING AWAY AFTER AN ACTION THAT ITSELF NAVIGATES — mandatory, this is the
   single most common runtime failure in generated web code. Clicking Login/Submit
   starts a navigation. Issuing another navigation while that one is still in
   flight makes the browser abort it:
     (e.g. net::ERR_ABORTED at <url>)
   So let the first navigation settle BEFORE starting the second:
     click(loginButton, "Login button");
     WaitHelper.waitForPageLoad(config);            // let the post-login redirect finish
     BrowserHelper.navigateTo(config, PROFILE_URL); // only now navigate onwards
   Use WaitHelper.waitForNetworkIdle(config) instead when the app is a SPA or the
   submit produces no visible page transition (CLAUDE.md's own guidance: "after
   form submissions with no visible feedback").
   Note BrowserHelper.navigateTo waits AFTER navigating, not before — it does NOT
   remove the need for the wait on the line above it.
7. Test classes: extend TestBase, and import automation.core.Enums.* (QA, Country, ...).
   Use @Test(description="...", dataProvider="getConfig", groups={{...}}) with the TestBase constants:
     - web flow:    groups={{GROUP_REGRESSION, GROUP_WEB}}
     - API flow:    groups={{GROUP_REGRESSION, GROUP_API}}
     - hybrid flow: groups={{GROUP_REGRESSION, GROUP_WEB, GROUP_API}}
   Every @Test method has @TestVariables(automatedBy = QA.Mukesh).
   STRICT GUARDRAILS for @Test methods:
     - Each step continues the chain: it calls a Helper operation or a business-level page
       action on the page the previous step returned, stores the page that call returns on the
       Helper's field for it — `shop.receiptPage = shop.paymentMethodPage.pay();` — and is
       followed by that step's AssertHelper checks, read from that page. Never hold a page in a
       local variable, and never construct a page object in a test. No loops, Java Stream
       filtering or JSONPath extraction (see rule 14c).
     - Short: about {BODY_LINES_GUIDELINE} lines between the method's braces is normal. Go past
       it only when the input's own steps and checks need it; a longer test usually means small
       page actions that belong in one page method (rule 14a), or a sequence that belongs in a
       Helper operation.
     - Hide API intricacies: never build a request body (new XBuilder()...) or chain dependent API
       calls inside @Test — the Helper does it and returns the result.
     - Test data is ONE setup line: a Helper method that reads the module's data and returns the
       built Data object — `PaymentData payment = shop.buildPayment("card_with_promo");` — with the
       CSV lookup and the Builder chain inside it. Name it build*/get*: it is setup, not a step.
       Never a Builder chain, a CSV read or a hardcoded value inside @Test. Group CSV data by business entity inside the
       module's csvFiles/ folder (users.csv, products.csv), NOT by API vs web — API and web tests
       that use the same entity share one sheet. A CSV listed under "Files to generate" is
       OPTIONAL: return it only if a generated test reads from it. When extending an existing CSV,
       return the whole file with every existing row unchanged and new rows appended. Never put
       a login secret in a CSV — a password, token or API key, or an OTP the login asks for;
       those come from properties (see WEB LOGIN CREDENTIALS). Other values the flow types, a
       card number or a bank page's OTP among them, are test data and go in the CSV.
     - State isolation: one user per test; never share an account between test methods.
   LOGGING — decided by the KIND of class, never by what you want to say:
     - test class  -> config.logStep("...")            NEVER Log.step / Log.comment
     - every other class (page objects, helpers, builders)
                   -> Log.comment(config, "...")       NEVER config.logStep / Log.step
   A reference page object that calls Log.step() is a known violation, not a pattern —
   follow the rule above, not that file.
7b. STEP NARRATION — one logStep per business step: never one summary line for the whole
   test, never one per click. The run report prints ONE LINE PER logStep, and the intent
   contract is derived from these same strings.
   - Every object in this method's "steps" in <generation_plan> gets its OWN
     config.logStep("<its logstep text>"), placed immediately BEFORE its "call" and followed
     by its "checks", with a blank line between steps.
   - Setup lines — reading properties or credentials, building data, constructing the
     helper — get no logStep.
   - A check that sits between two page actions means calling them in separate steps; use
     the composed operation only when nothing is checked in between.
   WRONG — page objects driven click by click from the test, one logStep per field:
     config.logStep("Enter the card number");
     cardPage.fillCardNumber(order.getCardNumber());
     config.logStep("Enter the expiry date");
     cardPage.fillExpiry(order.getCardExpiry());
     config.logStep("Click Pay and open the receipt");
     ReceiptPage receipt = cardPage.clickPay();
   RIGHT — each business step continues from the page the previous one returned, stored on the
   Helper's field, with its checks right after it:
     config.logStep("Check out the order and verify the total matches the order amount");
     shop.paymentMethodPage = shop.checkout(order);
     AssertHelper.assertEquals(config, shop.paymentMethodPage.getTotal(), order.getAmount(), "Total should match the order amount");

     config.logStep("Pay by credit card and verify the receipt charges the same total");
     shop.cardPage = (CardPage) shop.paymentMethodPage.choosePaymentMethod(PaymentMethod.CreditCard);
     shop.receiptPage = shop.cardPage.fillCardDetails(order).pay();
     AssertHelper.assertEquals(config, shop.receiptPage.getAmount(), order.getAmount(), "Receipt should charge the checkout total");
   WEB LOGIN CREDENTIALS (not API auth — see rule 5b for that) — follow this priority order:
   a) For EXISTING modules: scan every @Test method in the existing test class shown in
      <existing_file_contents> and find how they load credentials. Copy that pattern exactly.
      Do NOT look at what methods are available on the helper — look at what the existing test
      METHODS actually call. Valid patterns (use whichever the existing methods already use):
        • config.getRunTimeProperty("feature.username") / "feature.password" → helper.doLogin(u, p)
        • user = sauceDemo.getUser("standard") → sauceDemo.doLogin(user)   (a CSV row looked up by key)
        • github.loginWithStoredSession()                          (a saved storage state)
      NEVER introduce a new credential mechanism (e.g. getCredentials(), CSV lookup, allocateUser())
      if the existing test methods don't already use it.
   b) For NEW modules where no prior test exists: use config.getRunTimeProperty("{feature.lower()}.username")
      and config.getRunTimeProperty("{feature.lower()}.password") unless a CSV file is listed above.
   c) allocateUser() is ONLY for internal applications with a DB-backed user pool. NEVER use it for
      external/3rd-party services (GitHub, SauceDemo, public APIs, etc.).
8. Locators: prefer [data-cy='...'] > [id='...'] > [name='...'] > CSS > XPath.
9. Assertions: ONLY AssertHelper.* — never Assert.*.
   Every verification step in the plan becomes a real assertion. Never express a
   check as an `if` plus a `logWarning`/`logComment`, never wrap one in a
   try/catch, and never make one conditional on the thing it is checking. Those
   all produce a test that passes without proving anything, which is worse than
   no test — a green run is read as evidence.
   Only assert on a locator in the confirmed list, or one covered by a discovered
   mechanism. If the plan names a check with neither, leave the assertion out
   rather than inventing a locator to hang it on — a guessed locator like
   `[class*='toast']` fails later and looks like a flake.
10. Waits: ONLY WaitHelper.* — never Thread.sleep().
11. REUSE BEFORE ANYTHING NEW. generation_plan["reuse"] is binding:
    - "as_is": call that existing method exactly as <existing_module_index> or
      <shared_code_index> declares it. Never write a method that does the same thing.
    - "extend": make exactly the stated "change" to that existing method, in the existing file
      shown in <existing_file_contents>. Allowed changes only: add an enum value and its case;
      add a parameter through an overload whose old signature delegates with its former value;
      read a new optional Data field, absent meaning today's behaviour; return a value where it
      returned void; extract a private step both callers share. Never change what an existing
      call does for its current callers, never remove or rename a public method or enum value,
      and never edit an existing test method.
    - Only then a new method (generation_plan["helper_web_methods"], or a page's actions). A
      call to a method that exists nowhere — not in either index, not in the plan — is a new
      method: write it. A method that would differ from an existing one only by a hard-coded
      value or choice is never new; extend the existing one.
    - Every existing file you return is COMPLETE: every other member, field, annotation, JavaDoc
      and comment exactly as it was; new members go at the end of their section.
    - Data, Builder, Api enum of an existing module: return them only when they are listed under
      "Files to generate" (the plan adds a field or an endpoint), keeping every existing member.
    - If the test class file in <files_to_generate> already exists (shown in <existing_file_contents>),
      add the new @Test method(s) to THAT class — do NOT create a separate class.
12. Preserve ALL existing JavaDoc comments, inline comments, and annotations exactly as written.
    When updating an existing file, do NOT remove, shorten, or reword any existing JavaDoc or comments.
    Only add new JavaDoc for newly added methods.
13. When reading credentials from a CSV file, use ONLY role strings that exist in that file.
    Refer to the "Available roles" list above. Using an unlisted role will cause a runtime error.
14. Helpers and page objects — put cohesive work where it belongs, so the @Test method stays short:
    a) PAGE OBJECTS: several small actions on the SAME page in a row (filling a form's five fields)
       become ONE higher-level method on that page object — `fillCheckoutDetails(data)` — and the
       test calls that once.
    b) HELPERS (business operations): opening the app and crossing pages before the first check
       is the entry operation, and a run of page actions a later test wants as one call is a
       composed operation (rule 5). The same sequence is never written twice — not in two
       tests, and not in two operations.
    c) HELPERS (non-trivial logic): JSON extraction (`response.jsonPath().getList(...)`), Java
       Stream filtering/mapping, loops and multi-step data preparation live in the Helper, which
       returns what the test asserts on. Never do them inside the @Test method.
    d) Do NOT add a thin wrapper: a method that only renames one existing call and adds nothing —
       no navigation, read, wait or page of its own. An entry operation is not one, and neither
       is a composed operation.
15. INTERLEAVED FLOWS — when generation_plan["flow_style"] == "interleaved", generate exactly ONE
    test method (do NOT split into separate Api/Web test classes) in the single test class listed
    under "Files to generate". Follow generation_plan["interleaved_steps"] IN ORDER: for each step,
    call the Helper's API methods (execute()/executeAndVerify()/etc., per rule 5) when
    "interface": "api", and the Helper's web operations (rule 5) when "interface": "web",
    following each entry's "call" and "checks" when it has them — all within one @Test method named
    generation_plan["interleaved_test_method_name"]. Data an earlier API step produced (e.g. an id
    from a create call) must be threaded into later steps exactly as a real caller would, not
    re-fetched or re-derived redundantly. For this method the Helper has both API methods and
    web operations, which is expected.
16. URLs — NEVER write a literal "http://..." or "https://..." anywhere in the Java you
    generate: not in a test, not in a page object, not in a helper, and above all not as a
    `private static final String BASE_URL = "https://..."` constant. Every URL listed under
    "URL properties" above is already in parameters/{props_file_name}; read it back instead:
      • ApiHelper base URL:  super(config, config.getRunTimeProperty("{feature}.api.url"))
                             — inline in the super() call; an instance field cannot be read there.
      • Navigation:          BrowserHelper.navigateTo(config, config.getRunTimeProperty("{feature}.login.url"))
      • A URL a class reuses: private final String profileUrl = config.getRunTimeProperty("{feature}.profile.url");
                             — an INSTANCE field (static cannot reach `config`), never a literal.
    If you need a URL that is NOT in the list above, still do not inline it: call
    config.getRunTimeProperty("{feature}.<page>.url") with a key named the same way and it will be
    added to the properties file. Pointing this module at another environment must never require
    editing Java.
17. Code quality (strict): no System.out.println, no commented-out code, no unused imports, and no
    intermediate variable whose value is never used.
18. OPTION ENUMS — for every entry in generation_plan["option_enums"]:
    a) Declare it in the module's <Feature>Enums class, `public class <Feature>Enums {{ public enum
       PaymentMethod {{ … }} }}`, imported with `import <package_main>.<Feature>Enums.*;`.
       Values in CamelCase.
    b) The values are exactly the OPTION SETS listed for that control, each constructed with its
       key and label — `CreditCard("<key>", "<label>")` — and exposing getKey() and getLabel().
       A value the page showed more than once gets a Javadoc line saying its locator is not
       unique there; never reach for .first(). With no option set recorded, list only the
       exercised values and say so in the enum's Javadoc.
    c) The page object has ONE selection method for the control. Its locator is the confirmed
       selector with only the key replaced, as the OPTION SETS hint shows, so the value the flow
       used rebuilds the measured selector exactly. A native <select> takes getKey() in its
       select call instead. When the choice keeps the user on this page, it returns `this`.
       When it decides which page comes next, it returns BasePage, switching on the value to
       construct the page each exercised value lands on; the caller casts —
       `shop.cardPage = (CardPage) shop.paymentMethodPage.choosePaymentMethod(PaymentMethod.CreditCard);`.
    d) The selection method, and any composed operation that takes the choice, does the
       follow-up steps for each value this test exercised, and every other value reaches
       `default -> throw new UnsupportedOperationException(method.getLabel() + " is not automated yet");`
       — never guessed steps for an option nobody ran.
    e) The choice is the enum wherever it travels: a parameter, or a Data field.
"""
    SYSTEM_PROMPT_FILE.write_text(static_system_prompt)

    def build_prompt(batch_files: list, generated_context: str = "") -> str:
        return f"""{csv_roles_hint}
{existing_files_context}{module_section}{generated_context}

<generation_plan>
{json.dumps(plan, indent=2)}
</generation_plan>
{selector_hint}{option_hint}{mechanism_hint}{value_hint}{settle_hint}{kept_unverified_hint}{dom_context}{api_hint}{url_property_hint}

Generate the following files (Java source, plus CSV test data where a test reads data) and return them as a single JSON object where
keys are relative file paths (from the automation repo root) and values are the complete
file contents as strings, following the Rules in your system prompt.

Files to generate:
{json.dumps(batch_files, indent=2)}

Return ONLY a JSON object, no prose:
{{
  "src/main/java/automation/modules/{feature}/{feature_class}Data.java": "...full file content...",
  "src/main/java/automation/modules/{feature}/api/{feature_class}Api.java": "...full file content...",
  "src/test/resources/{feature}/csvFiles/{feature}-data.csv": "...full CSV content, only if a test reads it...",
  "src/test/java/automation/{feature}/{feature_class}ApiTest.java": "...full file content..."
}}
"""

    batches = _batch_by_layer(files_to_generate, GENERATE_BATCH_SIZE)
    log(f"Calling Claude to generate {len(files_to_generate)} files "
        f"in {len(batches)} batch(es), {GENERATE_TIMEOUT}s budget each...")

    files_map: dict = {}
    failed_batches: list = []
    for i, batch_files in enumerate(batches, 1):
        tag = f"[batch {i}/{len(batches)}]"
        # A page batch often writes the next page too (submitOtp() returns
        # PaymentSuccessPage), so asking for it again is a call whose output is
        # discarded by the re-emit guard below.
        batch_files = [f for f in batch_files if f not in files_map]
        if not batch_files:
            log(f"  {tag} already produced by an earlier batch — skipping")
            continue
        log(f"  {tag} {', '.join(Path(f).name for f in batch_files)}")
        # Later layers must call the REAL method and locator names the earlier
        # ones just got, not names re-invented from the plan — batching without
        # this is how a test class ends up calling a helper method that the
        # helper batch never generated.
        response = call_claude(
            build_prompt(batch_files, _generated_context(files_map)),
            label=f" {tag}",
        )
        batch_map = extract_json(response)
        if not batch_map:
            # One bad batch no longer sinks the step: keep going so the audit can
            # name exactly which files are missing rather than all of them.
            log(f"  {tag} ERROR: no valid files map in response")
            failed_batches.append({"batch": i, "files": batch_files,
                                   "raw_response": response[:3000]})
            continue
        # A later batch is told not to re-emit earlier files, but if it does anyway
        # the earlier version is the one every subsequent batch was shown and wrote
        # its call sites against — keeping the re-emitted copy would silently break
        # that agreement. First writer wins.
        stale = [f for f in batch_map if f in files_map]
        for f in stale:
            batch_map.pop(f)
            log(f"  {tag} ignoring re-emitted {Path(f).name} — keeping the earlier version")
        files_map.update(batch_map)
        log(f"  {tag} returned {len(batch_map)} file(s)")

    # CSVs are optional: a scenario that reads no test data rightly returns none.
    missing = [f for f in files_to_generate if f not in files_map and not f.endswith(".csv")]
    if not files_map:
        log("ERROR: Claude did not return a valid files map")
        (AUDIT_DIR / "03-generate.json").write_text(json.dumps({
            "error": "generation_failed",
            "failed_batches": failed_batches,
        }, indent=2))
        sys.exit(1)
    if missing:
        # Writing a partial module would hand step 04 a compile error whose real
        # cause — a batch that never came back — is a whole step upstream.
        log(f"ERROR: {len(missing)} of {len(files_to_generate)} files were never generated:")
        for f in missing:
            log(f"  - {f}")
        (AUDIT_DIR / "03-generate.json").write_text(json.dumps({
            "error": "generation_incomplete",
            "files_returned": sorted(files_map),
            "files_missing": missing,
            "failed_batches": failed_batches,
        }, indent=2))
        sys.exit(1)

    # Rule 16 says no literal URLs. This is the enforcement behind the rule —
    # run before anything reaches disk, so what gets written (and committed) is
    # already property-driven.
    files_map, hardcoded_by_file = _repair_hardcoded_urls(
        files_map, url_props, feature, props_file_name)
    if hardcoded_by_file:
        log(f"WARNING: {len(hardcoded_by_file)} file(s) still hardcode a URL after "
            f"repair — recorded in 03-generate.json for review")

    # Rule 7b says one logStep per step. Same shape as the URL guard: enforced
    # here, before anything reaches disk, because a test that ships with one
    # summary logStep is only noticed when someone reads a failure report and
    # finds it says nothing.
    files_map, under_narrated = _repair_step_narration(files_map, plan)
    if under_narrated:
        log(f"WARNING: {len(under_narrated)} test class(es) still narrate several "
            f"steps in one logStep after repair — recorded in 03-generate.json")

    # Every expected value has to come from somewhere: the test case, or what step
    # 02 saw. One that came from neither was invented, and the test would fail on
    # it in step 04 — where the fix loop may not change an expected value at all.
    files_map, untraced_values = _repair_untraced_expected_values(
        files_map, raw_input, web_data)
    if untraced_values:
        log(f"WARNING: {len(untraced_values)} file(s) still expect a value nobody "
            f"stated or saw — recorded in 03-generate.json")

    # Rule 5f: a conversion lives once, in the Helper. Page objects come a batch at a
    # time, and each batch can copy what the last one wrote.
    files_map, _still_copied = _repair_copied_methods(files_map, feature_class)

    # A locator step 02 never confirmed was written from a guess. Held to what step
    # 02's helpers counted: confirmed by a count, or pointed at an element they did
    # count, or reported as still a guess.
    guesses = []
    if locator_gaps:
        evidence_rows = flow_map.read_evidence(AUDIT_DIR / "02-web-evidence.jsonl")
        files_map, guesses = _repair_guessed_locators(
            files_map, guessed_locators(files_map, locator_gaps, evidence_rows), evidence_rows)

    # A CSV is committed with the PR. A login secret in one leaves the sheet for the
    # properties file, and the code that read it is pointed at the property. What
    # cannot be moved stops the run here: a test reading a column that is no longer
    # in its sheet would only fail in step 04.
    files_map, csv_secrets, unmoved_secrets = _move_csv_secrets(
        files_map, feature, props_file_name, raw_input)
    if unmoved_secrets:
        log("ERROR: login secrets in the generated test data could not be moved out of "
            "the CSV into the properties file:")
        for path, problems in unmoved_secrets.items():
            for problem in problems:
                log(f"  - {Path(path).name}: {problem}")
        (AUDIT_DIR / "03-generate.json").write_text(json.dumps({
            "error": "csv_secrets_unmoved",
            "csv_secrets_unmoved": unmoved_secrets,
            "csv_secrets_moved": csv_secrets,
        }, indent=2))
        sys.exit(1)

    # After the secrets move, which takes columns out of the sheets: each CSV read
    # is held to the sheet it will actually read.
    files_map, csv_lookup_problems = _check_csv_lookups(files_map)
    for path, problems in csv_lookup_problems.items():
        for problem in problems:
            log(f"WARNING: {Path(path).name} {problem} — recorded in 03-generate.json")

    # The mirror-image failure: code that reads a URL property nobody ever wrote.
    # getRunTimeProperty returns null, navigation goes nowhere, and step 04 sees a
    # page that never loaded rather than a missing setting. Recover what the browser
    # can vouch for; abort on the rest rather than writing a test that cannot run.
    props_path = properties_file.properties_path(AUTOMATION_FRAMEWORK_DIR)
    known = properties_file.read_values(
        props_path.read_text() if props_path.exists() else "")
    missing_url_props = sorted({
        key for content in files_map.values()
        for key in url_properties.referenced_keys(content or "")
        if key not in known})
    if missing_url_props:
        # First, try to satisfy the key from a URL the browser actually opened.
        # collect_urls() already mints one property per visited URL, so this only
        # fires when the model named the key slightly differently from derive_key
        # — real, and cheap to repair, because the value is not a guess: it is an
        # address step 02 loaded.
        visited = {}
        for url in (web_data.get("urls_visited") or []):
            clean = url_properties.normalize(url)
            if clean:
                visited.setdefault(
                    url_properties.derive_key(feature.lower(), clean), clean)
        recovered = {k: visited[k] for k in missing_url_props if k in visited}
        if recovered:
            url_properties.write_url_properties(
                AUTOMATION_FRAMEWORK_DIR, recovered, feature.lower(), log=log)
            log(f"Recovered {len(recovered)} URL propert(ies) from the pages step 02 "
                f"actually opened: {', '.join(sorted(recovered))}")
            url_props.update(recovered)
            missing_url_props = [k for k in missing_url_props if k not in recovered]

    if missing_url_props:
        # What is left cannot be recovered: no page step 02 opened maps to this key.
        # getRunTimeProperty would return null, and the first navigation would die as
        # "url: expected string, got undefined" — a Playwright protocol error that
        # reads nothing like the missing setting it is. Guessing a value would be
        # worse than saying so, and writing the files anyway is worse than both: it
        # spends a maven run, a browser launch and a fix attempt rediscovering what
        # is already known here.
        log(f"ERROR: generated code reads {len(missing_url_props)} URL "
            f"propert(ies) that parameters/{props_file_name} does not define, and no "
            f"page step 02 opened supplies them: {', '.join(missing_url_props)}")
        log("  → FIX: add a value for each key to "
            f"parameters/{props_file_name}, or re-run step 02 so the browser visits "
            "that page and the key is minted from the URL it loaded.")
        (AUDIT_DIR / "03-generate.json").write_text(json.dumps({
            "error": "missing_url_properties",
            "missing_url_properties": missing_url_props,
            "url_properties": url_props,
            "urls_visited": list(web_data.get("urls_visited") or []),
        }, indent=2))
        sys.exit(1)

    # Existing test methods are somebody's shipped tests. Codegen and every repair
    # pass above are told not to touch them, and one still did: a repair rewrote an
    # existing assertEquals as a non-empty check. Telling is not enough, so any
    # existing @Test method that changed is put back exactly as it was.
    files_map, restored_tests = _restore_existing_tests(files_map)

    # Every existing file as it was before this run touches it. The review notes
    # diff against these copies, and step 04 reads them to find the existing tests
    # that reach a method this run (or one of its fixes) changed.
    pre_run = _snapshot_existing(files_map)

    # Write each file to Thanos-pw, saving content for per-step git commits in ship step
    written = []
    written_contents: dict = {}  # {rel_path: content} — used by 05_ship.py for step-03 commit
    unusable_by_file: dict = {}  # {rel_path: [selector, ...]} — persisted into the audit
    unsettled_by_file: dict = {}  # {rel_path: [{action_line, nav_line}, ...]}
    for rel_path, content in files_map.items():
        if not content or not content.strip():
            log(f"  Skipping empty: {rel_path}")
            continue
        # Safety check — only write inside Thanos-pw
        full_path = AUTOMATION_FRAMEWORK_DIR / rel_path
        try:
            full_path.resolve().relative_to(AUTOMATION_FRAMEWORK_DIR.resolve())
        except ValueError:
            log(f"  BLOCKED: path escapes Thanos-pw root: {rel_path}")
            continue
        if rel_path.endswith(".csv") and _is_credential_csv(
                read_existing_file(rel_path).split("\n", 1)[0], raw_input):
            # An existing credential sheet, which tests read as it is on disk. A model
            # rewriting it can mangle real passwords. New secret columns never get
            # here: _move_csv_secrets took them out of every sheet it was given.
            log(f"  KEPT: {rel_path} is an existing credential sheet — left as it is on disk")
            continue
        if rel_path.endswith(".csv"):
            lost_rows = _lost_csv_rows(read_existing_file(rel_path), content)
            if lost_rows:
                # Other tests look these rows up by key, and step 04 runs only the
                # generated test, so a dropped or edited row would ship unnoticed.
                log(f"  BLOCKED: {rel_path} would drop or change {len(lost_rows)} existing "
                    f"row(s) other tests read, e.g. {lost_rows[0][:80]!r} — keep every "
                    f"existing row as it is and append new ones")
                continue
        for a_line, a_text, n_line, n_text in unsettled_navigations(content):
            log(f"  WARNING: {Path(rel_path).name}:{n_line} navigates while the "
                f"action on line {a_line} may still be navigating — Playwright will "
                f"abort it (net::ERR_ABORTED). Add WaitHelper.waitForPageLoad(config) "
                f"between them.")
            log(f"    {a_line}: {a_text[:90]}")
            log(f"    {n_line}: {n_text[:90]}")
            unsettled_by_file.setdefault(rel_path, []).append(
                {"action_line": a_line, "nav_line": n_line})

        bad = unusable_locators(content)
        if bad:
            # Not fatal: step 04 can still repair it, and aborting codegen on a
            # heuristic would be worse. But it must be visible here rather than
            # surfacing as a page-load timeout three steps later.
            unusable_by_file[rel_path] = bad
            log(f"  WARNING: {Path(rel_path).name} contains {len(bad)} locator(s) that "
                f"cannot match a real DOM:")
            for sel in bad:
                log(f"    - {sel!r}")
        write_file(rel_path, content)
        written.append(rel_path)
        written_contents[rel_path] = content

    log(f"Generated {len(written)} files")

    # Compile before step 04 does. A wrong import is seconds to catch here and a
    # maven run, a browser launch and a fix attempt to catch there.
    written_contents = _compile_check(written_contents, pre_run)

    review = _review_records(written_contents, pre_run, plan, web_data, feature, feature_class)

    # A dropped check that reappears in the generated code is the whole pruning
    # step defeated: the locator would be guessed, the assertion would fail, and
    # step 04 would be back to choosing between a bad fix and a red test.
    resurrected = {}
    for name in (pruned.get("removed_locators") or []) + (pruned.get("removed_actions") or []):
        hits = [rel for rel, content in written_contents.items()
                if re.search(rf"\b{re.escape(name)}\b", content)]
        if hits:
            resurrected[name] = hits
    if resurrected:
        log("WARNING: names dropped as unverified-and-unrequested came back in the "
            "generated code — they will be built on a guessed locator:")
        for name, hits in resurrected.items():
            log(f"  - {name} in {', '.join(Path(h).name for h in hits)}")

    result = {
        "feature": feature,
        "feature_class": feature_class,
        "test_type": test_type,
        "existing_module": existing,
        "files_written": written,
        "files_content": written_contents,  # full content snapshot for per-step commits
        "automation_framework_dir": str(AUTOMATION_FRAMEWORK_DIR),
        "test_class": _infer_test_class(written, test_type),
        "test_method": _resolve_test_method(plan, test_type, written_contents,
                                            _infer_test_class(written, test_type)),
        # Persisted so a page that shipped with 100% guessed locators has a
        # durable trace beyond a console line that scrolls away — was silently
        # invisible before this field existed.
        "pages_with_zero_coverage": [name for name, _needed in pages_with_zero_coverage],
        # class -> locator names the plan wanted that nothing confirmed. Named
        # individually so "why did this locator get guessed?" has an answer.
        "unconfirmed_locators": locator_gaps,
        # What codegen wrote for each of those, held to step 02's counts: confirmed by
        # a count, repaired to a counted element, or still a guess.
        "guessed_locators": guesses,
        # Checks step 02 could not observe: what was dropped because nobody asked
        # for it, and what was kept because someone did (those tests fail on
        # purpose — 05 puts them in the PR body). kept_unmeasured_checks is the
        # subset step 02 reported as passing but never measured a selector for.
        "dropped_unverified_checks": pruned.get("dropped") or [],
        "resurrected_dropped_names": resurrected,
        "kept_unverified_checks": pruned.get("kept_unverified") or [],
        "kept_unmeasured_checks": pruned.get("kept_unmeasured") or [],
        # Locators generated that cannot match a real DOM. Empty is the normal
        # case; non-empty tells step 04 exactly where to look first.
        "unusable_locators": unusable_by_file,
        # Navigations issued without letting a prior one settle — the net::ERR_ABORTED
        # shape. Empty is the normal case.
        "unsettled_navigations": unsettled_by_file,
        "credential_property_status": credential_property_status,
        # Written before codegen and committed by 05_ship.py — unlike credentials,
        # a URL key that never reaches the repo breaks the test for everyone else.
        "url_properties": url_props,
        "url_property_status": url_property_status,
        # Literal URLs the repair pass could not move into properties. Empty is the
        # normal case; non-empty is a review finding, not a runtime failure.
        "hardcoded_urls": hardcoded_by_file,
        "missing_url_properties": missing_url_props,
        # Test methods whose steps are still narrated more coarsely than the plan
        # they came from. Empty is the normal case; non-empty means the run report
        # for those tests cannot say which step failed.
        "under_narrated_tests": {
            path: {name: {k: v for k, v in f.items() if k != "narration"}
                   for name, f in methods.items()}
            for path, methods in under_narrated.items()},
        # Expected values neither the test case states nor step 02 saw, left after
        # the repair pass. Empty is the normal case.
        "untraced_expected_values": untraced_values,
        # {column: property key} for login secrets moved out of a generated CSV.
        "csv_secrets_moved": csv_secrets,
        # {path: [problem, ...]} for CSV reads their sheet cannot answer. Empty is
        # the normal case.
        "csv_lookup_problems": csv_lookup_problems,
        # Which files existed before this run (their pre-run copies are in
        # pre-run/ beside this file) and which it created. Step 04 re-runs the
        # existing tests that reach a changed method of the former.
        # Existing @Test methods this run changed and _restore_existing_tests put
        # back. Empty is the normal case.
        "restored_existing_tests": restored_tests,
        "pre_run_files": sorted(pre_run),
        "created_files": sorted(p for p in written if p not in pre_run),
        # The reuse ledger as planned, and claims that named nothing real.
        "reuse": plan.get("reuse") or [],
        "reuse_unknown": plan.get("reuse_unknown") or [],
        "new_operations": [{"name": op.get("name"), "kind": op.get("kind"),
                            "why_new": op.get("why_new") or ""}
                           for op in plan.get("helper_web_methods") or []
                           if isinstance(op, dict)],
        # Review notes, never gates — see _review_records.
        **review,
    }
    (AUDIT_DIR / "03-generate.json").write_text(json.dumps(result, indent=2))

    summary_lines = [
        "# Generation Results",
        "",
        f"Feature:   {feature_class}",
        f"Test type: {test_type}",
        f"Files:     {len(written)}",
        f"Credentials property: {credential_property_status}",
        f"URL properties: {url_property_status}"
        + (f" ({', '.join(url_props)})" if url_props else ""),
        "",
        "## Files Written",
    ] + [f"- `{f}`" for f in written]
    if hardcoded_by_file:
        summary_lines += [
            "",
            "## ⚠️ Hardcoded URLs Still In Generated Code",
            f"These belong in `parameters/{props_file_name}`, read back with "
            "`config.getRunTimeProperty(...)`:",
        ] + [f"- `{path}`: {', '.join(urls)}"
             for path, urls in sorted(hardcoded_by_file.items())]
    if under_narrated:
        summary_lines += [
            "",
            "## ⚠️ Tests Narrated In One logStep",
            "The run report prints one line per `logStep`, so a failure in these "
            "methods cannot be traced to a step:",
        ] + [f"- `{Path(path).name}#{name}` — {f['log_steps']} logStep(s) for "
             f"{f['expected']}+ steps"
             for path, methods in sorted(under_narrated.items())
             for name, f in sorted(methods.items())]
    if pages_with_zero_coverage:
        summary_lines += [
            "",
            "## ⚠️ Pages Generated with ZERO Confirmed Selectors",
            "All locators below are guessed from naming conventions, not validated:",
        ] + [f"- `{name}`" for name, _needed in pages_with_zero_coverage]
    summary_lines += _review_summary(review)
    (AUDIT_DIR / "03-generate.md").write_text("\n".join(summary_lines))


def _review_summary(review: dict) -> list:
    """03-generate.md lines for the review records: guidance, never a gate."""
    lines = []
    methods = (review.get("test_shape") or {}).get("methods") or {}
    if methods:
        lines += ["", "## Test Method Shape",
                  f"Guideline: about {BODY_LINES_GUIDELINE} lines between the braces."]
        lines += [f"- `{name}` — {m['body_lines']} lines" for name, m in sorted(methods.items())]
    notes = []
    for path, diff in (review.get("modified_existing") or {}).items():
        parts = [f"{kind} {', '.join(f'`{k}`' for k in keys)}"
                 for kind, keys in diff.items() if keys]
        notes.append(f"- existing `{Path(path).name}`: " + "; ".join(parts))
    for path, gone in (review.get("changed_existing_api") or {}).items():
        notes.append(f"- ⚠️ `{Path(path).name}` lost existing API: {', '.join(gone)}")
    for group in review.get("near_duplicates") or []:
        notes.append(f"- ⚠️ {module_index.describe_duplicates(group)}")
    for found in review.get("rebuilt_pages") or []:
        notes.append(f"- ⚠️ `{found['method']}` constructs `{found['page']}` mid-flow instead of "
                     f"continuing from the page the previous step returned")
    for name, gap in (review.get("option_enum_gaps") or {}).items():
        notes.append(f"- ⚠️ enum `{name}`: missing keys {gap['missing_keys']}, "
                     f"{gap['unobserved_constants']} constant(s) the page did not offer")
    for existing in review.get("reuse_unused") or []:
        notes.append(f"- planned reuse never called: `{existing}`")
    if notes:
        lines += ["", "## Review Notes (guidance, not a gate)"] + notes
    return lines


def _restore_existing_tests(files_map: dict) -> tuple:
    """Put back every existing @Test method a generated test class changed. (files_map, restored).

    Compared on comment-free text with whitespace collapsed, so re-indentation is
    not a change; anything else is, however small. The method is restored from the
    file on disk, member for member, and the run's new methods stay.
    """
    restored = {}
    for path, content in list(files_map.items()):
        if not (path.startswith("src/test/") and path.endswith(".java") and content):
            continue
        before = read_existing_file(path)
        if not before:
            continue
        old = {m["name"]: m["text"] for m in split_class_members(before)
               if m["kind"] == "method" and re.search(r"@Test\b", m["text"])}
        new = {m["name"]: m["text"] for m in split_class_members(content)
               if m["kind"] == "method" and re.search(r"@Test\b", m["text"])}
        names = []
        for name, old_text in old.items():
            new_text = new.get(name)
            if new_text is None or new_text == old_text:
                continue
            if " ".join(without_comments(new_text).split()) == " ".join(without_comments(old_text).split()):
                continue
            content = content.replace(new_text, old_text, 1)
            names.append(name)
        if names:
            files_map[path] = content
            restored[path] = names
            log(f"GUARD: {Path(path).name} changed existing test method(s) "
                f"{', '.join(names)} — restored exactly as they were")
    return files_map, restored


def _snapshot_existing(files_map: dict) -> dict:
    """{path: content before this run} for every existing source file about to be overwritten.

    Also written to `pre-run/<path>` in the audit dir: step 04 runs in another
    process, and its own fixes are diffed against the same copies.
    """
    pre_run = {}
    for rel_path in files_map:
        if not rel_path.endswith(".java"):
            continue
        before = read_existing_file(rel_path)
        if not before:
            continue
        pre_run[rel_path] = before
        copy = AUDIT_DIR / "pre-run" / rel_path
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(before)
    return pre_run


def _review_records(written_contents: dict, pre_run: dict, plan: dict, web_data: dict,
                    feature: str, feature_class: str) -> dict:
    """What a reviewer needs to know about this run's code. Notes, never gates.

    - test_shape: each new @Test's length. About BODY_LINES_GUIDELINE lines is the
      norm, not a limit.
    - rebuilt_pages: a Helper method this run wrote that constructs a page object
      mid-flow, where it should continue from the page the previous step returned
      (rule 5): the chain break the page fields exist to prevent.
    - modified_existing: per existing file, the methods this run changed, added or
      removed. A slight change is allowed; a reviewer still has to see it.
    - changed_existing_api: public signatures and enum constants that disappeared.
      Callers written against them no longer compile, or no longer mean the same.
    - near_duplicates: a new method whose calls repeat an existing one's with only
      literals different — the copy the reuse ladder forbids.
    - option_enum_gaps: options the page offered that the enum lacks, and constants
      nothing on the page backs.
    - reuse_unused: "as_is" entries of the reuse ledger that no generated file calls —
      the plan said an existing method does this, and the code did something else.
    """
    feature_lower = feature.lower()
    module_rel = f"src/main/java/automation/modules/{feature_lower}"
    module_dir = AUTOMATION_FRAMEWORK_DIR / module_rel

    page_classes = {Path(p).stem for p in written_contents if "/web/" in p}
    if (module_dir / "web").is_dir():
        page_classes |= {f.stem for f in (module_dir / "web").glob("*.java")}
    test_shape = {}
    for path, content in written_contents.items():
        if path.startswith("src/test/") and path.endswith(".java"):
            prior = set(test_methods_in(pre_run.get(path, "")))
            for name, measured in logstep_narration.shape(content, prior).items():
                test_shape[f"{Path(path).stem}#{name}"] = measured

    modified, lost = {}, {}
    for path, before in pre_run.items():
        after = written_contents.get(path)
        if after is None:
            continue
        diff = module_index.changed_methods(before, after)
        if any(diff.values()):
            modified[path] = diff
        gone = module_index.lost_api(before, after)
        if gone:
            lost[path] = gone

    new_sources = {p: c for p, c in written_contents.items()
                   if p.startswith("src/main/") and p.endswith(".java")}
    only = {p: ((modified.get(p) or {}).get("added", []) + (modified.get(p) or {}).get("changed", []))
            if p in pre_run else module_index.method_keys(c)
            for p, c in new_sources.items()}
    existing_sources = {}
    if module_dir.is_dir():
        for f in sorted(module_dir.rglob("*.java")):
            rel = str(f.relative_to(AUTOMATION_FRAMEWORK_DIR))
            if rel in pre_run:
                existing_sources[rel] = pre_run[rel]
            elif rel not in written_contents:
                existing_sources[rel] = f.read_text(encoding="utf-8", errors="ignore")
    near = module_index.near_duplicates(new_sources, existing_sources, only=only)
    rebuilt = [{"file": Path(p).name, **found}
               for p, c in sorted(new_sources.items()) if p.endswith("Helper.java")
               for found in module_index.rebuilt_pages(c, page_classes, only=only.get(p))]

    gaps = {}
    enums_path = f"{module_rel}/{feature_class}Enums.java"
    enums_src = written_contents.get(enums_path) or read_existing_file(enums_path)
    sets = web_data.get("option_sets") or {}
    for enum in plan.get("option_enums") or []:
        if not isinstance(enum, dict) or not enums_src:
            continue
        found = sets.get(f"{enum.get('page')}.{enum.get('control')}") or sets.get(enum.get("control") or "")
        if not found:
            continue
        missing = [o["key"] for o in found["options"] if f'"{o["key"]}"' not in enums_src]
        constants = next((v for k, v in module_index.enum_constants(enums_src).items()
                          if k.rsplit(".", 1)[-1] == enum.get("name")), [])
        extra = max(0, len(constants) - len(found["options"]))
        if missing or extra:
            gaps[enum.get("name")] = {"missing_keys": missing, "unobserved_constants": extra}

    generated = "\n".join(written_contents.values())
    unused = []
    for entry in plan.get("reuse") or []:
        if not isinstance(entry, dict) or entry.get("how") != "as_is":
            continue
        ref = module_index.parse_member_reference(str(entry.get("existing") or ""))
        if ref and not re.search(rf"\b{re.escape(ref[1])}\s*\(", generated):
            unused.append(entry.get("existing"))

    long_tests = {k: v for k, v in test_shape.items() if v["body_lines"] > BODY_LINES_GUIDELINE}
    for name, measured in long_tests.items():
        log(f"NOTE: {name} is {measured['body_lines']} lines (guideline ~{BODY_LINES_GUIDELINE})")
    for found in rebuilt:
        log(f"WARNING: {found['method']} constructs {found['page']} mid-flow instead of "
            f"continuing from the page the previous step returned")
    for path, diff in modified.items():
        log(f"NOTE: existing {Path(path).name} — changed {len(diff['changed'])}, "
            f"added {len(diff['added'])}, removed {len(diff['removed'])} method(s)")
    for path, gone in lost.items():
        log(f"WARNING: {Path(path).name} lost existing API: {', '.join(gone)}")
    for group in near:
        log(f"WARNING: {module_index.describe_duplicates(group).replace('`', '')}")
    for name, gap in gaps.items():
        log(f"WARNING: enum {name} — missing keys {gap['missing_keys']}, "
            f"{gap['unobserved_constants']} constant(s) the page did not offer")
    for existing in unused:
        log(f"NOTE: the plan reuses {existing} as it is, but no generated file calls it")
    return {
        "test_shape": {"guideline_lines": BODY_LINES_GUIDELINE, "methods": test_shape},
        "modified_existing": modified,
        "changed_existing_api": lost,
        "near_duplicates": near,
        "rebuilt_pages": rebuilt,
        "option_enum_gaps": gaps,
        "reuse_unused": unused,
    }


def _find_existing_test_class(feature_lower: str, test_type: str) -> str:
    """
    Look for an existing test class to ADD to rather than creating a new file.
    Returns the relative path (from repo root) if found, empty string otherwise.

    Selection rules:
    - test_type == "api"         → prefer *ApiTest.java
    - test_type == "web"         → prefer *WebTest.java or *LoginTest.java (anything without "Api" in stem)
    - test_type == "interleaved" → prefer *FlowTest.java (see _plan_files' interleaved branch)
    - Multiple matches           → alphabetically first (deterministic)
    """
    test_dir = AUTOMATION_FRAMEWORK_DIR / "src" / "test" / "java" / "automation" / feature_lower
    if not test_dir.exists():
        return ""

    candidates = sorted(test_dir.glob("*Test.java"))  # alphabetical = deterministic

    if test_type == "api":
        for f in candidates:
            if "Api" in f.stem:
                return str(f.relative_to(AUTOMATION_FRAMEWORK_DIR))
        # Fallback: any test class
        return str(candidates[0].relative_to(AUTOMATION_FRAMEWORK_DIR)) if candidates else ""

    if test_type == "web":
        # Prefer explicit Web/Login classes; skip Api classes
        for f in candidates:
            if "Api" not in f.stem:
                return str(f.relative_to(AUTOMATION_FRAMEWORK_DIR))
        return ""  # all found classes are Api ones — create a new Web class

    if test_type == "interleaved":
        for f in candidates:
            if "Flow" in f.stem:
                return str(f.relative_to(AUTOMATION_FRAMEWORK_DIR))
        return ""  # no existing flow class — create a new one

    return ""  # "both"/parallel → caller handles api + web separately


def _is_credential_csv(header: str, test_case: str = "") -> bool:
    """Whether a CSV header row has a login-secret column (secret_columns)."""
    return bool(secret_columns(header, test_case))


def _move_csv_secrets(files_map: dict, feature: str, props_file_name: str,
                      test_case: str) -> tuple:
    """Move login secrets out of generated CSVs into the properties file.

    A CSV is committed with the PR, and the properties file's secrets never are.
    Refusing the whole sheet was the old answer, and it left the test that reads it
    pointing at a file that was never written: a payment run lost every value of
    its test case that way, and step 04 then made up its own. Now only the secret
    columns leave. Their values go to `{feature}.<column>` properties, the rest of
    the sheet is written, and one repair pass points the code that read each column
    at its property. A column the sheet on disk already had is left alone.

    Returns (files_map, {column: key} moved, {path: [problem, ...]} unresolved).
    The caller aborts on anything unresolved: a test reading a column that is no
    longer there would only fail in step 04.
    """
    moved, values, unresolved = {}, {}, {}
    for path, content in list(files_map.items()):
        if not path.endswith(".csv") or not content:
            continue
        already = set(secret_columns(read_existing_file(path).split("\n", 1)[0], test_case))
        new = [c for c in secret_columns(content.split("\n", 1)[0], test_case)
               if c not in already]
        stripped, taken, kept = take_secret_columns(content, new)
        for column, why in kept.items():
            unresolved.setdefault(path, []).append(
                f"column {column!r} {why}; move them to parameters/{props_file_name} by hand")
        if not taken:
            continue
        files_map[path] = stripped
        for column, value in taken.items():
            moved[column] = secret_property_key(feature, column)
            values[moved[column]] = value
        log(f"  Moved login secret column(s) {', '.join(taken)} out of {Path(path).name} "
            f"into parameters/{props_file_name}")
    if not moved:
        return files_map, moved, unresolved
    properties_file.upsert(properties_file.properties_path(AUTOMATION_FRAMEWORK_DIR), values,
                           f"{feature} test secrets (auto-added by test-authoring-agent)", log)

    readers = {p: c for p, c in files_map.items()
               if p.endswith(".java") and c and any(f'"{col}"' in c for col in moved)}
    if readers:
        table = "".join(f'  "{col}" -> config.getRunTimeProperty("{key}")\n'
                        for col, key in moved.items())
        files = "".join(f"\n--- {p} ---\n{c}\n" for p, c in readers.items())
        prompt = f"""These CSV columns hold login secrets. A CSV is committed with the pull
request, so they were taken out of the CSV and put in parameters/{props_file_name}, which
is not committed:

{table}
Rewrite each file below so it reads each of these values from its property instead of
from the CSV row. Change NOTHING else: same methods, same signatures, same data flow.
{files}
Return ONLY a JSON object mapping each file path above to its complete corrected
contents. No prose.
"""
        repaired = extract_json(call_claude(prompt, label=" [csv-secret-repair]")) or {}
        for path, content in repaired.items():
            if path not in readers or not (content or "").strip():
                continue
            # The same budget as the URL repair: a property lookup swapped in for a read.
            ok, reason = validate_fix(readers[path], content, Path(path).name,
                                      URL_REPAIR_MAX_DIFF_LINES)
            if ok:
                files_map[path] = content
            else:
                log(f"  csv-secret-repair REJECTED for {Path(path).name} — {reason}")
    for path, content in files_map.items():
        still = [col for col in moved if path.endswith(".java") and f'"{col}"' in (content or "")]
        if still:
            unresolved.setdefault(path, []).append(
                f"still reads {', '.join(still)} from the CSV row instead of its property")
    return files_map, moved, unresolved


def _lost_csv_rows(existing: str, updated: str) -> list:
    """Rows of an existing CSV that the regenerated file no longer contains.

    The prompt asks for every existing row back unchanged; this checks it. Where a
    new row lands is free, so rows are matched as a multiset, not by position. A
    new column at the end is free too: a row survives when its fields still lead
    some row, which leaves every column other tests read, by name or by index,
    exactly as it was. Comparing whole lines refused every added column while the
    test that reads it was written anyway, so step 04 met a null and paid for a fix
    that wrote back the very file this had refused.
    """
    def rows(text):
        return [[field.strip() for field in row] for row in csv.reader(io.StringIO(text))
                if any(field.strip() for field in row)]

    pool, lost = rows(updated), []
    for row in rows(existing):
        # ponytail: linear scan per row, fine for test-data CSVs; index by leading
        # fields if one ever grows to thousands of rows.
        hit = next((i for i, new in enumerate(pool) if new[:len(row)] == row), None)
        if hit is None:
            lost.append(",".join(row))
        else:
            pool.pop(hit)
    return lost


# The column the environment-aware CSV read matches its extra argument against.
_CSV_ENVIRONMENT_COLUMN = "environment"


def _csv_path(module: str, csv_file: str) -> str:
    """Where a module's CSV read by name lives, relative to the framework root."""
    return f"src/test/resources/{module}/csvFiles/{csv_file}.csv"


def _check_csv_lookups(files_map: dict) -> tuple:
    """Hold every generated CSV read to the sheet it reads, before anything runs.

    The test and its sheet come out of one batch, and nothing compiled checks that
    they agree, so a mismatch only showed in step 04 as "CSV row not found". The
    one that recurs is the environment-aware read, copied from a reference Helper
    whose sheets carry an environment column, pointed at a new sheet with none: no
    row can ever match. Data the same in every environment is right not to have
    the column, so the filter is what goes. The rest — a sheet nobody wrote, a key
    column the header lacks, a literal key no row has — are recorded, not guessed.

    Returns (files_map, {path: [problem, ...]}).
    """
    from shared.entry_path import csv_reads

    def table(text):
        rows = [[field.strip() for field in row] for row in csv.reader(io.StringIO(text or ""))
                if any(field.strip() for field in row)]
        return (rows[0], rows[1:]) if rows else ([], [])

    problems = {}
    for path, content in list(files_map.items()):
        if not path.endswith(".java") or not content:
            continue
        name = Path(path).name
        for read in reversed(csv_reads(content)):
            sheet = _csv_path(read["module"], read["file"])
            on_disk = read_existing_file(sheet)
            header, rows = table(files_map.get(sheet) or on_disk)
            label = f"{read['file']}.csv"
            if not header:
                problems.setdefault(path, []).append(
                    f"reads {label}, which neither this run nor the repository has")
                continue
            if read["environment_scoped"] and _CSV_ENVIRONMENT_COLUMN not in header \
                    and _CSV_ENVIRONMENT_COLUMN not in table(on_disk)[0]:
                start, end = read["filter_span"]
                content = content[:start] + content[end:]
                log(f"  CSV lookup in {name} filters by environment, but {label} has no "
                    f"{_CSV_ENVIRONMENT_COLUMN} column — dropped the filter")
            if read["column"] not in header:
                problems.setdefault(path, []).append(
                    f"looks rows up by {read['column']!r}, which {label} has no column for")
                continue
            column = header.index(read["column"])
            if read["value"] and not any(
                    len(row) > column and row[column].lower() == read["value"].lower()
                    for row in rows):
                problems.setdefault(path, []).append(
                    f"looks up {read['column']}={read['value']!r}, which no row of {label} has")
        files_map[path] = content
    return files_map, problems


def _plan_csv_files(feature_lower: str, test_case: str = "") -> list:
    """The module's test-data CSVs codegen may extend, or the one it may create.

    Credential sheets are left out: a model regenerating one can mangle real
    passwords, and 05_ship commits whatever step 03 wrote.
    """
    csv_dir = AUTOMATION_FRAMEWORK_DIR / "src" / "test" / "resources" / feature_lower / "csvFiles"
    data = []
    for path in sorted(csv_dir.glob("*.csv")):
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                header = handle.readline()
        except OSError:
            continue
        if not _is_credential_csv(header, test_case):
            data.append(str(path.relative_to(AUTOMATION_FRAMEWORK_DIR)))
    return data or [_csv_path(feature_lower, f"{feature_lower}-data")]


def _find_existing_helper(feature_lower: str, feature_class: str) -> str:
    """The module's existing Helper, so an existing module gains methods instead of a
    second Helper named after one scenario (NaukriProfileSummaryHelper beside NaukriHelper).

    `{feature_class}Helper.java` when it exists, otherwise the shortest `*Helper.java`
    name — the module's own helper rather than a specialised one like CheckoutApiHelper.
    "" when the module has none.
    """
    module_dir = (AUTOMATION_FRAMEWORK_DIR / "src" / "main" / "java" / "automation"
                  / "modules" / feature_lower)
    exact = module_dir / f"{feature_class}Helper.java"
    candidates = sorted(module_dir.glob("*Helper.java"), key=lambda p: (len(p.stem), p.stem))
    chosen = exact if exact.is_file() else (candidates[0] if candidates else None)
    return str(chosen.relative_to(AUTOMATION_FRAMEWORK_DIR)) if chosen else ""


def _extended_files(plan: dict, feature_lower: str) -> list:
    """The module's existing files that a "reuse" entry with how=extend changes.

    The entry names a class; the file it lives in is found on disk inside this
    module. A class elsewhere — the framework's shared code — is never edited by
    an authoring run, so it is not listed even when an entry names it.
    """
    module_dir = (AUTOMATION_FRAMEWORK_DIR / "src" / "main" / "java" / "automation"
                  / "modules" / feature_lower)
    found = []
    for entry in plan.get("reuse") or []:
        if not isinstance(entry, dict) or entry.get("how") != "extend":
            continue
        ref = module_index.parse_member_reference(str(entry.get("existing") or ""))
        if not ref:
            continue
        owner = ref[0].split(".")[0]
        for path in sorted(module_dir.rglob(f"{owner}.java")) if module_dir.is_dir() else []:
            found.append(str(path.relative_to(AUTOMATION_FRAMEWORK_DIR)))
    return found


def _plan_files(plan, test_type, existing, pkg_main, pkg_test, feature_class, feature,
                test_case: str = "") -> list:
    """Build the list of files that need to be generated or updated."""
    files = []
    feature_lower = feature.lower()

    if not existing:
        # New module — generate the full set from scratch
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Data.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Builder.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Helper.java")
        # The API enum has one entry per endpoint, so a web test with none has
        # nothing to put in it. Asked for one anyway, the model declined to invent
        # an interface, and the file it never wrote aborted the step.
        if test_type in ("api", "both") or plan.get("api_endpoints"):
            files.append(f"src/main/java/automation/modules/{feature_lower}/api/{feature_class}Api.java")
        if test_type in ("web", "both"):
            for page_def in plan.get("web_pages", []):
                class_name = page_def["class_name"]
                files.append(f"src/main/java/automation/modules/{feature_lower}/web/{class_name}.java")
    else:
        # Existing module — update Helper + all page objects required by this scenario
        # (existing page objects are always included so Claude can ADD new methods to them)
        files.append(_find_existing_helper(feature_lower, feature_class)
                     or f"src/main/java/automation/modules/{feature_lower}/{feature_class}Helper.java")
        if test_type in ("web", "both"):
            for page_def in plan.get("web_pages", []):
                class_name = page_def["class_name"]
                page_path = f"src/main/java/automation/modules/{feature_lower}/web/{class_name}.java"
                files.append(page_path)
        # A field the plan adds has to land somewhere. Leaving Data and Builder out
        # for every existing module meant a new optional field — one of the small
        # changes the reuse ladder allows — could never be written.
        if plan.get("data_fields"):
            files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Data.java")
            files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Builder.java")
        files.extend(_extended_files(plan, feature_lower))

    # Option enums live in one class per module, new or existing alike.
    if plan.get("option_enums"):
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Enums.java")
    files = list(dict.fromkeys(files))

    # Test classes — for existing modules, prefer adding to an existing class.
    # Interleaved "both" flows get ONE combined test class instead of the usual
    # separate Api/Web pair — see 01_parse.py rule 7b for how flow_style is set.
    if test_type == "both" and plan.get("flow_style") == "interleaved":
        existing_flow = _find_existing_test_class(feature_lower, "interleaved") if existing else ""
        if existing_flow:
            log(f"  Reusing existing flow test class: {existing_flow}")
            files.append(existing_flow)
        else:
            files.append(f"src/test/java/automation/{feature_lower}/{feature_class}FlowTest.java")
        return files + _plan_csv_files(feature_lower, test_case)

    if test_type in ("api", "both"):
        existing_api = _find_existing_test_class(feature_lower, "api") if existing else ""
        if existing_api:
            log(f"  Reusing existing API test class: {existing_api}")
            files.append(existing_api)
        else:
            files.append(f"src/test/java/automation/{feature_lower}/{feature_class}ApiTest.java")

    if test_type in ("web", "both"):
        existing_web = _find_existing_test_class(feature_lower, "web") if existing else ""
        if existing_web:
            log(f"  Reusing existing web test class: {existing_web}")
            files.append(existing_web)
        else:
            files.append(f"src/test/java/automation/{feature_lower}/{feature_class}WebTest.java")

    return files + _plan_csv_files(feature_lower, test_case)


def _infer_test_class(written: list, test_type: str) -> str:
    """Find the primary test class name from the written files."""
    test_paths = [p for p in written if p.endswith("Test.java") and "src/test" in p]

    # Prefer the best match for the type first
    for path in test_paths:
        stem = Path(path).stem
        if test_type == "api" and "Api" in stem:
            return stem
        if test_type == "web" and ("Web" in stem or "Api" not in stem):
            return stem
        if test_type == "both" and "Api" in stem:
            return stem

    # Fallback: first test class found (handles reused classes like GitHubLoginTest)
    return Path(test_paths[0]).stem if test_paths else ""


def _resolve_test_method(plan: dict, test_type: str, written_contents: dict,
                         test_class_name: str) -> str:
    """The @Test method to run, read from the code that was actually generated.

    The plan's method_name is only a request. Claude frequently names the method
    something equivalent but different — plan said toggleDotAndVerifyProfileSummary,
    generated code declared toggleDotInProfileSummaryAndVerify — and handing the
    plan's name to `mvn -Dtest=Class#method` then matches nothing. Surefire calls
    that BUILD SUCCESS with "Tests run: 0", which step 04 read as a pass and
    shipped as APPROVED: a green PR for a test that never executed.

    Falls back to the plan only when the source cannot be read, so behaviour is
    unchanged for anything this regex does not understand.
    """
    planned = _infer_test_method(plan, test_type)

    # written_contents is keyed by RELATIVE PATH while the caller identifies the
    # class by its simple name (_infer_test_class returns Path(...).stem), so match
    # on the stem. Looking it up by name directly always missed, silently fell back
    # to the planned name, and left the original bug in place.
    source = ""
    for rel, content in (written_contents or {}).items():
        if Path(rel).stem == test_class_name:
            source = content
            break
    if not source:
        return planned

    names = test_methods_in(source)
    if not names:
        log(f"  WARNING: no @Test method found in {test_class_name} — "
            f"falling back to the planned name {planned!r}")
        return planned
    if planned in names:
        return planned
    chosen = names[0]
    if planned:
        log(f"  Test method: generated code declares {chosen!r}, the plan asked for "
            f"{planned!r} — using the generated name, which is what mvn can run")
    return chosen


def _infer_test_method(plan: dict, test_type: str) -> str:
    """Find the first test method name from the plan."""
    if test_type == "both" and plan.get("flow_style") == "interleaved":
        return plan.get("interleaved_test_method_name", "")
    if test_type in ("api", "both"):
        methods = plan.get("api_test_methods", [])
        if methods:
            return methods[0].get("method_name", "")
    if test_type == "web":
        methods = plan.get("web_test_methods", [])
        if methods:
            return methods[0].get("method_name", "")
    return ""


if __name__ == "__main__":
    main()
