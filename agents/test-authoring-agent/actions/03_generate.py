#!/usr/bin/env python3
"""
Step 03 — Generate
Uses Claude to generate all required Java files for the feature module and
writes them directly into the Thanos-pw repository.

For new modules: creates Data, Builder, Helper, Api enum, Page objects, Test classes.
For existing modules: adds new methods / new test class only.

Reads:  $AUDIT_DIR/01-parse.json
        $AUDIT_DIR/02-validate-web.json
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
    """Read an existing file from Thanos-pw if it exists (for append mode)."""
    full = AUTOMATION_FRAMEWORK_DIR / rel_path
    return full.read_text() if full.exists() else ""


def write_file(rel_path: str, content: str) -> None:
    """Write a file into Thanos-pw, creating parent directories as needed."""
    full = AUTOMATION_FRAMEWORK_DIR / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    log(f"  Wrote: {rel_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    plan = json.loads((AUDIT_DIR / "01-parse.json").read_text())
    web_data = json.loads((AUDIT_DIR / "02-validate-web.json").read_text())
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
    selectors         = web_data.get("selectors", {})
    page_elements     = web_data.get("page_elements", {})
    interaction_hints = web_data.get("interaction_hints", [])

    log(f"Generating code for {feature_class} | type={test_type} | existing={existing}")

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

    # Build rich DOM context from live page inspection
    dom_context = ""
    if page_elements:
        dom_context += "\n\nConfirmed page elements from live DOM inspection:\n"
        for page_name, elements in page_elements.items():
            dom_context += f"\n{page_name} page:\n"
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

    # Determine which files to generate
    files_to_generate = _plan_files(plan, test_type, existing, pkg_main, pkg_test, feature_class, feature)

    prompt = f"""You are a Java test automation code generator for the Jarvis framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<reference_implementations>
{ref_section}
</reference_implementations>

<generation_plan>
{json.dumps(plan, indent=2)}
</generation_plan>
{selector_hint}{dom_context}

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
   For token auth: call setAuthToken(token) on the helper after construction.
   Web methods only if they orchestrate 2+ page objects.
6. Page objects: extend BasePage. Define all locators in constructor using page.locator().
   Call waitUntilLoaded() LAST in constructor. waitUntilLoaded() uses WaitHelper.
   All interactions use BasePage methods (click, fillText, getText, isElementDisplayed).
   Navigation methods return the next page object.
7. Test classes: extend TestBase. Use @Test(dataProvider="getConfig", groups={{...}}).
   Every @Test method has @TestVariables(automatedBy = QA.Mukesh, country = Country.{country}).
   Use allocateUser(config, UserType.{user_type}, Feature.{feature_enum}, Country.{country}).
   Use config.logStep() in test methods only.
8. Locators: prefer [data-cy='...'] > [id='...'] > [name='...'] > CSS > XPath.
9. Assertions: ONLY AssertHelper.* — never Assert.*.
10. Waits: ONLY WaitHelper.* — never Thread.sleep().
11. For existing modules: only generate new test class + new helper methods.
    Do NOT regenerate existing Data/Builder/Api files.

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

    # Write each file to Thanos-pw
    written = []
    for rel_path, content in files_map.items():
        if not content or not content.strip():
            log(f"  Skipping empty: {rel_path}")
            continue
        # Safety check — only write into Thanos-pw
        full_path = AUTOMATION_FRAMEWORK_DIR / rel_path
        try:
            full_path.resolve().relative_to(AUTOMATION_FRAMEWORK_DIR.resolve())
        except ValueError:
            log(f"  BLOCKED: path escapes Thanos-pw root: {rel_path}")
            continue
        write_file(rel_path, content)
        written.append(rel_path)

    log(f"Generated {len(written)} files")

    result = {
        "feature": feature,
        "feature_class": feature_class,
        "test_type": test_type,
        "existing_module": existing,
        "files_written": written,
        "automation_framework_dir": str(AUTOMATION_FRAMEWORK_DIR),
        "test_class": _infer_test_class(written, test_type),
        "test_method": _infer_test_method(plan, test_type),
    }
    (AUDIT_DIR / "03-generate.json").write_text(json.dumps(result, indent=2))

    summary_lines = [
        "# Generation Results",
        "",
        f"Feature:   {feature_class}",
        f"Test type: {test_type}",
        f"Files:     {len(written)}",
        "",
        "## Files Written",
    ] + [f"- `{f}`" for f in written]
    (AUDIT_DIR / "03-generate.md").write_text("\n".join(summary_lines))


def _plan_files(plan, test_type, existing, pkg_main, pkg_test, feature_class, feature) -> list:
    """Build the list of files that need to be generated."""
    files = []
    feature_lower = feature.lower()

    if not existing:
        # New module — full set
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Data.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Builder.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Helper.java")
        files.append(f"src/main/java/automation/modules/{feature_lower}/api/{feature_class}Api.java")
        if test_type in ("web", "both"):
            for page_def in plan.get("web_pages", []):
                class_name = page_def["class_name"]
                files.append(f"src/main/java/automation/modules/{feature_lower}/web/{class_name}.java")
    else:
        # Existing module — only add to helper (new methods) if web/both
        files.append(f"src/main/java/automation/modules/{feature_lower}/{feature_class}Helper.java")
        if test_type in ("web", "both"):
            for page_def in plan.get("web_pages", []):
                class_name = page_def["class_name"]
                page_path = f"src/main/java/automation/modules/{feature_lower}/web/{class_name}.java"
                if not (AUTOMATION_FRAMEWORK_DIR / page_path).exists():
                    files.append(page_path)

    # Test classes — always new files (even for existing modules)
    if test_type in ("api", "both"):
        files.append(f"src/test/java/automation/{feature_lower}/{feature_class}ApiTest.java")
    if test_type in ("web", "both"):
        files.append(f"src/test/java/automation/{feature_lower}/{feature_class}WebTest.java")

    return files


def _infer_test_class(written: list, test_type: str) -> str:
    """Find the primary test class name from the written files."""
    for path in written:
        if path.endswith("Test.java") and "src/test" in path:
            class_name = Path(path).stem
            if test_type == "api" and "Api" in class_name:
                return class_name
            if test_type == "web" and "Web" in class_name:
                return class_name
            if test_type == "both" and "Api" in class_name:
                return class_name
    # Fallback to first test class found
    for path in written:
        if path.endswith("Test.java") and "src/test" in path:
            return Path(path).stem
    return ""


def _infer_test_method(plan: dict, test_type: str) -> str:
    """Find the first test method name from the plan."""
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
