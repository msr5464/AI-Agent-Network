# QA Agent Network — Makefile
# Usage:
#   make run AGENT=test-triaging-agent                              # scout mode
#   make run AGENT=test-triaging-agent BUILD_TAG=ProdSanity-541    # direct mode
#   make run AGENT=test-healing-agent                                  # fix queue mode
#   make run AGENT=test-healing-agent BUILD_TAG=ProdSanity-541        # fix direct mode
#   make run AGENT=test-healing-agent TEST=LoginTest#testLogin        # standalone: run + fix one test
#   make audit AGENT=test-triaging-agent                            # list recent sessions
#   make audit AGENT=test-triaging-agent SESSION=20260328-...       # view specific session
#   make setup                                                   # install deps
#   make test                                                    # run unit tests

# The interpreter every Python target runs through. An activated virtualenv wins,
# then a .venv/ in the repo, then whatever python3 is on PATH. Without this,
# `make test` runs the bare `pytest` on PATH — which on a machine that keeps its
# dependencies in .venv is a different interpreter than the one they are
# installed into, and the suite fails with import errors that look like missing
# packages rather than the wrong python. Override with `make test PY=...`.
PY ?= $(shell if [ -n "$$VIRTUAL_ENV" ] && [ -x "$$VIRTUAL_ENV/bin/python" ]; then \
                  echo "$$VIRTUAL_ENV/bin/python"; \
              elif [ -x .venv/bin/python ]; then echo .venv/bin/python; \
              else echo python3; fi)

AGENT     ?= test-triaging-agent
BUILD_TAG ?=
MODULE    ?=
SESSION   ?=
AGENTS_DIR = agents

# ── Run ────────────────────────────────────────────────────────────────────────
.PHONY: run
run:
	@if [ -z "$(AGENT)" ]; then echo "Usage: make run AGENT=<test-authoring-agent|test-triaging-agent|test-healing-agent|test-adaptation-agent>"; exit 1; fi
	@if [ ! -f "$(AGENTS_DIR)/$(AGENT)/run.sh" ]; then echo "Agent not found: $(AGENTS_DIR)/$(AGENT)/run.sh"; exit 1; fi
	@BUILD_TAG="$(BUILD_TAG)" MODULE="$(MODULE)" TEST="$(TEST)" bash "$(AGENTS_DIR)/$(AGENT)/run.sh" "$(BUILD_TAG)"

# ── Audit ──────────────────────────────────────────────────────────────────────
.PHONY: audit
audit:
	@if [ -n "$(SESSION)" ]; then \
		SESSION_DIR="$(AGENTS_DIR)/$(AGENT)/audit/$(SESSION)"; \
		if [ -d "$$SESSION_DIR" ]; then \
			echo ""; \
			echo "Session: $(SESSION)"; \
			echo ""; \
			ls -la "$$SESSION_DIR"; \
			echo ""; \
			if [ -f "$$SESSION_DIR/.verdict" ]; then echo "Verdict: $$(cat $$SESSION_DIR/.verdict)"; fi; \
			if [ -f "$$SESSION_DIR/.fix-passed" ]; then echo "Fix gate: $$(tr -d '\n' < $$SESSION_DIR/.fix-passed)"; fi; \
			if [ -f "$$SESSION_DIR/05-ship.json" ]; then \
				echo ""; \
				echo "=== 05-ship.json ==="; \
				cat "$$SESSION_DIR/05-ship.json"; \
			elif [ -f "$$SESSION_DIR/02-ship.json" ]; then \
				echo ""; \
				echo "=== 02-ship.json ==="; \
				cat "$$SESSION_DIR/02-ship.json"; \
			fi; \
		else \
			echo "Session not found: $$SESSION_DIR"; \
		fi; \
	else \
		echo ""; \
		echo "Recent sessions for $(AGENT):"; \
		echo ""; \
		if [ -d "$(AGENTS_DIR)/$(AGENT)/audit" ]; then \
			ls -lt "$(AGENTS_DIR)/$(AGENT)/audit" | head -20; \
		else \
			echo "No audit directory found."; \
		fi; \
	fi

# ── Setup ──────────────────────────────────────────────────────────────────────
.PHONY: setup
setup:
	@echo "Installing Python dependencies..."
	$(PY) -m pip install -r requirements.txt
	@echo ""
	@echo "Setup complete."
	@echo "Next: copy config/.env.example to config/.env and fill in your credentials."
	@echo "Then run: make setup-mcp  (configures MCP tools in ~/.claude.json)"

.PHONY: setup-mcp
setup-mcp: ## Configure MCP tools (GitHub, Slack) in ~/.claude.json
	bash scripts/setup-mcp.sh

# ── Dashboard ─────────────────────────────────────────────────────────────────
PORT ?= 8888
.PHONY: dashboard
dashboard: ## Browse audit trails in web UI (PORT=8888)
	$(PY) scripts/audit_viewer.py --agents-dir agents --port $(PORT)

# ── Tests ──────────────────────────────────────────────────────────────────────
.PHONY: test
test:
	$(PY) -m pytest tests/unit/ -v

.PHONY: test-cov
test-cov:
	$(PY) -m pytest tests/unit/ --cov=agents/test-triaging-agent/lib --cov-report=term-missing

# ── Feedback ───────────────────────────────────────────────────────────────────
.PHONY: feedback
feedback:
	@echo "=== skip-buildtags.json ==="
	@cat "$(AGENTS_DIR)/$(AGENT)/feedback/skip-buildtags.json" 2>/dev/null || echo "(empty)"
	@echo ""
	@echo "=== known-issues.json ==="
	@cat "$(AGENTS_DIR)/$(AGENT)/feedback/known-issues.json" 2>/dev/null || echo "(empty)"

.PHONY: clear-feedback
clear-feedback:
	@echo '[]' > "$(AGENTS_DIR)/$(AGENT)/feedback/skip-buildtags.json"
	@echo "Cleared skip-buildtags.json"

# ── Help ───────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "QA Agent Network"
	@echo ""
	@echo "Usage:"
	@echo "  make run AGENT=test-triaging-agent                     Scout mode (auto-select build)"
	@echo "  make run AGENT=test-triaging-agent BUILD_TAG=tag       Direct mode (specific build)"
	@echo "  make run AGENT=test-triaging-agent STOP_AFTER=collect  Stop after collect step"
	@echo ""
	@echo "  make run AGENT=test-healing-agent                         Fix queue mode (picks oldest)"
	@echo "  make run AGENT=test-healing-agent BUILD_TAG=tag           Fix direct mode (specific build)"
	@echo "  AUTO_PUSH=false make run AGENT=test-healing-agent         Dry-run (no PR)"
	@echo ""
	@echo "  make run AGENT=test-healing-agent TEST=Class#method        Standalone: run one test locally and fix it"
	@echo "  make run AGENT=test-healing-agent TEST=Class               Standalone: whole class"
	@echo "  REPAIR=true  make run AGENT=test-healing-agent TEST=...    Park the browser for live inspection"
	@echo "  FORCE=true   make run AGENT=test-healing-agent TEST=...    Fix even if it is not a locator failure"
	@echo ""
	@echo "  make audit AGENT=test-triaging-agent                   List recent sessions"
	@echo "  make audit AGENT=test-triaging-agent SESSION=...       View specific session"
	@echo "  make feedback AGENT=test-triaging-agent                Show feedback files"
	@echo "  make clear-feedback AGENT=test-triaging-agent          Clear skip-buildtags.json"
	@echo ""
	@echo "  make run AGENT=test-authoring-agent MODULE=payments       Create from queue/payments.txt"
	@echo "  AUTO_PUSH=false make run AGENT=test-authoring-agent      Dry-run (generates + tests, no PR)"
	@echo "  make audit AGENT=test-authoring-agent                    List recent sessions"
	@echo ""
	@echo "  make setup                                         Install deps + show next steps"
	@echo "  make setup-mcp                                     Configure MCP tools in ~/.claude.json"
	@echo "  make dashboard                                     Browse audit sessions at localhost:8888"
	@echo "  make dashboard PORT=9000                           Use custom port"
	@echo "  make test                                          Run unit tests"
	@echo ""
	@echo "Environment (test-triaging-agent):"
	@echo "  STOP_AFTER=scout|collect|classify|review           Stop pipeline early"
	@echo ""
	@echo "Environment (test-healing-agent):"
	@echo "  AUTO_PUSH=false                                    Dry-run (no PR)"
	@echo "  MAX_FIX_ATTEMPTS=2                                 Max retry cycles"
	@echo ""
