#!/usr/bin/env python3
"""
Step 01 — Parse
Reads the plain-text input file from the queue, calls Claude to extract a
structured generation plan, and detects whether the target feature module
already exists in Thanos-pw.

Reads:  $INPUT_FILE  (plain text in queue/)
Writes: $AUDIT_DIR/01-parse.json
        $AUDIT_DIR/01-parse.md
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # repo root → platform.*

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))
INPUT_FILE = Path(os.environ["INPUT_FILE"])

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
AUTOMATION_FRAMEWORK_DIR    = WORKSPACE_DIR / os.environ.get("GITHUB_REPO_AUTOMATION", "Jarvis")

MODEL = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")

# ── Helpers ───────────────────────────────────────────────────────────────────

from shared.log import log as _log
def log(msg: str) -> None: _log("01-parse", msg)

from shared.claude import call_claude as _call_claude
def call_claude(prompt: str) -> str:
    output = _call_claude(prompt, MODEL, str(REPO_ROOT), timeout=600)
    if not output:
        log("ERROR: Claude CLI returned empty response")
    return output


def extract_json(text: str):
    """Extract JSON from Claude response — tries ```json block first, then bare JSON."""
    m = re.search(r"```json\s*([\s\S]*?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Bare JSON object
    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return None


def normalize_module_name(raw: str) -> str:
    """Turn the input's `Module:` value into a legal Java package segment.

    The module name reaches the filesystem as a directory and the source as a
    package segment, so anything the author writes naturally ("Naukari", "My
    Feature", "user-profile") has to survive as a lowercase identifier.
    """
    name = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    if name and name[0].isdigit():
        name = f"m_{name}"      # a package segment cannot start with a digit
    return name


def module_exists(feature_name: str) -> bool:
    """Check if the feature module already exists in Thanos-pw."""
    module_dir = AUTOMATION_FRAMEWORK_DIR / "src/main/java/automation/modules" / feature_name.lower()
    return module_dir.exists()


def read_existing_module_files(feature_name: str) -> dict:
    """Read existing module files to give Claude context when appending."""
    module_dir = AUTOMATION_FRAMEWORK_DIR / "src/main/java/automation/modules" / feature_name.lower()
    if not module_dir.exists():
        return {}
    files = {}
    for f in module_dir.rglob("*.java"):
        rel = f.relative_to(AUTOMATION_FRAMEWORK_DIR)
        try:
            files[str(rel)] = f.read_text()[:3000]  # truncate large files
        except Exception:
            pass
    return files


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log(f"Reading input: {INPUT_FILE}")
    raw_text = INPUT_FILE.read_text()

    # Load Jarvis CLAUDE.md (the single source of truth for framework conventions).
    # For parsing we only need structure/naming rules, not full Java examples, so
    # trim at the first ```java block to keep the prompt compact.
    fw_claude_md_path = AUTOMATION_FRAMEWORK_DIR / "CLAUDE.md"
    fw_claude_md_full = fw_claude_md_path.read_text() if fw_claude_md_path.exists() else ""
    if not fw_claude_md_full:
        log("WARNING: Jarvis/CLAUDE.md not found — check WORKSPACE_DIR and GITHUB_REPO_AUTOMATION")
    java_block_pos = fw_claude_md_full.find("```java")
    claude_md = fw_claude_md_full[:java_block_pos].strip() if java_block_pos > 0 else fw_claude_md_full

    # The input's `Module:` line names the module — it decides the package and the
    # directory on disk. It is NOT a hint: letting the model pick its own
    # feature_name is how a "Module: Naukari" input ended up in
    # modules/naukri_profile_summary, which then made the existence check below
    # (keyed on the Module: name) miss forever, so every rerun regenerated the
    # module from scratch instead of appending to it.
    module_name = ""
    for line in raw_text.splitlines():
        if line.lower().startswith("module:"):
            module_name = normalize_module_name(line.split(":", 1)[1])
            break
    if module_name:
        log(f"Module name from input's 'Module:' line: '{module_name}'")
    else:
        # No Module: line — fall back to the queue filename, as before.
        module_name = normalize_module_name(INPUT_FILE.stem)
        log(f"No 'Module:' line in input — falling back to filename: '{module_name}'")

    existing = module_exists(module_name)
    existing_files = read_existing_module_files(module_name) if existing else {}
    log(f"Module '{module_name}' exists: {existing}")

    existing_context = ""
    if existing_files:
        existing_context = "\n\n<existing_module_files>\n"
        for path, content in existing_files.items():
            existing_context += f"\n--- {path} ---\n{content}\n"
        existing_context += "</existing_module_files>"

    prompt = f"""You are a QA automation planning agent for the Jarvis Java framework.

<framework_conventions>
{claude_md}
</framework_conventions>

<input_file>
{raw_text}
</input_file>
{existing_context}

Analyze the input and produce a structured JSON generation plan. The plan must include:

{{
  "feature_name": "payments",
  "feature_class": "Payment",
  "test_type": "both",
  "country": "SG",
  "user_type": "Admin",
  "feature_enum": "CARD",
  "package_main": "automation.modules.payments",
  "package_test": "automation.payments",
  "existing_module": false,
  "web_base_url": "https://app.staging.example.com",
  "api_base_url": "https://api.staging.example.com",
  "api_auth": {{
    "type": "bearer_token",
    "login_endpoint": {{"method": "POST", "path": "/v1/auth/login", "body_fields": {{"username": "username", "password": "password"}}}},
    "token_json_path": "token",
    "header_name": "Authorization",
    "header_prefix": "Bearer "
  }},
  "data_fields": [
    {{"name": "recipientId", "type": "String", "json_key": "recipient_id", "response_only": false}},
    {{"name": "amount",      "type": "String", "json_key": "amount",       "response_only": false}},
    {{"name": "currency",    "type": "String", "json_key": "currency",     "response_only": false}},
    {{"name": "id",          "type": "String", "json_key": "id",           "response_only": true}},
    {{"name": "status",      "type": "String", "json_key": "status",       "response_only": true}}
  ],
  "builder_defaults": [
    {{"field": "currency", "default_value": "SGD"}}
  ],
  "api_endpoints": [
    {{"enum_name": "CreatePayment", "method": "POST",   "path": "/v1/payments",       "expected_status": 201}},
    {{"enum_name": "GetPayment",    "method": "GET",    "path": "/v1/payments/{{id}}", "expected_status": 200, "path_params": ["id"]}}
  ],
  "api_test_methods": [
    {{
      "method_name": "createAndVerifyPayment",
      "description": "Create a payment and verify it is returned by GET",
      "steps": [
        "allocate Admin user",
        "setAuthToken",
        "build PaymentData with amount 100 and currency SGD",
        "call createPayment and assertNotNull id",
        "call getPayment by id and assertEquals status PENDING"
      ]
    }}
  ],
  "web_pages": [
    {{
      "class_name": "PaymentListPage",
      "locators_needed": ["newPaymentButton", "paymentList"],
      "actions_needed": ["clickNewPayment", "isPaymentVisible"]
    }},
    {{
      "class_name": "PaymentFormPage",
      "locators_needed": ["recipientField", "amountField", "currencyDropdown", "submitButton", "successMessage"],
      "actions_needed": ["fillRecipient", "fillAmount", "selectCurrency", "submit", "isSuccessMessageVisible", "createPayment"]
    }}
  ],
  "web_test_methods": [
    {{
      "method_name": "createPaymentViaUI",
      "description": "Create a payment via UI and verify success message",
      "steps": [
        "allocate Admin user",
        "build PaymentData",
        "doLogin -> DashboardPage",
        "createPaymentViaUI(dashboard, payment) -> PaymentFormPage",
        "assertTrue isSuccessMessageVisible"
      ]
    }}
  ],
  "helper_api_methods": [
    {{"name": "createPayment",  "endpoint_enum": "CreatePayment", "returns": "PaymentData", "body": "PaymentData"}},
    {{"name": "getPayment",     "endpoint_enum": "GetPayment",    "returns": "PaymentData", "path_param": "id"}}
  ],
  "helper_web_methods": [
    {{"name": "createPaymentViaUI", "navigates_through": ["DashboardPage", "PaymentListPage", "PaymentFormPage"]}}
  ],
  "web_steps_for_validation": [
    "Navigate to login page at the base URL",
    "Login with test credentials",
    "Navigate to the feature page",
    "Perform the main action",
    "Verify the result"
  ],
  "flow_style": "parallel",
  "interleaved_steps": [],
  "interleaved_test_method_name": "",
  "type_resolution": null
}}

Rules:
1. Set "existing_module" to {str(existing).lower()} (already detected from filesystem).
1b. "feature_name" MUST be exactly "{module_name}" — it comes from the input's `Module:` line and
   names the package and directory on disk. Do NOT substitute a more descriptive name, correct its
   spelling, or derive one from the scenario. Set "package_main" to "automation.modules.{module_name}"
   and "package_test" to "automation.{module_name}" to match.
   "feature_class" is separate and SHOULD describe the scenario (e.g. a "Module: naukari" input about
   profile summaries gives feature_name "naukari" with feature_class "NaukriProfileSummary").
2. "feature_enum" must be one of: CARD, BUDGET, CLAIM, DBS_SG, DBS_HK, CC_SG, CALASTONE_SG.
   Pick the closest match; if unsure use CARD.
3. "response_only": true for fields set by the server (id, status, createdAt, updatedAt).
4. Infer "web_steps_for_validation" from the plain English web steps for use in the
   Playwright validation script — list them as simple imperative sentences.
5. If "existing_module" is true and the input only adds new test scenarios (not new endpoints):
   still list EVERY endpoint this scenario actually calls in "api_endpoints" — including ones that
   already exist in the module's Api enum — because step [02/05] Validate API needs the full list to
   pre-validate auth and real endpoint reachability, regardless of whether any Java file gets
   regenerated for them. Leaving "api_endpoints" empty here silently disables that pre-validation for
   the single most common real case (adding a scenario to an existing module). Regeneration of
   Data.java/Builder.java/the Api enum is controlled separately by "existing_module" itself (see rule
   11 in the generation step) — it does NOT depend on this list being empty, so there is no
   regeneration risk in populating it. Only "data_fields" should be [] here, and only if no NEW
   request/response field is needed beyond what the existing Data.java already has.
5b. For any GET endpoint in "api_endpoints" that has "path_params", also set
   "sample_path_params" — a dict mapping each param name to a REAL, LITERAL example value —
   whenever the input text actually gives one, e.g. "GET /users/octocat" implies
   {{"username": "octocat"}} for a path "/users/{{username}}". Step [02/05] Validate API uses
   this to make the real call and confirm the endpoint genuinely works right now, instead of
   just noting that it exists. Only set a value when the input gives an actual literal — never
   invent or guess one. Omit "sample_path_params" (or leave a param out of it) whenever the
   real value only exists at test-run time — e.g. an id returned by an earlier create call in
   THIS SAME scenario — that case has nothing safe to substitute during parse-time validation
   and is correctly left for step 04's real test run to exercise instead.
6. "api_auth" — only relevant when test_type includes "api". This is the concrete recipe
   step [02/05] Validate API uses to actually authenticate against the real API before
   codegen, and what setAuthToken(token) in the generated Helper is ultimately wired to.
   Infer it from the input; do NOT invent an endpoint that isn't implied by the text.
     "type": one of "none" | "bearer_token" | "basic" | "api_key".
     - "none": no other api_auth fields matter; omit login_endpoint/token_json_path.
     - "bearer_token": set "login_endpoint" (method/path/body_fields — body_fields maps each
       JSON body key the login call needs to which demo_credentials field supplies it, e.g.
       {{"email": "username", "pwd": "password"}}) and "token_json_path" — a dot-path into the
       login response JSON locating the token (e.g. "token" or "data.access_token").
     - "basic": HTTP Basic auth using demo_credentials username/password on every request;
       login_endpoint/token_json_path are not needed.
     - "api_key": set "header_name" (e.g. "X-API-Key"); the key value is expected in
       demo_credentials.api_key — note in your response if that wasn't present in the input.
   If the input gives no indication of how API auth works, default to "type": "none" rather
   than guessing a plausible-looking login endpoint that doesn't actually exist.
7. "test_type" and interleaved flows — determine "test_type" from what the steps actually
   DO, not from the "Type:" line at face value; the declared Type is a hint, not the source
   of truth. A step counts as a genuine API action only if it performs one (sends a request,
   calls a specific method+path, checks a response status/body) — a step that merely
   MENTIONS the other interface as backstory (e.g. "the payment created via the API
   earlier") does NOT count.
     a) If the input has a SEPARATE "Web Steps:" section in addition to "Steps:" (the
        classic dual-list format), that is a deliberate signal for two INDEPENDENT flows
        testing the same feature via each interface — set "test_type": "both",
        "flow_style": "parallel", and leave "interleaved_steps" as [].
     b) If there is only ONE step list and it contains genuine actions of BOTH flavors in
        sequence, set "test_type": "both", "flow_style": "interleaved", and set
        "interleaved_steps" to one entry per step, in the EXACT order given — order is the
        whole point, do not reorder or group by interface:
          [{{"step": 1, "interface": "api", "description": "Create a payment of 100 SGD via POST /v1/payments"}},
           {{"step": 2, "interface": "web", "description": "Login as Admin and navigate to Payments page"}},
           {{"step": 3, "interface": "web", "description": "Verify the payment appears in the payments list"}},
           {{"step": 4, "interface": "api", "description": "Fetch the payment by ID and verify status is PENDING"}}]
        Also set "interleaved_test_method_name" to a single camelCase method name describing
        the whole flow (e.g. "createPaymentViaApiThenVerifyOnWeb"). When flow_style is
        "interleaved", leave "api_test_methods"/"web_test_methods" as [] — interleaved_steps
        is the sole source of truth for this method's steps, don't duplicate them there.
     c) If there is only one step list and it contains genuine actions of only ONE flavor,
        set "test_type" to that single flavor ("api" or "web") — even if the declared Type
        line said "both" or "hybrid".
     d) If the resolved "test_type"/"flow_style" differs from what the "Type:" line declared,
        set "type_resolution" to a one-sentence explanation naming which steps drove the
        decision (e.g. "Declared: web. Resolved: both (interleaved) — steps 1 and 4 perform
        real API calls (POST /v1/payments, GET /v1/payments/{{id}})."). Otherwise leave
        "type_resolution" as null.
8. Output ONLY valid JSON, no prose, no markdown wrapper.
"""

    log("Calling Claude to parse input...")
    response = call_claude(prompt)
    plan = extract_json(response)

    if not plan:
        log("ERROR: Claude did not return a valid JSON plan")
        (AUDIT_DIR / "01-parse.json").write_text(json.dumps({
            "error": "parse_failed",
            "raw_response": response[:2000]
        }, indent=2))
        sys.exit(1)

    # Override existing_module with filesystem truth (don't trust Claude to infer this)
    plan["existing_module"] = existing
    plan["_input_file"] = str(INPUT_FILE)

    # Same treatment for the module name: rule 1b asks for it, this guarantees it.
    # These three fields decide where every generated file lands and what package it
    # declares, so a model that renamed the module would silently create a second
    # copy alongside the real one — and `existing` above, computed from the Module:
    # name, would then be answering about a directory nothing writes to.
    claude_name = plan.get("feature_name")
    if claude_name != module_name:
        log(f"Module name: overriding Claude's '{claude_name}' with '{module_name}' "
            f"from the input's 'Module:' line")
    plan["feature_name"]  = module_name
    plan["package_main"]  = f"automation.modules.{module_name}"
    plan["package_test"]  = f"automation.{module_name}"

    # Defensive default so every downstream consumer (step 02 Validate API in
    # particular) can read plan["api_auth"]["type"] unconditionally, rather than
    # needing its own fallback for a key an older cached plan or a parse that
    # skipped it entirely might not have.
    if not isinstance(plan.get("api_auth"), dict) or not plan["api_auth"].get("type"):
        plan["api_auth"] = {"type": "none"}

    # Defensive default — an older cached plan, or a parse that didn't hit rule 7's
    # interleaved branch, won't have "flow_style" at all. "parallel" reproduces
    # today's existing behavior (two independent test classes) exactly, so this is
    # a safe default rather than a guess.
    if plan.get("flow_style") not in ("parallel", "interleaved"):
        plan["flow_style"] = "parallel"
    if not isinstance(plan.get("interleaved_steps"), list):
        plan["interleaved_steps"] = []
    if plan["flow_style"] != "interleaved":
        # Belt-and-suspenders: never let a stray interleaved_steps list survive
        # into a plan whose flow_style says parallel — 03_generate.py branches
        # purely on flow_style, but a non-empty list here would be misleading
        # to anyone reading the audit trail directly.
        plan["interleaved_steps"] = []

    # Surface any auto-detected test_type/flow_style change loudly — nothing in
    # the pipeline pauses for approval between parse and generate, so this is the
    # only place a user watching the live run will see WHY more (or less) code is
    # about to be generated than the declared Type implied.
    if plan.get("type_resolution"):
        log(f"NOTE: {plan['type_resolution']}")

    # Extract demo credentials from raw input text (for step 04 infra auto-repair)
    # Claude may not always put them in demo_credentials, so do it in Python as a fallback.
    if not plan.get("demo_credentials"):
        creds = {}
        # Pass 1: top-level key: value lines
        for line in raw_text.splitlines():
            lower = line.lower().strip()
            if lower.startswith("demo username:") or lower.startswith("username:"):
                creds["username"] = line.split(":", 1)[1].strip()
            elif lower.startswith("demo password:") or lower.startswith("password:"):
                creds["password"] = line.split(":", 1)[1].strip()
            elif lower.startswith("demo otp:") or lower.startswith("otp:"):
                creds["otp"] = line.split(":", 1)[1].strip()
        # Pass 2: inline patterns within step text, e.g. "login using username: foo, password: bar"
        if not (creds.get("username") and creds.get("password")):
            u_match = re.search(r'username[:\s]+([^\s,]+)', raw_text, re.IGNORECASE)
            p_match = re.search(r'password[:\s]+([^\s,]+)', raw_text, re.IGNORECASE)
            if u_match and not creds.get("username"):
                creds["username"] = u_match.group(1).strip()
            if p_match and not creds.get("password"):
                creds["password"] = p_match.group(1).strip()
        if creds.get("username") and creds.get("password"):
            plan["demo_credentials"] = creds
            log(f"Extracted demo credentials for: {creds['username']}")

    (AUDIT_DIR / "01-parse.json").write_text(json.dumps(plan, indent=2))

    # Human-readable summary
    summary_lines = [
        "# Parse Results",
        "",
        f"Feature:         {plan.get('feature_name')}",
        f"Class prefix:    {plan.get('feature_class')}",
        f"Test type:       {plan.get('test_type')}",
        f"Flow style:      {plan.get('flow_style')}" + (
            "" if plan.get("test_type") == "both" else "  (n/a — only meaningful for test_type=both)"
        ),
        f"Existing module: {plan.get('existing_module')}",
        f"Package main:    {plan.get('package_main')}",
        f"Package test:    {plan.get('package_test')}",
        f"API auth type:   {plan.get('api_auth', {}).get('type', 'none')}",
        "",
        f"Data fields:     {len(plan.get('data_fields', []))}",
        f"API endpoints:   {len(plan.get('api_endpoints', []))}",
        f"API test methods:{len(plan.get('api_test_methods', []))}",
        f"Web pages:       {len(plan.get('web_pages', []))}",
        f"Web test methods:{len(plan.get('web_test_methods', []))}",
        f"Interleaved steps:{len(plan.get('interleaved_steps', []))}",
    ]
    if plan.get("type_resolution"):
        summary_lines += ["", f"⚠️ Type resolution: {plan['type_resolution']}"]
    if plan.get("flow_style") == "interleaved" and plan.get("interleaved_steps"):
        summary_lines += ["", "## Interleaved Steps"] + [
            f"{s.get('step')}. [{s.get('interface', '?').upper()}] {s.get('description', '')}"
            for s in plan["interleaved_steps"]
        ]
    (AUDIT_DIR / "01-parse.md").write_text("\n".join(summary_lines))

    log(f"Plan: {plan.get('feature_class')} | type={plan.get('test_type')} "
        f"({plan.get('flow_style')}) | existing={existing} | "
        f"{len(plan.get('api_endpoints', []))} endpoints | "
        f"{len(plan.get('web_pages', []))} pages")


if __name__ == "__main__":
    main()
