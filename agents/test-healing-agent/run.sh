#!/bin/bash
set -Eeuo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# agents/test-healing-agent/run.sh
# Picks a handoff from the queue (or a specific BUILD_TAG), attempts locator
# fixes, verifies with test runs, and creates a GitHub PR.
#
# Usage:
#   ./scripts/run-healing-agent.sh                    # queue mode: picks oldest
#   ./scripts/run-healing-agent.sh ProdSanity-541     # direct: specific handoff
#   AUTO_PUSH=false ./scripts/run-healing-agent.sh    # dry-run: no PR
#
# Retry loop: if tests fail after fix, re-runs 01_fix.py up to HEALING_RETRY_COUNT.
# ─────────────────────────────────────────────────────────────────────────────

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
BUILD_TAG="${1:-${BUILD_TAG:-}}"

# Load .env files (root → agent override)
source "$REPO_ROOT/shared/load_env.sh"

export BUILD_TAG
export AGENT_DIR
export REPO_ROOT

# ── Session helpers (log, run_step, fmt_duration, elapsed_since) ──────────────
source "$REPO_ROOT/shared/session.sh"

# ── Queue scout / direct mode ─────────────────────────────────────────────────
QUEUE_DIR="$AGENT_DIR/queue"
PROCESSED_DIR="$QUEUE_DIR/processed"
mkdir -p "$PROCESSED_DIR"

# Honour HANDOFF_FILE env var if passed directly (e.g. from run-healing-agent.sh with a file path)
HANDOFF_FILE="${HANDOFF_FILE:-}"
TEST_NAME="${TEST_NAME:-${TEST:-}}"
export TEST_NAME

if [[ -n "$TEST_NAME" ]]; then
  # Standalone mode — no triaging, no queue. Step 00 runs the named test locally,
  # reproduces the failure and writes the handoff itself.
  SAFE_TAG="local-$(printf '%s' "$TEST_NAME" | tr '#' '-' | tr -c 'a-zA-Z0-9_-' '-' | sed 's/-\{2,\}/-/g; s/-$//')"
  MODE="local"
  BUILD_TAG=""

elif [[ -n "$HANDOFF_FILE" ]]; then
  # File path mode — caller supplied an explicit JSON file
  if [[ ! -f "$HANDOFF_FILE" ]]; then
    log "ERROR: Handoff file not found: $HANDOFF_FILE"
    exit 1
  fi
  BUILD_TAG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['build_tag'])" "$HANDOFF_FILE")
  SAFE_TAG="${BUILD_TAG//\//-}"
  MODE="file"

elif [[ -n "$BUILD_TAG" ]]; then
  # Build tag mode — look up file in queue/
  SAFE_TAG="${BUILD_TAG//\//-}"
  HANDOFF_FILE="$QUEUE_DIR/${SAFE_TAG}.json"
  if [[ ! -f "$HANDOFF_FILE" ]]; then
    log "ERROR: No handoff file for BUILD_TAG=$BUILD_TAG"
    log "Expected: $HANDOFF_FILE"
    log "Run test-triaging-agent first, or check the queue directory."
    exit 1
  fi
  MODE="direct"

else
  # Queue mode — claim the oldest .json file in queue/.
  #
  # Claiming, not just picking: `ls | tail -1` gave two concurrent healing runs
  # the same handoff, so both fixed the same test, raced on the same files, and
  # both then tried to move one handoff to processed. `mv` within a filesystem
  # is atomic and fails for the loser, which makes it the claim.
  CLAIM_DIR="$QUEUE_DIR/.claimed/$$"
  mkdir -p "$CLAIM_DIR"
  HANDOFF_FILE=""
  while IFS= read -r candidate; do
    [[ -z "$candidate" ]] && continue
    if mv "$candidate" "$CLAIM_DIR/" 2>/dev/null; then
      HANDOFF_FILE="$CLAIM_DIR/$(basename "$candidate")"
      break
    fi
    log "Handoff $(basename "$candidate") was claimed by another run — trying the next"
  done < <(ls -tr "$QUEUE_DIR"/*.json 2>/dev/null || true)

  if [[ -z "$HANDOFF_FILE" ]]; then
    rmdir "$CLAIM_DIR" 2>/dev/null || true
    log "Queue is empty — nothing to fix."
    log "Run test-triaging-agent first to populate the queue."
    exit 0
  fi
  # Whatever happens next, this run must not strand its claim in .claimed/.
  # This replaces shared/session.sh's EXIT trap, so it has to call
  # finalize_metrics itself — with the original exit code restored first —
  # or queue-mode runs would never write metrics.json or their analytics row.
  # shellcheck disable=SC2064
  trap "_rc=\$?; [[ -f \"$HANDOFF_FILE\" ]] && mv \"$HANDOFF_FILE\" \"$QUEUE_DIR/\" 2>/dev/null; rmdir \"$CLAIM_DIR\" 2>/dev/null; (exit \$_rc); finalize_metrics" EXIT
  BUILD_TAG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['build_tag'])" "$HANDOFF_FILE")
  SAFE_TAG="${BUILD_TAG//\//-}"
  MODE="queue"
fi

export BUILD_TAG HANDOFF_FILE

# ── Session init ───────────────────────────────────────────────────────────────
# qa_agents_server pre-computes SESSION_ID/AUDIT_DIR so it knows which directory
# to watch for step files before the process starts. Honour them when set.
SESSION_ID="${SESSION_ID:-$(date +%Y%m%d-%H%M%S)-fix-${SAFE_TAG}}"
AUDIT_DIR="${AUDIT_DIR:-$AGENT_DIR/audit/$SESSION_ID}"
mkdir -p "$AUDIT_DIR"
export SESSION_ID AUDIT_DIR

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "test-healing-agent | mode=$MODE"
log "build_tag=$BUILD_TAG"
log "handoff=$HANDOFF_FILE"
log "session=$SESSION_ID"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Write session init
cat > "$AUDIT_DIR/00-session-init.md" << EOF
# Session Init

Mode: $MODE
Session ID: $SESSION_ID
Build Tag: $BUILD_TAG
Handoff File: $HANDOFF_FILE
Started: $(date +%Y-%m-%dT%H:%M:%S)

## Env Snapshot (keys only)
$(env | grep -E '^(GITHUB_|SLACK_|MAX_|AUTO_|AUTOFIX_|CLAUDE_|WORKSPACE_|REPO_CONTEXT_|TEST_RUNNER_|PLAYWRIGHT_|LOCATE_)' | sed 's/=.*/=<set>/' | sort)
EOF

declare -a STEP_NAMES=()
declare -a STEP_DURATIONS=()

# ── Step 00 — Reproduce (standalone mode only) ────────────────────────────────
if [[ "$MODE" == "local" ]]; then
  run_step "[01/04] Reproduce" "python3 '$AGENT_DIR/actions/00_reproduce.py'" reproduce

  if [[ ! -f "$AUDIT_DIR/00-handoff.json" ]]; then
    # A passing test or a non-locator failure. Both are legitimate outcomes, and
    # step 00 has already written the explanation — there is nothing to fix, so
    # running the fix and ship steps would only produce noise.
    log "Nothing to fix — see $AUDIT_DIR/00-reproduce.md"
    flush_step_done
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    sed -n '3,12p' "$AUDIT_DIR/00-reproduce.md" 2>/dev/null || true
    log "Audit: $AUDIT_DIR"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 0
  fi

  HANDOFF_FILE="$AUDIT_DIR/00-handoff.json"
  BUILD_TAG=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['build_tag'])" "$HANDOFF_FILE")
  export HANDOFF_FILE BUILD_TAG
  log "Reproduced — handoff: $HANDOFF_FILE (build_tag=$BUILD_TAG)"
fi

# ── Crash handler ─────────────────────────────────────────────────────────────
# Without this, an unhandled exception in a step aborts the script under `set -e`
# before Ship runs, so nobody is told anything — the only trace is the terminal.
on_error() {
  local exit_code=$?
  local line_no="${1:-?}"
  log "ERROR: step failed (exit $exit_code, near run.sh:$line_no) — see the traceback above"
  echo "Crashed at run.sh:$line_no with exit $exit_code" > "$AUDIT_DIR/.crashed"

  if [[ -n "${SLACK_BOT_TOKEN:-}" && -n "${SLACK_ALERT_CHANNEL:-${SLACK_NOTIFY_CHANNEL:-}}" ]]; then
    BUILD_TAG="$BUILD_TAG" SESSION_ID="$SESSION_ID" EXIT_CODE="$exit_code" LINE_NO="$line_no" \
    python3 - <<'PYALERT' || true
import os, sys
sys.path.insert(0, os.environ["REPO_ROOT"])
from shared.slack import send_slack
channel = os.environ.get("SLACK_ALERT_CHANNEL") or os.environ.get("SLACK_NOTIFY_CHANNEL", "")
send_slack(
    os.environ.get("SLACK_BOT_TOKEN", ""), channel,
    ":rotating_light: *QA Auto-Fix CRASHED* — `{tag}`\n"
    "The healing run aborted (exit {code}, near run.sh:{line}); no PR was created.\n"
    "_Audit: `{sid}`_".format(
        tag=os.environ.get("BUILD_TAG", "unknown"),
        line=os.environ.get("LINE_NO", "?"),
        code=os.environ.get("EXIT_CODE", "?"),
        sid=os.environ.get("SESSION_ID", "unknown"),
    ),
)
PYALERT
    log "Crash reported to Slack"
  fi

  log "Handoff left in the queue for a retry: $HANDOFF_FILE"
  exit "$exit_code"
}
trap 'on_error $LINENO' ERR

# ── Steps 01 + 02 — Locate, then Fix, once per attempt ───────────────────────
# One broken locator hides the next: the test cannot reach locator #2 until #1 is
# repaired and it is re-run. So Locate runs inside the loop, working each time
# from the artifacts the last verification run wrote — that is what makes every
# link of a chain deterministic instead of only the first. It never edits a file
# (01_fix decides what to do with the result) and costs about a second, which is
# nothing beside the test run it precedes.
#
# The loop stops when 01_fix says so. The budget it applies counts attempts that
# made NO progress: an edit that repairs one locator and uncovers the next leaves
# the run red but is not a failed retry, and charging it as one meant a long chain
# could never finish. See retry_verdict in 01_fix.py.
HEALING_RETRY_COUNT="${HEALING_RETRY_COUNT:-4}"
if [[ -n "${MAX_FIX_ATTEMPTS:-}" ]]; then
  log "NOTE: MAX_FIX_ATTEMPTS is set but no longer read — use HEALING_RETRY_COUNT (currently $HEALING_RETRY_COUNT)"
fi
FIX_ATTEMPT=1

while true; do
  LOCATE_LABEL="[02/04] Locate"
  if [[ "$FIX_ATTEMPT" -gt 1 ]]; then
    LOCATE_LABEL="[02/04] Locate (attempt $FIX_ATTEMPT)"
  fi
  # Exported before EACH step, not once per pass: run_step unsets it on the way
  # out, so a single export at the top of the loop reached Locate and left Fix
  # recording every attempt as attempt 1 — which is what attributes the spend.
  export STEP_ATTEMPT="$FIX_ATTEMPT"
  run_step "$LOCATE_LABEL" \
    "FIX_ATTEMPT=$FIX_ATTEMPT python3 '$AGENT_DIR/actions/01_locate.py'" locate

  export STEP_ATTEMPT="$FIX_ATTEMPT"
  run_step "[03/04] Fix (attempt $FIX_ATTEMPT)" \
    "FIX_ATTEMPT=$FIX_ATTEMPT python3 '$AGENT_DIR/actions/01_fix.py'" fix

  # Missing means the step never got far enough to decide — stop rather than
  # loop on a file nobody wrote.
  RETRY_VERDICT=$(tr -d '\n' < "$AUDIT_DIR/.fix-retry" 2>/dev/null || echo "stop: no verdict written")
  if [[ "$RETRY_VERDICT" != "retry" ]]; then
    if [[ "$RETRY_VERDICT" == stop:* ]]; then
      log "${RETRY_VERDICT#stop: }"
    fi
    break
  fi

  log "Some tests still failing — retrying fix (attempt $((FIX_ATTEMPT + 1)))"
  FIX_ATTEMPT=$((FIX_ATTEMPT + 1))
done

# ── Step 02 — Ship (PR + Slack) ───────────────────────────────────────────────
run_step "[04/04] Ship" "python3 '$AGENT_DIR/actions/02_ship.py'" ship

# ── Mark handoff as processed ─────────────────────────────────────────────────
# An infra skip (no GitHub token, workspace missing) means nothing was attempted.
# Consuming the handoff there would lose the work and force a full re-triage, so
# leave it queued for the next run instead.
# The redirect itself fails noisily when the file is absent — which is the normal
# case for a successful run — so guard on existence rather than on tr's exit code.
SKIP_REASON=""
if [[ -f "$AUDIT_DIR/.skip-reason" ]]; then
  SKIP_REASON=$(tr -d '\n' < "$AUDIT_DIR/.skip-reason")
fi
if [[ "$MODE" == "local" ]]; then
  log "Standalone run — handoff kept with the session: $HANDOFF_FILE"
elif [[ "$SKIP_REASON" == "infra" ]]; then
  # Return the claim to the queue so another run (or a retry) picks it up.
  if [[ -n "${CLAIM_DIR:-}" && -f "$HANDOFF_FILE" ]]; then
    mv "$HANDOFF_FILE" "$QUEUE_DIR/" 2>/dev/null || true
    HANDOFF_FILE="$QUEUE_DIR/$(basename "$HANDOFF_FILE")"
  fi
  log "Infra skip — leaving handoff queued for retry: $HANDOFF_FILE"
else
  mv "$HANDOFF_FILE" "$PROCESSED_DIR/$(basename "$HANDOFF_FILE")"
  log "Moved handoff to processed: $PROCESSED_DIR/$(basename "$HANDOFF_FILE")"
fi

# ── Final summary ─────────────────────────────────────────────────────────────
flush_step_done
TOTAL_ELAPSED=$(elapsed_since $SESSION_START)
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
log "Done. Total time: $(fmt_duration $TOTAL_ELAPSED)"
echo ""
print_step_table 50

# Roll up now so the spend is on screen with the timings rather than only in
# metrics.json. The EXIT trap re-runs this; a rollup is idempotent.
_METRICS=$(cd "${REPO_ROOT:-.}" && python3 -m shared.metrics 2>/dev/null || true)
[[ -n "$_METRICS" ]] && log "Spend: $_METRICS"


# What each attempt actually changed. The step logs interleave this with the
# build output, so by the end you would have to scroll through several minutes
# of maven to answer "what did it try?" — especially after a reverted attempt.
if [[ -f "$AUDIT_DIR/01-fix.json" ]]; then
  python3 - "$AUDIT_DIR/01-fix.json" <<'PYSUM' || true
import json, sys
from pathlib import Path
try:
    data = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    sys.exit(0)
history = data.get("attempts") or []
if not any(a.get("entries") for a in history):
    sys.exit(0)
print("")
print("  Changes attempted:")
for a in history:
    n = a.get("attempt")
    if not a.get("entries"):
        print(f"    attempt {n}: nothing applied")
        continue
    # One edit is recorded once per failing test it covers, so a locator three
    # tests shared printed its description and diff three times over. Group on
    # the change itself; the verdicts below it say which test got which result,
    # which was the only thing that ever differed between those copies.
    groups = {}
    for e in a["entries"]:
        tgt = Path(e["target_file"]).name if e.get("target_file") else "-"
        why = e.get("fix_description") or e.get("unfixable_reason") or ""
        diff = "\n".join(l for l in (e.get("fix_diff") or "").splitlines()
                         if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
        verdict = e.get("outcome", "?") + (", reverted" if e.get("reverted") else "")
        tests = e.get("test_names") or [e.get("test_name")]
        groups.setdefault((tgt, why, diff), {}).setdefault(verdict, []).extend(
            t.rsplit(".", 1)[-1] for t in tests if t)

    for (tgt, why, diff), verdicts in groups.items():
        head = f"    attempt {n}: {tgt}"
        if len(verdicts) == 1:
            head += f" — {next(iter(verdicts))}"
        print(head)
        if why:
            print(f"      {why[:150]}")
        for line in diff.splitlines():
            print(f"      {line[:150]}")
        for verdict, tests in verdicts.items():
            label = f"{verdict}: " if len(verdicts) > 1 else ""
            if label or len(tests) > 1:
                print(f"      {label}{', '.join(tests)}"[:150])
PYSUM
fi
echo ""
log "Audit: $AUDIT_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
