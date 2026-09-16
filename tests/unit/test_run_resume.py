"""Recovering the queue file when a run is retried from one of its steps.

The retry button sends no module — the server recovers it from the session's own
audit trail. It looked the session up in the *authoring* agent's audit dir no
matter which agent had run, so every adaptation retry died with "could not
determine module for session ...". Behind that sat a second trap: an adaptation
session's `module` is the product module from the note's `Module:` header
(`SauceDemo`), while run.sh resolves `queue/<name>.txt` and needs the file's name.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qa_agents_server import runner                      # noqa: E402
from qa_agents_server.agents import get_agent            # noqa: E402


class TestRecoverModule:
    def test_the_session_is_looked_up_under_the_agent_that_ran_it(self, monkeypatch):
        seen = {}

        def fake_get_session(session_id, agent=None):
            seen["agent"] = agent
            if agent != "test-adaptation-agent":
                return None          # what the authoring audit dir really returns
            return {"module": "SauceDemo", "note": "saucedemo_cart_details"}

        monkeypatch.setattr(runner, "_get_session", fake_get_session)
        recovered = runner._recover_module(get_agent("test-adaptation-agent"), "sid")

        assert seen["agent"] == "test-adaptation-agent"
        assert recovered == "saucedemo_cart_details", (
            "run.sh resolves queue/<module>.txt — the note's `Module:` header "
            "names a product module, not a file")

    def test_an_agent_without_a_note_falls_back_to_its_module(self, monkeypatch):
        monkeypatch.setattr(runner, "_get_session",
                            lambda session_id, agent=None: {"module": "payments"})
        assert runner._recover_module(get_agent("test-authoring-agent"), "sid") == "payments"

    def test_a_session_that_cannot_be_read_recovers_nothing(self, monkeypatch):
        monkeypatch.setattr(runner, "_get_session",
                            lambda session_id, agent=None: None)
        assert runner._recover_module(get_agent("test-authoring-agent"), "sid") is None
