#!/usr/bin/env python3
"""
QA Agent Network — Audit Dashboard
Flask server for browsing agent audit sessions.

Usage:
  python3 scripts/audit_viewer.py [--port 8888] [--agents-dir agents]
  make dashboard
  make dashboard PORT=9000
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, render_template_string

app = Flask(__name__)

AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", "agents"))

# ── Data helpers ──────────────────────────────────────────────────────────────

def load_json(path):
    path = Path(path)
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_text(path):
    path = Path(path)
    return path.read_text() if path.exists() else ""


def fmt_duration(t1_str, t2_str):
    try:
        t1 = datetime.fromisoformat(t1_str.replace("Z", "+00:00"))
        t2 = datetime.fromisoformat(t2_str.replace("Z", "+00:00"))
        secs = max(0, int((t2 - t1).total_seconds()))
        m, s = divmod(secs, 60)
        return f"{m}m {s:02d}s"
    except Exception:
        return ""


def parse_session_ts(name):
    """Parse 20260327-033247 from folder name into display string."""
    try:
        raw = name[:15]
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[9:11]}:{raw[11:13]}"
    except Exception:
        return name[:15]


# ── Agent discovery ───────────────────────────────────────────────────────────

AGENT_ORDER = ["test-triaging-agent", "test-healing-agent",
               "test-adaptation-agent", "test-authoring-agent"]


def get_agents():
    """Discover all agents with audit directories."""
    agents = []
    order_map = {name: i for i, name in enumerate(AGENT_ORDER)}
    for d in sorted(AGENTS_DIR.iterdir(), key=lambda d: order_map.get(d.name, 999)):
        if d.is_dir() and (d / "audit").is_dir() and not d.name.startswith("."):
            audit_dir = d / "audit"
            session_dirs = [s for s in audit_dir.iterdir() if s.is_dir() and not s.name.startswith(".")]
            agents.append({
                "name": d.name,
                "display": d.name.replace("-", " ").title(),
                "session_count": len(session_dirs),
            })
    return agents


def get_sessions(agent_name):
    """Get all sessions for an agent, dispatched by agent type."""
    audit_dir = AGENTS_DIR / agent_name / "audit"
    if not audit_dir.exists():
        return []

    parsers = {
        "test-triaging-agent": _get_triaging_session,
        "test-healing-agent": _get_healing_session,
        "test-authoring-agent": _get_authoring_session,
        "test-adaptation-agent": _get_adaptation_session,
    }
    parser = parsers.get(agent_name, _get_generic_session)

    sessions = []
    for sd in sorted(
        [s for s in audit_dir.iterdir() if s.is_dir() and not s.name.startswith(".")],
        reverse=True,
    ):
        sessions.append(parser(sd))
    return sessions


# ── Session parsers ───────────────────────────────────────────────────────────

def _get_triaging_session(sd):
    scout = load_json(sd / "01-scout.json")
    classify = load_json(sd / "03-classify.json")
    ship = load_json(sd / "05-ship.json")
    verdict = load_text(sd / ".verdict").strip() or "PENDING"

    selected_tag = scout.get("selected_build_tag", "") or load_text(sd / ".selected-buildtag").strip()
    total = classify.get("total_failures", 0)
    automation = classify.get("summary", {}).get("AUTOMATION_ISSUE", 0)

    if ship:
        if verdict == "APPROVED":
            status, status_cls = "Shipped", "shipped"
        else:
            status, status_cls = "Escalated", "escalated"
    elif verdict == "APPROVED":
        status, status_cls = "Approved", "shipped"
    elif verdict == "NEEDS-HUMAN":
        status, status_cls = "Needs Human", "escalated"
    elif classify:
        status, status_cls = "Classified", "progress"
    elif scout:
        status, status_cls = "Scouted", "progress"
    else:
        status, status_cls = "Pending", "progress"

    all_ts = [
        v.get("timestamp", "")
        for v in [scout, classify, ship]
        if v.get("timestamp")
    ]
    dur = fmt_duration(min(all_ts), max(all_ts)) if len(all_ts) >= 2 else ""

    return {
        "id": sd.name,
        "title": selected_tag or sd.name,
        "subtitle": f"{total} failures · {automation} automation issues" if total else "",
        "ts": parse_session_ts(sd.name),
        "verdict": verdict,
        "status": status,
        "status_cls": status_cls,
        "duration": dur,
    }


def _get_healing_session(sd):
    fix = load_json(sd / "01-fix.json")
    ship = load_json(sd / "02-ship.json")
    fix_passed = load_text(sd / ".fix-passed").strip()

    build_tag = fix.get("build_tag", "") or sd.name.split("-", 2)[-1] if sd.name.count("-") >= 2 else sd.name
    # 01-fix.json writes succeeded/unverified/failed; the older *_count names it
    # is read under here never existed, so this panel always showed "0 fixed".
    fixed = fix.get("succeeded", fix.get("fixed_count", 0))
    failed = fix.get("failed", fix.get("failed_count", 0))
    unverified = fix.get("unverified", 0)
    pr_url = ship.get("pr_url", "")

    if pr_url:
        status, status_cls = "PR Created", "shipped"
    elif fix_passed == "true" and unverified and not fixed:
        status, status_cls = "Applied (unverified)", "escalated"
    elif fix_passed == "true":
        status, status_cls = "Fixed", "shipped"
    elif fix_passed == "false":
        status, status_cls = "Fix Failed", "escalated"
    elif fix_passed == "skipped":
        status, status_cls = "Skipped", "progress"
    elif fix:
        status, status_cls = "In Progress", "progress"
    else:
        status, status_cls = "Pending", "progress"

    all_ts = [v.get("timestamp", "") for v in [fix, ship] if v.get("timestamp")]
    dur = fmt_duration(min(all_ts), max(all_ts)) if len(all_ts) >= 2 else ""

    subtitle = ""
    if fixed or failed or unverified:
        parts = [f"{fixed} fixed"]
        if unverified:
            parts.append(f"{unverified} unverified")
        parts.append(f"{failed} failed")
        subtitle = " · ".join(parts)
    if pr_url:
        subtitle += f" · PR created" if subtitle else "PR created"

    return {
        "id": sd.name,
        "title": build_tag,
        "subtitle": subtitle,
        "ts": parse_session_ts(sd.name),
        "verdict": fix_passed.upper() if fix_passed else "PENDING",
        "status": status,
        "status_cls": status_cls,
        "duration": dur,
        "pr_url": pr_url,
    }


def _get_authoring_session(sd):
    parse_data = load_json(sd / "01-parse.json")
    ship = load_json(sd / "05-ship.json")
    verdict = load_text(sd / ".verdict").strip() or "PENDING"
    fix_passed = load_text(sd / ".fix-passed").strip()

    feature = parse_data.get("feature_name", "") or sd.name
    pr_url = ship.get("pr_url", "")

    if pr_url and verdict == "APPROVED":
        status, status_cls = "PR Created", "shipped"
    elif pr_url:
        status, status_cls = "PR (Needs Review)", "escalated"
    elif verdict == "APPROVED":
        status, status_cls = "Approved", "shipped"
    elif verdict == "NEEDS-REVIEW":
        status, status_cls = "Needs Review", "escalated"
    elif fix_passed:
        status, status_cls = "Tests Run", "progress"
    elif parse_data:
        status, status_cls = "Parsed", "progress"
    else:
        status, status_cls = "Pending", "progress"

    all_ts = [v.get("timestamp", "") for v in [parse_data, ship] if v.get("timestamp")]
    dur = fmt_duration(min(all_ts), max(all_ts)) if len(all_ts) >= 2 else ""

    return {
        "id": sd.name,
        "title": feature,
        "subtitle": f"test_type={parse_data.get('test_type', '')}" if parse_data else "",
        "ts": parse_session_ts(sd.name),
        "verdict": verdict,
        "status": status,
        "status_cls": status_cls,
        "duration": dur,
        "pr_url": pr_url,
    }



def _get_adaptation_session(sd):
    """One adaptation session row.

    Reads the same fields as qa_agents_server's summariser on purpose: two
    dashboards looking at the same run must not disagree about what happened.
    """
    parse = load_json(sd / "01-parse-change.json")
    adapt = load_json(sd / "04-adapt.json")
    ship = load_json(sd / "05-ship.json")
    gate = load_text(sd / ".fix-passed").strip()
    skip = load_text(sd / ".skip-reason").strip()
    verdict = load_text(sd / ".verdict").strip()

    items = adapt.get("items") or []
    applied = sum(1 for i in items if i.get("status") in ("applied", "partial"))
    proposed = sum(1 for i in items if i.get("status") == "proposed")
    escalated = sum(1 for i in items if i.get("status") in ("escalated", "declined"))
    pr_url = ship.get("pr_url", "") or ""

    # An escalation is the design working, not a failure. Painting a correct
    # refusal red trains people to ignore the runs most worth reading.
    if pr_url:
        status, status_cls = "PR — needs review", "escalated"
    elif skip in ("escalate", "unsafe", "no-session", "unreachable"):
        status, status_cls = "Escalated to a human", "progress"
    elif skip == "explore-only":
        status, status_cls = "Explored", "shipped"
    elif proposed:
        status, status_cls = f"Proposed ({proposed})", "progress"
    elif gate == "false":
        status, status_cls = "Adapt failed", "escalated"
    elif adapt or parse:
        status, status_cls = "In Progress", "progress"
    else:
        status, status_cls = "Unknown", "progress"

    bits = [f"{len(parse.get('items') or [])} change item(s)"]
    if applied:
        bits.append(f"{applied} applied")
    if proposed:
        bits.append(f"{proposed} proposed")
    if escalated:
        bits.append(f"{escalated} escalated")

    return {
        "id": sd.name,
        "title": parse.get("module", "") or sd.name,
        "subtitle": " · ".join(bits),
        "ts": parse_session_ts(sd.name),
        "verdict": verdict or (gate.upper() if gate else "PENDING"),
        "status": status,
        "status_cls": status_cls,
        "duration": "",
        "pr_url": pr_url,
    }


def _get_generic_session(sd):
    verdict = load_text(sd / ".verdict").strip() or "PENDING"
    return {
        "id": sd.name,
        "title": sd.name,
        "subtitle": "",
        "ts": parse_session_ts(sd.name),
        "verdict": verdict,
        "status": verdict.title() if verdict != "PENDING" else "Pending",
        "status_cls": "shipped" if verdict == "APPROVED" else "progress",
        "duration": "",
    }


# ── HTML templates ────────────────────────────────────────────────────────────

THEME_CSS = """
:root {
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #848d97; --accent: #58a6ff;
    --green: #3fb950; --yellow: #d29922; --red: #f85149;
    --progress: #58a6ff;
}
[data-theme="light"] {
    --bg: #f6f8fa; --card: #ffffff; --border: #d0d7de;
    --text: #24292f; --muted: #57606a; --accent: #0969da;
    --green: #1a7f37; --yellow: #9a6700; --red: #cf222e;
    --progress: #0969da;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; font-size: 14px; line-height: 1.5; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 1100px; margin: 0 auto; padding: 24px 16px; }
.header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
.header h1 { font-size: 20px; font-weight: 600; }
.theme-btn { background: var(--card); border: 1px solid var(--border); color: var(--text); padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.agent-section { margin-bottom: 32px; }
.agent-title { font-size: 16px; font-weight: 600; margin-bottom: 12px; color: var(--text); }
.agent-subtitle { font-size: 13px; color: var(--muted); font-weight: 400; }
.session-list { display: flex; flex-direction: column; gap: 8px; }
.session-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; display: flex; align-items: center; gap: 12px; transition: border-color 0.15s; }
.session-card:hover { border-color: var(--accent); }
.status-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.shipped { background: var(--green); }
.escalated { background: var(--red); }
.progress { background: var(--progress); }
.session-info { flex: 1; min-width: 0; }
.session-title { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.session-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
.session-meta { display: flex; gap: 12px; align-items: center; flex-shrink: 0; font-size: 12px; color: var(--muted); }
.badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }
.badge-green { background: #1c2e1c; color: var(--green); border: 1px solid #2e4c2e; }
.badge-red { background: #2e1c1c; color: var(--red); border: 1px solid #4c2e2e; }
.badge-blue { background: #1c2440; color: var(--accent); border: 1px solid #2e3d60; }
.badge-gray { background: var(--card); color: var(--muted); border: 1px solid var(--border); }
[data-theme="light"] .badge-green { background: #dafbe1; border-color: #74c17a; }
[data-theme="light"] .badge-red { background: #ffebe9; border-color: #f5a8a8; }
[data-theme="light"] .badge-blue { background: #ddf4ff; border-color: #96ceff; }
.empty { color: var(--muted); font-style: italic; padding: 12px 0; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
.detail-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.detail-card h3 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
.kv-row { display: flex; gap: 8px; margin-bottom: 6px; font-size: 13px; }
.kv-key { color: var(--muted); min-width: 120px; flex-shrink: 0; }
.kv-val { color: var(--text); word-break: break-all; }
pre { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; overflow-x: auto; font-size: 12px; white-space: pre-wrap; word-break: break-word; color: var(--text); margin-top: 8px; }
.back-link { margin-bottom: 20px; display: block; font-size: 13px; }
"""

THEME_TOGGLE = """
<script>
const saved = localStorage.getItem('theme') || 'dark';
document.documentElement.setAttribute('data-theme', saved);
function toggleTheme() {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
}
</script>
"""

INDEX_HTML = """
<!DOCTYPE html>
<html data-theme="dark">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>QA Agent Network — Audit Dashboard</title>
<style>{{ css }}</style>
{{ theme_script }}
</head>
<body>
<div class="container">
  <div class="header">
    <h1>QA Agent Network — Audit Dashboard</h1>
    <button class="theme-btn" onclick="toggleTheme()">Toggle Theme</button>
  </div>

  {% for agent in agents %}
  <div class="agent-section">
    <div class="agent-title">
      {{ agent.display }}
      <span class="agent-subtitle">({{ agent.session_count }} sessions)</span>
    </div>
    <div class="session-list">
      {% set sessions = get_sessions(agent.name) %}
      {% if not sessions %}
        <div class="empty">No sessions yet.</div>
      {% else %}
        {% for s in sessions %}
        <a href="/agent/{{ agent.name }}/session/{{ s.id }}" style="text-decoration:none; color:inherit;">
          <div class="session-card">
            <div class="status-dot {{ s.status_cls }}"></div>
            <div class="session-info">
              <div class="session-title">{{ s.title }}</div>
              {% if s.subtitle %}
              <div class="session-sub">{{ s.subtitle }}</div>
              {% endif %}
            </div>
            <div class="session-meta">
              {% if s.duration %}<span>{{ s.duration }}</span>{% endif %}
              <span>{{ s.ts }}</span>
              {% if s.status_cls == 'shipped' %}
                <span class="badge badge-green">{{ s.status }}</span>
              {% elif s.status_cls == 'escalated' %}
                <span class="badge badge-red">{{ s.status }}</span>
              {% else %}
                <span class="badge badge-blue">{{ s.status }}</span>
              {% endif %}
            </div>
          </div>
        </a>
        {% endfor %}
      {% endif %}
    </div>
  </div>
  {% endfor %}
</div>
</body>
</html>
"""

SESSION_HTML = """
<!DOCTYPE html>
<html data-theme="dark">
<head>
<meta charset="utf-8">
<title>{{ session.id }} — QA Agent Network</title>
<style>{{ css }}</style>
{{ theme_script }}
</head>
<body>
<div class="container">
  <div class="header">
    <h1>Session: {{ session.id }}</h1>
    <button class="theme-btn" onclick="toggleTheme()">Toggle Theme</button>
  </div>

  <a class="back-link" href="/agent/{{ agent_name }}">← Back to {{ agent_name }}</a>

  <div class="detail-grid">
    <div class="detail-card">
      <h3>Overview</h3>
      {% for key, val in session.items() %}
        {% if val and key not in ['id', 'agent'] %}
        <div class="kv-row">
          <span class="kv-key">{{ key }}</span>
          <span class="kv-val">
            {% if val is mapping %}
              <pre>{{ val | tojson(indent=2) }}</pre>
            {% elif val is iterable and val is not string %}
              {{ val | join(', ') }}
            {% else %}
              {{ val }}
            {% endif %}
          </span>
        </div>
        {% endif %}
      {% endfor %}
    </div>

    <div class="detail-card">
      <h3>Audit Files</h3>
      {% for f in audit_files %}
      <div class="kv-row">
        <span class="kv-key">{{ f.name }}</span>
        <span class="kv-val">
          {% if f.is_json %}
            <pre>{{ f.content }}</pre>
          {% else %}
            <pre>{{ f.content[:3000] }}</pre>
          {% endif %}
        </span>
      </div>
      {% endfor %}
    </div>
  </div>
</div>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    agents = get_agents()
    return render_template_string(
        INDEX_HTML,
        agents=agents,
        get_sessions=get_sessions,
        css=THEME_CSS,
        theme_script=THEME_TOGGLE,
    )


@app.route("/agent/<agent_name>")
def agent_sessions(agent_name):
    agents = get_agents()
    agent = next((a for a in agents if a["name"] == agent_name), None)
    if not agent:
        abort(404)
    sessions = get_sessions(agent_name)
    return render_template_string(
        INDEX_HTML,
        agents=[agent],
        get_sessions=get_sessions,
        css=THEME_CSS,
        theme_script=THEME_TOGGLE,
    )


@app.route("/agent/<agent_name>/session/<session_id>")
def session_detail(agent_name, session_id):
    sd = AGENTS_DIR / agent_name / "audit" / session_id
    if not sd.exists():
        abort(404)

    parsers = {
        "test-triaging-agent": _get_triaging_session,
        "test-healing-agent": _get_healing_session,
        "test-authoring-agent": _get_authoring_session,
        "test-adaptation-agent": _get_adaptation_session,
    }
    session = parsers.get(agent_name, _get_generic_session)(sd)

    # Load all audit files
    audit_files = []
    for f in sorted(sd.iterdir()):
        if f.name.startswith(".") or f.is_dir():
            continue
        content = f.read_text(errors="replace")
        is_json = f.suffix == ".json"
        if is_json:
            try:
                content = json.dumps(json.loads(content), indent=2)
            except Exception:
                pass
        audit_files.append({"name": f.name, "content": content, "is_json": is_json})

    return render_template_string(
        SESSION_HTML,
        agent_name=agent_name,
        session=session,
        audit_files=audit_files,
        css=THEME_CSS,
        theme_script=THEME_TOGGLE,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global AGENTS_DIR
    parser = argparse.ArgumentParser(description="QA Agent Network Audit Dashboard")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--agents-dir", default="agents")
    args = parser.parse_args()

    AGENTS_DIR = Path(args.agents_dir)
    if not AGENTS_DIR.exists():
        print(f"ERROR: agents dir not found: {AGENTS_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"Audit Dashboard: http://localhost:{args.port}")
    print(f"Agents dir: {AGENTS_DIR.resolve()}")
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
