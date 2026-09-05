#!/usr/bin/env python3
"""
Step 03 — Generate
Uses Claude to generate all required Java files for the feature module and
writes them directly into the Thanos-pw repository.

For new modules: creates Data, Builder, Helper, Api enum, Page objects, Test classes.
For existing modules: adds new methods / new test class only.

When plan["flow_style"] == "interleaved" (set by 01_parse.py when a test_type=="both"
input describes ONE sequence mixing real API and web actions, rather than two
independent flows), generates a single combined test class following
plan["interleaved_steps"]'s order instead of separate Api/Web test classes.

Reads:  $AUDIT_DIR/01-parse.json
        $AUDIT_DIR/02-validate-web.json
        $AUDIT_DIR/02-validate-api.json (if present — API validation hints)
Writes: Java files into Thanos-pw repo
        $AUDIT_DIR/03-generate.json
        $AUDIT_DIR/03-generate.md
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*

from shared import workspace as workspace_helper

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
AUTOMATION_FRAMEWORK_DIR    = workspace_helper.resolve(
    WORKSPACE_DIR, os.environ.get("GITHUB_REPO_AUTOMATION", ""),
    exclude=REPO_ROOT)

MODEL = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")
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
# Diff budget for the URL repair pass. Swapping a literal for a property lookup is
# a handful of lines per URL; anything past this is the model rewriting a file it
# was asked only to de-hardcode.
URL_REPAIR_MAX_DIFF_LINES = int(os.environ.get("URL_REPAIR_MAX_DIFF_LINES", "60"))

# ── Helpers ───────────────────────────────────────────────────────────────────

from shared.log import log as _log
def log(msg: str) -> None: _log("03-generate", msg)

from shared.claude import call_claude_ex as _call_claude_ex
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
    )
    if not result.ok:
        log(f"ERROR: Claude call{label} {result.describe()}")
        # A timeout still carries whatever arrived before the kill; handing it back
        # lets extract_json() salvage a complete object when the model had already
        # finished and was only idling on the wire.
        return result.stdout if result.status == "timeout" else ""
    return result.stdout


def extract_json(text: str):
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def read_reference_files() -> dict:
    """Read reference implementation files from Jarvis to show Claude the patterns."""
    ref_paths = [
        "src/main/java/automation/modules/github/GitHubData.java",
        "src/main/java/automation/modules/github/GitHubBuilder.java",
        "src/main/java/automation/modules/github/GitHubHelper.java",
        "src/main/java/automation/modules/github/api/GitHubApi.java",
        "src/main/java/automation/modules/saucedemo/SauceDemoHelper.java",
        "src/main/java/automation/core/api/ApiHelper.java",
        "src/test/java/automation/github/GitHubApiTest.java",
        "src/test/java/automation/github/GitHubLoginTest.java",   # shows correct credential pattern
        "src/test/java/automation/saucedemo/SauceDemoWebTest.java",
    ]
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
        "The files below ALREADY EXIST in the repo. "
        "You MUST preserve every existing method, field, import, and JavaDoc exactly. "
        "Only ADD new methods/locators required for this scenario — do not remove or rewrite anything.\n"
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
from shared.credential_extraction import credentials_from_plan  # noqa: E402
from shared.page_identity import is_dom_selector  # noqa: E402
from shared.test_catalog import test_methods_in  # noqa: E402
# URLs are the same story as credentials — one place decides the property file and
# the key names — except that URLs are not secrets, so 05_ship.py commits them.
from shared import properties_file, url_properties  # noqa: E402
from shared.edit_guards import validate_fix  # noqa: E402
from shared import check_provenance  # noqa: E402


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

    Returns {"dropped": [...], "kept_unverified": [...]} for the audit trail.
    """
    unverified = web_data.get("steps_unverified") or []
    if not unverified:
        return {"dropped": [], "kept_unverified": []}

    dropped, kept = [], []
    for entry in unverified:
        step = entry.split("|", 1)[0].strip()
        if check_provenance.droppable(step, raw_input):
            dropped.append(step)
        else:
            kept.append(step)

    for step in kept:
        log(f"UNVERIFIED but asked for — keeping the assertion for {step!r}. The "
            f"generated test WILL fail here: the product did not do this.")

    if not dropped:
        return {"dropped": [], "kept_unverified": kept}

    # What to remove: the locator names and accessor names whose subject matches a
    # dropped check. `successToast` and `isSuccessToastVisible` both share "toast"
    # with "Verify a success confirmation toast appears".
    subjects = [check_provenance.subject_words(s) for s in dropped]
    confirmed = set(web_data.get("selectors") or {})

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

    for method in plan.get("web_test_methods") or []:
        steps = method.get("steps") or []
        keep = []
        for step in steps:
            if (check_provenance.shape(step) == check_provenance.VERIFICATION
                    and any(check_provenance.subject_words(step) & subj
                            for subj in subjects)):
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

    return {"dropped": dropped, "kept_unverified": kept,
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
        missing = [n for n in (page.get("locators_needed") or []) if n not in covered]
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
        if needed and not (confirmed & set(needed)):
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
    """
    violations = {path: found for path, content in files_map.items()
                  if content and (found := url_properties.hardcoded_urls(content))}
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
        f"\n--- {path} ---\n{files_map[path]}\n" for path in violations)
    prompt = f"""These generated Java files hardcode URLs. Every URL below is already a
property in parameters/{props_file_name}:

{key_table}
Rewrite each file so no literal "http://" or "https://" string remains in the code,
reading the URL from its property instead:
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
        still = url_properties.hardcoded_urls(content)
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
    contents of everything it depends on: page objects and data types first,
    then the Helper that orchestrates them, then the test class that calls both.
    """
    name = Path(rel_path).name
    if rel_path.startswith("src/test/"):
        return 3          # test classes call helpers, pages, builders
    if name.endswith("Helper.java"):
        return 2          # helpers orchestrate page objects
    if "/web/" in rel_path:
        return 1          # page objects depend only on the framework's BasePage
    return 0              # Data / Builder / Api enum — no intra-module deps


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
    # Playwright-MCP refs, and a TESTING_MODE rerun would feed them straight into
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
    ref_section = "\n".join(
        f"\n--- {path} ---\n{content}\n" for path, content in refs.items()
    )

    # Build selector hint for page objects
    selector_hint = ""
    if selectors:
        selector_hint = "\n\nConfirmed DOM selectors from Playwright validation:\n"
        for name, sel in selectors.items():
            selector_hint += f"  {name} = page.locator(\"{sel}\");\n"
        selector_hint += "\nUse these exact selectors in the page object locators where they match."
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

    # A check the user asked for that the browser could not observe. It stays in
    # the test at full strength and the test fails — the model needs to be told
    # that on purpose, or it will "helpfully" soften it.
    kept_unverified_hint = ""
    if pruned.get("kept_unverified"):
        kept_unverified_hint = (
            "\n\nCHECKS THAT WILL FAIL, ON PURPOSE — step 02 could not observe "
            "these on the live page, but the test input explicitly asked for them:\n"
            + "".join(f"  - {s}\n" for s in pruned["kept_unverified"])
            + "Generate these assertions at FULL STRENGTH anyway. Do not soften "
              "them, do not wrap them in a condition, do not turn one into a log "
              "line or a warning, and do not leave one out. The test failing here "
              "is the correct and intended outcome: it reports that the product "
              "does not do what was asked. A human decides what happens next.\n")

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
    files_to_generate = _plan_files(plan, test_type, existing, pkg_main, pkg_test, feature_class, feature)

    # Read current content of files that already exist so Claude can extend them
    existing_files_context = read_existing_files_context(files_to_generate)

    def build_prompt(batch_files: list, generated_context: str = "") -> str:
        return f"""You are a Java test automation code generator for the Jarvis framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<reference_implementations>
{ref_section}
</reference_implementations>
{csv_roles_hint}
{existing_files_context}{generated_context}

<generation_plan>
{json.dumps(plan, indent=2)}
</generation_plan>
{selector_hint}{mechanism_hint}{kept_unverified_hint}{dom_context}{api_hint}{url_property_hint}

Generate the following Java files and return them as a single JSON object where
keys are relative file paths (from Thanos-pw repo root) and values are the complete
file contents as strings.

Files to generate:
{json.dumps(batch_files, indent=2)}

Rules (MANDATORY — violations will cause compilation failures):
1. Every file must compile standalone — include all necessary imports.
2. Data POJO: use @Data @NoArgsConstructor @AllArgsConstructor @JsonInclude(NON_NULL).
   Each field needs @JsonProperty("snake_case_key").
3. Builder: fluent with*() methods returning `this`. withDefaults() sets null fields.
   build() calls withDefaults() then constructs the POJO.
4. API enum: implements ApiDetails. Include withPath(String param, String value) method.
5. Helper: extends ApiHelper (import automation.core.api.ApiHelper). Pass customBaseUrl to super(config, BASE_URL).
   API methods call execute()/executeAndVerify()/executeRaw().
   Web methods only if they orchestrate 2+ page objects.
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
6. Page objects: extend BasePage. Define all locators in constructor using page.locator().
   Call waitUntilLoaded() LAST in constructor. waitUntilLoaded() uses WaitHelper.
   All interactions use BasePage methods (click, fillText, getText, isElementDisplayed).
   Navigation methods return the next page object.
6b. NAVIGATION — never call page.navigate() directly. Use
   BrowserHelper.navigateTo(config, url), which logs the action and waits for the
   page to load afterwards.
6c. NAVIGATING AWAY AFTER AN ACTION THAT ITSELF NAVIGATES — mandatory, this is the
   single most common runtime failure in generated web code. Clicking Login/Submit
   starts a navigation. Issuing another navigation while that one is still in
   flight makes Playwright abort it:
     com.microsoft.playwright.PlaywrightException: net::ERR_ABORTED at <url>
   So let the first navigation settle BEFORE starting the second:
     click(loginButton, "Login button");
     WaitHelper.waitForPageLoad(config);            // let the post-login redirect finish
     BrowserHelper.navigateTo(config, PROFILE_URL); // only now navigate onwards
   Use WaitHelper.waitForNetworkIdle(config) instead when the app is a SPA or the
   submit produces no visible page transition (CLAUDE.md's own guidance: "after
   form submissions with no visible feedback").
   Note BrowserHelper.navigateTo waits AFTER navigating, not before — it does NOT
   remove the need for the wait on the line above it.
7. Test classes: extend TestBase. Use @Test(dataProvider="getConfig", groups={{...}}).
   Every @Test method has @TestVariables(automatedBy = QA.Mukesh).
   Use config.logStep() in test methods only.
   WEB LOGIN CREDENTIALS (not API auth — see rule 5b for that) — follow this priority order:
   a) For EXISTING modules: scan every @Test method in the existing test class shown in
      <existing_file_contents> and find how they load credentials. Copy that pattern exactly.
      Do NOT look at what methods are available on the helper — look at what the existing test
      METHODS actually call. Valid patterns (use whichever the existing methods already use):
        • config.getRunTimeProperty("feature.username") / "feature.password" → github.doLogin(u, p)
        • github.loginWithStoredSession()
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
11. For existing modules:
    - Data, Builder, Api enum: do NOT regenerate — omit them from your output entirely.
    - Helper, page objects, AND any existing test class shown in <existing_file_contents>:
      Return the COMPLETE file with ALL existing methods/fields/annotations kept intact.
      ADD your new methods/locators at the end of the appropriate section.
      Do NOT remove, rename, or rewrite any existing method — only append.
    - If the test class file in <files_to_generate> already exists (shown in <existing_file_contents>),
      add the new @Test method(s) to THAT class — do NOT create a separate class.
12. Preserve ALL existing JavaDoc comments, inline comments, and annotations exactly as written.
    When updating an existing file, do NOT remove, shorten, or reword any existing JavaDoc or comments.
    Only add new JavaDoc for newly added methods.
13. When reading credentials from a CSV file, use ONLY role strings that exist in that file.
    Refer to the "Available roles" list above. Using an unlisted role will cause a runtime error.
14. Helpers — do NOT add thin convenience wrapper methods that simply chain existing calls with no
    additional logic. For example: a method that only calls getCredentials(role) then doLogin() adds
    zero value — the test can call those two methods directly. Only add helper methods when they
    genuinely orchestrate ≥2 distinct page objects or encapsulate non-trivial multi-step logic.
15. INTERLEAVED FLOWS — when generation_plan["flow_style"] == "interleaved", generate exactly ONE
    test method (do NOT split into separate Api/Web test classes) in the single test class listed
    under "Files to generate". Follow generation_plan["interleaved_steps"] IN ORDER: for each step,
    call the Helper's API methods (execute()/executeAndVerify()/etc., per rule 5) when
    "interface": "api", and drive the Page Objects via the Helper's web orchestration methods
    (per rule 6) when "interface": "web" — all within one @Test method named
    generation_plan["interleaved_test_method_name"]. Data an earlier API step produced (e.g. an id
    from a create call) must be threaded into later steps exactly as a real caller would, not
    re-fetched or re-derived redundantly. For this method, the Helper is EXPECTED to have both API
    methods (rule 5) and web orchestration methods (rule 6) — that is correct here, not a violation
    of rule 5's "web methods only if they orchestrate 2+ page objects" guidance, since the method
    orchestrates real cross-interface state, not just page objects.
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

Return ONLY a JSON object, no prose:
{{
  "src/main/java/automation/modules/{feature}/{feature_class}Data.java": "...full file content...",
  "src/main/java/automation/modules/{feature}/api/{feature_class}Api.java": "...full file content...",
  "src/test/java/automation/{feature}/{feature_class}ApiTest.java": "...full file content..."
}}
"""

    batches = _batch_by_layer(files_to_generate, GENERATE_BATCH_SIZE)
    log(f"Calling Claude to generate {len(files_to_generate)} Java files "
        f"in {len(batches)} batch(es), {GENERATE_TIMEOUT}s budget each...")

    files_map: dict = {}
    failed_batches: list = []
    for i, batch_files in enumerate(batches, 1):
        tag = f"[batch {i}/{len(batches)}]"
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

    missing = [f for f in files_to_generate if f not in files_map]
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

    # The mirror-image failure: code that reads a URL property nobody ever wrote.
    # getRunTimeProperty returns null, navigation goes nowhere, and step 04 sees a
    # page that never loaded rather than a missing setting. Guessing a value would
    # be worse than saying so.
    props_path = properties_file.properties_path(AUTOMATION_FRAMEWORK_DIR)
    known = properties_file.read_values(
        props_path.read_text() if props_path.exists() else "")
    missing_url_props = sorted({
        key for content in files_map.values()
        for key in url_properties.referenced_keys(content or "")
        if key not in known})
    if missing_url_props:
        log(f"WARNING: generated code reads {len(missing_url_props)} URL "
            f"propert(ies) that parameters/{props_file_name} does not define — "
            f"add a value for: {', '.join(missing_url_props)}")

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
        # Checks step 02 could not observe: what was dropped because nobody asked
        # for it, and what was kept because someone did (those tests fail on
        # purpose — 05 puts them in the PR body).
        "dropped_unverified_checks": pruned.get("dropped") or [],
        "resurrected_dropped_names": resurrected,
        "kept_unverified_checks": pruned.get("kept_unverified") or [],
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
    if pages_with_zero_coverage:
        summary_lines += [
            "",
            "## ⚠️ Pages Generated with ZERO Confirmed Selectors",
            "All locators below are guessed from naming conventions, not validated:",
        ] + [f"- `{name}`" for name, _needed in pages_with_zero_coverage]
    (AUDIT_DIR / "03-generate.md").write_text("\n".join(summary_lines))


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


def _plan_files(plan, test_type, existing, pkg_main, pkg_test, feature_class, feature) -> list:
    """Build the list of files that need to be generated or updated."""
    files = []
    feature_lower = feature.lower()

    if not existing:
        # New module — generate the full set from scratch
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Data.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Builder.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Helper.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/api/{feature_class}Api.java")
        if test_type in ("web", "both"):
            for page_def in plan.get("web_pages", []):
                class_name = page_def["class_name"]
                files.append(f"src/main/java/automation/modules/{feature_lower}/web/{class_name}.java")
    else:
        # Existing module — update Helper + all page objects required by this scenario
        # (existing page objects are always included so Claude can ADD new methods to them)
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Helper.java")
        if test_type in ("web", "both"):
            for page_def in plan.get("web_pages", []):
                class_name = page_def["class_name"]
                page_path = f"src/main/java/automation/modules/{feature_lower}/web/{class_name}.java"
                files.append(page_path)

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
        return files

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

    return files


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
