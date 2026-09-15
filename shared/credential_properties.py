"""Write per-module login credentials into the Java automation framework's
environment+country properties file (parameters/{environment}-{country}.properties).

The file location, key naming and idempotence rules live in
shared/properties_file.py, which url_properties.py shares — this module only
decides WHICH keys a module's credentials use.

Used by test-authoring-agent's generate (03) and run-and-fix (04) steps:
  - 03_generate.py writes it once, right after generating a new web module,
    using the same property key ({feature}.username) the codegen prompt already
    instructs Claude to reference via config.getRunTimeProperty().
  - 04_run_and_fix.py re-checks/writes it defensively before diagnosing a
    CODE_ERROR failure — a safety net for cases 03's write doesn't cover (an
    existing module, a session run before this existed, or a properties file
    edited/reverted since generation).

These values are real credentials, so 05_ship.py never commits them — see
url_properties.py for why URLs are the opposite case.
"""
from pathlib import Path

from shared import properties_file


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

    return properties_file.upsert(
        properties_file.properties_path(automation_framework_dir),
        {f"{feature_lower}.username": username, f"{feature_lower}.password": password},
        f"{feature_lower.capitalize()} (auto-added by test-authoring-agent)",
        log,
    )
