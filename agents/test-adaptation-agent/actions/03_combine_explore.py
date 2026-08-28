#!/usr/bin/env python3
"""
Step 03 (combine) — one summary file for the two exploration halves.

`runner._audit_watcher` polls for exactly one filename per step, so a step whose
two halves write two files needs a third that always exists. The authoring agent
keys its shared step-02 slot on `02-validate-web.json`, which means an API-only run
never produces that file and the UI's step chip stays on "running" until the whole
process exits. This exists so that does not happen here.

It also merges both halves into a single ordered flow map — API steps first, then
web — so everything downstream reads one artifact and neither guard nor prompt has
to know which interface a step came from.

Reads:   $AUDIT_DIR/03-explore-api.json, $AUDIT_DIR/03-explore-web.json
Writes:  $AUDIT_DIR/03-explore.json + .md
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from shared.log import log as _log
def log(msg): _log("explore", msg)

from shared import flow_map

AUDIT_DIR = Path(os.environ["AUDIT_DIR"])


def _load(name: str) -> dict:
    path = AUDIT_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


# Worst status wins: a combined run is only as trustworthy as its weaker half.
_RANK = {"unsafe": 0, "no_session": 1, "error": 2, "timeout": 3, "empty": 4,
         "partial": 5, "skipped": 6, "ok": 7}


def main():
    api, web = _load("03-explore-api.json"), _load("03-explore-web.json")

    ran = [name for name, part in (("api", api), ("web", web)) if part.get("ran")]
    steps = []
    for step in api.get("steps") or []:
        steps.append({**step, "interface": "api"})
    offset = len(steps)
    for step in (web.get("flow") or {}).get("steps") or []:
        steps.append({**step, "index": offset + step.get("index", 0),
                      "interface": "web"})

    web_flow = web.get("flow") or {}
    combined = {
        "schema_version": flow_map.SCHEMA_VERSION,
        "steps": steps,
        "pages": web_flow.get("pages") or {},
        "unreachable": web_flow.get("unreachable") or [],
        "refusals": web_flow.get("refusals") or [],
        "violations": web_flow.get("violations") or [],
        "outcomes": web_flow.get("outcomes") or [],
        "notes": web_flow.get("notes") or [],
    }

    # Only halves that actually ran get a vote. A web-only change note leaves the
    # API half "skipped", and taking the worst status across both reported a
    # perfectly good seven-step exploration as "skipped" — which then read as
    # "nothing was observed" everywhere downstream.
    statuses = [part.get("status", "skipped") for part in (api, web)
                if part and part.get("ran")]
    status = min(statuses, key=lambda s: _RANK.get(s, 9)) if statuses else "skipped"

    unexplained = ((api.get("unexplained_failures") or [])
                   + (web.get("unexplained_failures") or []))

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "ran": ran,
        "status": status,
        "steps": len(steps),
        "refusals": len(combined["refusals"]),
        "violations": len(combined["violations"]),
        "unreachable": len(combined["unreachable"]),
        "unexplained_failures": unexplained,
        "flow": combined,
    }
    (AUDIT_DIR / "03-explore.json").write_text(json.dumps(result, indent=2))

    md = ["# Explore", "",
          f"Ran: **{', '.join(ran) if ran else 'nothing'}** — status **{status}**", ""]
    if steps:
        md += [flow_map.describe(combined), ""]
    if unexplained:
        md += ["## ⚠️ Unexplained failures", "",
               "These were not accounted for by any line of the change note. A human "
               "asserted one change; that says nothing about a second, unrelated "
               "defect. The run escalates rather than adapting to them.", ""]
        md += [f"- step {u.get('index')}: {u.get('target') or u.get('endpoint')} "
               f"({u.get('category')}) {u.get('detail','')}" for u in unexplained]
    (AUDIT_DIR / "03-explore.md").write_text("\n".join(md) + "\n")

    log(f"Combined: {len(steps)} step(s) from [{', '.join(ran) or 'none'}], "
        f"status {status}"
        + (f", {len(unexplained)} unexplained failure(s)" if unexplained else ""))


if __name__ == "__main__":
    main()
