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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
AUDIT_DIR  = Path(os.environ["AUDIT_DIR"])
AGENT_DIR  = Path(os.environ.get("AGENT_DIR", Path(__file__).resolve().parents[1]))
REPO_ROOT  = Path(os.environ.get("REPO_ROOT",  Path(__file__).resolve().parents[3]))
INPUT_FILE = Path(os.environ["INPUT_FILE"])

WORKSPACE_DIR    = Path(os.environ.get("WORKSPACE_DIR", REPO_ROOT.parent))
THANOS_PW_DIR    = WORKSPACE_DIR / os.environ.get("GITHUB_REPO_AUTOMATION", "Thanos-pw")

CLAUDE_CLI = os.environ.get("CLAUDE_CLI_PATH", "claude")
MODEL      = os.environ.get("AUTOCREATE_MODEL", "claude-opus-4-6")

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [01-parse] {msg}", flush=True)


def call_claude(prompt: str) -> str:
    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt, "--model", MODEL],
            capture_output=True, text=True, timeout=600, cwd=str(REPO_ROOT)
        )
    except subprocess.TimeoutExpired:
        log("ERROR: Claude CLI timed out (600s)")
        return ""
    if result.returncode != 0:
        log(f"Claude error: {result.stderr[:400]}")
        return ""
    return result.stdout


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


def module_exists(feature_name: str) -> bool:
    """Check if the feature module already exists in Thanos-pw."""
    module_dir = THANOS_PW_DIR / "src/main/java/automation/modules" / feature_name.lower()
    return module_dir.exists()


def read_existing_module_files(feature_name: str) -> dict:
    """Read existing module files to give Claude context when appending."""
    module_dir = THANOS_PW_DIR / "src/main/java/automation/modules" / feature_name.lower()
    if not module_dir.exists():
        return {}
    files = {}
    for f in module_dir.rglob("*.java"):
        rel = f.relative_to(THANOS_PW_DIR)
        try:
            files[str(rel)] = f.read_text()[:3000]  # truncate large files
        except Exception:
            pass
    return files


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log(f"Reading input: {INPUT_FILE}")
    raw_text = INPUT_FILE.read_text()

    # Load agent CLAUDE.md — for parsing we only need the package structure and naming
    # conventions, not the full Java code examples (those are only needed in 03_generate.py).
    # Trim at the first Java code block to keep the prompt compact.
    claude_md_path = AGENT_DIR / "CLAUDE.md"
    claude_md_full = claude_md_path.read_text() if claude_md_path.exists() else ""
    # Keep everything up to (but not including) the first ```java block
    java_block_pos = claude_md_full.find("```java")
    claude_md = claude_md_full[:java_block_pos].strip() if java_block_pos > 0 else claude_md_full

    # Extract feature name hint from first line to check module existence early
    feature_hint = ""
    for line in raw_text.splitlines():
        if line.lower().startswith("feature:"):
            feature_hint = line.split(":", 1)[1].strip().lower()
            break
    if not feature_hint:
        feature_hint = INPUT_FILE.stem.lower()

    existing = module_exists(feature_hint)
    existing_files = read_existing_module_files(feature_hint) if existing else {}
    log(f"Module '{feature_hint}' exists: {existing}")

    existing_context = ""
    if existing_files:
        existing_context = "\n\n<existing_module_files>\n"
        for path, content in existing_files.items():
            existing_context += f"\n--- {path} ---\n{content}\n"
        existing_context += "</existing_module_files>"

    prompt = f"""You are a QA automation planning agent for the Thanos-pw Java framework.

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
        "loginAndSetAuth",
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
  ]
}}

Rules:
1. Set "existing_module" to {str(existing).lower()} (already detected from filesystem).
2. "feature_enum" must be one of: CARD, BUDGET, CLAIM, DBS_SG, DBS_HK, CC_SG, CALASTONE_SG.
   Pick the closest match; if unsure use CARD.
3. "response_only": true for fields set by the server (id, status, createdAt, updatedAt).
4. Infer "web_steps_for_validation" from the plain English web steps for use in the
   Playwright validation script — list them as simple imperative sentences.
5. If "existing_module" is true and the input only adds new test scenarios (not new endpoints),
   set "api_endpoints" and "data_fields" to [] to avoid regenerating existing files.
6. Output ONLY valid JSON, no prose, no markdown wrapper.
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

    # Extract demo credentials from raw input text (for step 04 infra auto-repair)
    # Claude may not always put them in demo_credentials, so do it in Python as a fallback.
    if not plan.get("demo_credentials"):
        creds = {}
        for line in raw_text.splitlines():
            lower = line.lower().strip()
            if lower.startswith("demo username:") or lower.startswith("username:"):
                creds["username"] = line.split(":", 1)[1].strip()
            elif lower.startswith("demo password:") or lower.startswith("password:"):
                creds["password"] = line.split(":", 1)[1].strip()
            elif lower.startswith("demo otp:") or lower.startswith("otp:"):
                creds["otp"] = line.split(":", 1)[1].strip()
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
        f"Existing module: {plan.get('existing_module')}",
        f"Package main:    {plan.get('package_main')}",
        f"Package test:    {plan.get('package_test')}",
        "",
        f"Data fields:     {len(plan.get('data_fields', []))}",
        f"API endpoints:   {len(plan.get('api_endpoints', []))}",
        f"API test methods:{len(plan.get('api_test_methods', []))}",
        f"Web pages:       {len(plan.get('web_pages', []))}",
        f"Web test methods:{len(plan.get('web_test_methods', []))}",
    ]
    (AUDIT_DIR / "01-parse.md").write_text("\n".join(summary_lines))

    log(f"Plan: {plan.get('feature_class')} | type={plan.get('test_type')} | "
        f"existing={existing} | {len(plan.get('api_endpoints', []))} endpoints | "
        f"{len(plan.get('web_pages', []))} pages")


if __name__ == "__main__":
    main()
