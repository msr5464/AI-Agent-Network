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

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR = Path(os.environ["AUDIT_DIR"])
AGENT_DIR = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
AUTOMATION_FRAMEWORK_DIR    = WORKSPACE_DIR / os.environ.get("GITHUB_REPO_AUTOMATION", "Jarvis")

MODEL = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")

# ── Helpers ───────────────────────────────────────────────────────────────────

from shared.log import log as _log
def log(msg: str) -> None: _log("03-generate", msg)

from shared.claude import call_claude as _call_claude
def call_claude(prompt: str) -> str:
    output = _call_claude(prompt, MODEL, str(REPO_ROOT), timeout=900)
    if not output:
        log("ERROR: Claude CLI returned empty response")
    return output


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
from shared.credential_properties import write_credential_property  # noqa: E402


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
        log("WARNING: Jarvis/CLAUDE.md not found — check WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")

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

    log(f"Generating code for {feature_class} | type={test_type} | existing={existing}")

    _guard_web_validation(test_type, web_data, selectors, page_elements, interaction_hints)
    pages_with_zero_coverage = []
    if test_type in ("web", "both"):
        pages_with_zero_coverage = _warn_page_coverage(web_pages, selectors, interaction_hints)

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
            AUTOMATION_FRAMEWORK_DIR, feature.lower(), plan.get("demo_credentials", {}), log=log
        )

    # Determine which files to generate / update
    files_to_generate = _plan_files(plan, test_type, existing, pkg_main, pkg_test, feature_class, feature)

    # Read current content of files that already exist so Claude can extend them
    existing_files_context = read_existing_files_context(files_to_generate)

    prompt = f"""You are a Java test automation code generator for the Jarvis framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<reference_implementations>
{ref_section}
</reference_implementations>
{csv_roles_hint}
{existing_files_context}

<generation_plan>
{json.dumps(plan, indent=2)}
</generation_plan>
{selector_hint}{dom_context}{api_hint}

Generate the following Java files and return them as a single JSON object where
keys are relative file paths (from Thanos-pw repo root) and values are the complete
file contents as strings.

Files to generate:
{json.dumps(files_to_generate, indent=2)}

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

Return ONLY a JSON object, no prose:
{{
  "src/main/java/automation/modules/{feature}/{feature_class}Data.java": "...full file content...",
  "src/main/java/automation/modules/{feature}/api/{feature_class}Api.java": "...full file content...",
  "src/test/java/automation/{feature}/{feature_class}ApiTest.java": "...full file content..."
}}
"""

    log("Calling Claude to generate Java files...")
    response = call_claude(prompt)
    files_map = extract_json(response)

    if not files_map:
        log("ERROR: Claude did not return a valid files map")
        (AUDIT_DIR / "03-generate.json").write_text(json.dumps({
            "error": "generation_failed",
            "raw_response": response[:3000]
        }, indent=2))
        sys.exit(1)

    # Write each file to Thanos-pw, saving content for per-step git commits in ship step
    written = []
    written_contents: dict = {}  # {rel_path: content} — used by 05_ship.py for step-03 commit
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
        write_file(rel_path, content)
        written.append(rel_path)
        written_contents[rel_path] = content

    log(f"Generated {len(written)} files")

    result = {
        "feature": feature,
        "feature_class": feature_class,
        "test_type": test_type,
        "existing_module": existing,
        "files_written": written,
        "files_content": written_contents,  # full content snapshot for per-step commits
        "automation_framework_dir": str(AUTOMATION_FRAMEWORK_DIR),
        "test_class": _infer_test_class(written, test_type),
        "test_method": _infer_test_method(plan, test_type),
        # Persisted so a page that shipped with 100% guessed locators has a
        # durable trace beyond a console line that scrolls away — was silently
        # invisible before this field existed.
        "pages_with_zero_coverage": [name for name, _needed in pages_with_zero_coverage],
        "credential_property_status": credential_property_status,
    }
    (AUDIT_DIR / "03-generate.json").write_text(json.dumps(result, indent=2))

    summary_lines = [
        "# Generation Results",
        "",
        f"Feature:   {feature_class}",
        f"Test type: {test_type}",
        f"Files:     {len(written)}",
        f"Credentials property: {credential_property_status}",
        "",
        "## Files Written",
    ] + [f"- `{f}`" for f in written]
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
