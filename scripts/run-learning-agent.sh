#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/run-learning-agent.sh
# Run the learning-agent with live logs: read people's feedback on the agents'
# work, propose lessons, write a report (agents/learning-agent/CLAUDE.md).
#
#   ./scripts/run-learning-agent.sh                  # report only
#   AUTO_PUSH=true ./scripts/run-learning-agent.sh   # and open the lesson PRs
#   STOP_AFTER=classify ./scripts/run-learning-agent.sh
# ─────────────────────────────────────────────────────────────────────────────
exec "$(dirname "${BASH_SOURCE[0]}")/_run-agent.sh" learning-agent
