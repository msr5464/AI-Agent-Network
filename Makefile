# QA AI Agent — Makefile
# Usage:
#   make run AGENT=qa-auto-analyse                              # scout mode
#   make run AGENT=qa-auto-analyse BUILD_TAG=ProdSanity-541    # direct mode
#   make run AGENT=qa-auto-fix                                  # fix queue mode
#   make run AGENT=qa-auto-fix BUILD_TAG=ProdSanity-541        # fix direct mode
#   make audit AGENT=qa-auto-analyse                            # list recent sessions
#   make audit AGENT=qa-auto-analyse SESSION=20260328-...       # view specific session
#   make setup                                                   # install deps
#   make test                                                    # run unit tests

AGENT     ?= qa-auto-analyse
BUILD_TAG ?=
SESSION   ?=
AGENTS_DIR = agents

# ── Run ────────────────────────────────────────────────────────────────────────
.PHONY: run
run:
	@if [ -z "$(AGENT)" ]; then echo "Usage: make run AGENT=qa-auto-analyse  OR  make run AGENT=qa-auto-fix"; exit 1; fi
	@if [ ! -f "$(AGENTS_DIR)/$(AGENT)/run.sh" ]; then echo "Agent not found: $(AGENTS_DIR)/$(AGENT)/run.sh"; exit 1; fi
	@BUILD_TAG="$(BUILD_TAG)" bash "$(AGENTS_DIR)/$(AGENT)/run.sh" "$(BUILD_TAG)"

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
	pip install -r requirements.txt
	@echo ""
	@echo "Setup complete."
	@echo "Next: copy config/.env.example to config/.env and fill in your credentials."

# ── Tests ──────────────────────────────────────────────────────────────────────
.PHONY: test
test:
	pytest tests/unit/ -v

.PHONY: test-cov
test-cov:
	pytest tests/unit/ --cov=src --cov-report=term-missing

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
	@echo "QA AI Agent"
	@echo ""
	@echo "Usage:"
	@echo "  make run AGENT=qa-auto-analyse                     Scout mode (auto-select build)"
	@echo "  make run AGENT=qa-auto-analyse BUILD_TAG=tag       Direct mode (specific build)"
	@echo "  make run AGENT=qa-auto-analyse STOP_AFTER=collect  Stop after collect step"
	@echo ""
	@echo "  make run AGENT=qa-auto-fix                         Fix queue mode (picks oldest)"
	@echo "  make run AGENT=qa-auto-fix BUILD_TAG=tag           Fix direct mode (specific build)"
	@echo "  AUTO_PUSH=false make run AGENT=qa-auto-fix         Dry-run (no PR)"
	@echo ""
	@echo "  make audit AGENT=qa-auto-analyse                   List recent sessions"
	@echo "  make audit AGENT=qa-auto-analyse SESSION=...       View specific session"
	@echo "  make feedback AGENT=qa-auto-analyse                Show feedback files"
	@echo "  make clear-feedback AGENT=qa-auto-analyse          Clear skip-buildtags.json"
	@echo ""
	@echo "  make setup                                         Install deps"
	@echo "  make test                                          Run unit tests"
	@echo ""
	@echo "Environment (qa-auto-analyse):"
	@echo "  STOP_AFTER=scout|collect|classify|review           Stop pipeline early"
	@echo ""
	@echo "Environment (qa-auto-fix):"
	@echo "  AUTO_PUSH=false                                    Dry-run (no PR)"
	@echo "  MAX_FIX_ATTEMPTS=2                                 Max retry cycles"
	@echo ""
