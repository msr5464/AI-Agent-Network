"""Write per-module login credentials into the Java automation framework's
environment+country properties file (parameters/{environment}-{country}.properties),
matching Config.java's own file-loading convention (confirmed by reading
Config.java's loadPropertiesFile calls: filename = environment + "-" + country +
".properties", loaded from the "parameters" directory).

Used by test-authoring-agent's generate (03) and run-and-fix (04) steps:
  - 03_generate.py writes it once, right after generating a new web module,
    using the same property key ({feature}.username) the codegen prompt already
    instructs Claude to reference via config.getRunTimeProperty().
  - 04_run_and_fix.py re-checks/writes it defensively before diagnosing a
    CODE_ERROR failure — a safety net for cases 03's write doesn't cover (an
    existing module, a session run before this existed, or a properties file
    edited/reverted since generation).
"""
import os
from pathlib import Path


def write_credential_property(automation_framework_dir: Path, feature_lower: str,
                              demo_creds: dict, log=lambda msg: None) -> str:
    """Ensure {feature}.username / {feature}.password exist in the environment+
    country properties file. Idempotent — leaves existing values alone rather
    than overwriting them (a human may have deliberately changed one).

    Returns "written" / "already present" / "no credentials to write".
    """
    username = demo_creds.get("username")
    password = demo_creds.get("password")
    if not username or not password:
        return "no credentials to write"

    environment = os.environ.get("AUTOCREATE_ENVIRONMENT", "staging").lower()
    country     = os.environ.get("AUTOCREATE_COUNTRY", "SG").lower()
    props_path  = automation_framework_dir / "parameters" / f"{environment}-{country}.properties"

    key_user = f"{feature_lower}.username"
    key_pass = f"{feature_lower}.password"

    existing_text = props_path.read_text() if props_path.exists() else ""
    existing_keys = {
        s.split("=", 1)[0].strip()
        for line in existing_text.splitlines()
        if (s := line.strip()) and not s.startswith("#") and "=" in s
    }

    if key_user in existing_keys and key_pass in existing_keys:
        log(f"  {props_path.name} already has {key_user}/{key_pass} — leaving as-is")
        return "already present"

    props_path.parent.mkdir(parents=True, exist_ok=True)
    new_lines = [f"{k}={v}" for k, v in ((key_user, username), (key_pass, password))
                if k not in existing_keys]
    separator = "" if (not existing_text or existing_text.endswith("\n")) else "\n"
    addition = (f"{separator}# {feature_lower.capitalize()} (auto-added by test-authoring-agent)\n"
               + "\n".join(new_lines) + "\n")
    props_path.write_text(existing_text + addition)
    log(f"  Wrote credentials to {props_path.relative_to(automation_framework_dir)}: "
        f"{key_user}, {key_pass}")
    return "written"
