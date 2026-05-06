"""QA Agents HTTP + SSE server.

Thin Flask wrapper around the existing test-authoring-agent run.sh pipeline.
Exposes REST endpoints for triggering runs and streaming live progress; does
not replace the CLI / Makefile entry points — both paths coexist.
"""

__version__ = "0.1.0"
