"""Tests for the rest of the learning loop: Slack replies as feedback, precedents,
the review-outcomes summary and its routes, and lessons on analytics rows."""

import io
import json
import time
from types import SimpleNamespace

import pytest

from shared import feedback, lessons, precedents, slack, slack_feedback


# ── Slack ─────────────────────────────────────────────────────────────────────

class TestSlackReplies:
    @pytest.mark.parametrize("text, said", [
        ("lesson: read the total from the cart page", {"kind": "lesson",
                                                       "text": "read the total from the cart page"}),
        ("Verdict: reject wrong-flow, no-reuse — it never reached the cart",
         {"kind": "verdict", "verdict": "reject", "reasons": ["wrong-flow", "no-reuse"],
          "text": "it never reached the cart"}),
        ("verdict: accept", {"kind": "verdict", "verdict": "accept", "reasons": [], "text": ""}),
        ("nice one", {"kind": "comment", "text": "nice one"}),
    ])
    def test_what_a_reply_says(self, text, said):
        assert slack_feedback.parse(text) == said

    def test_a_posted_message_is_remembered_in_its_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
        monkeypatch.setattr(slack, "urlopen", lambda req, timeout: io.BytesIO(
            json.dumps({"ok": True, "channel": "C1", "ts": "1.5"}).encode()))
        assert slack.send_slack("xoxb", "#qa", "hello") is True
        assert json.loads((tmp_path / slack.MESSAGES_FILE).read_text())[0]["ts"] == "1.5"

    def _run(self, tmp_path, agent="test-adaptation-agent", session="s1"):
        audit = tmp_path / "agents" / agent / "audit" / session
        audit.mkdir(parents=True)
        (audit / slack.MESSAGES_FILE).write_text(json.dumps([{"channel": "C1", "ts": "1.0"}]))
        return tmp_path / "agents"

    def test_replies_become_events_and_a_verdict_reaches_its_agent(self, tmp_path):
        agents = self._run(tmp_path)
        feedback_dir = agents / "test-adaptation-agent" / "feedback"
        from shared import verdict_feedback
        verdict_feedback.record_proposal(feedback_dir / "proposals.json", "s1", "shop", 1, "locator", None)
        replies = {"ok": True, "messages": [
            {"ts": "1.0", "bot_id": "B1", "text": "the bot's own message"},
            {"ts": "2.0", "user": "U1", "text": "verdict: edited convention — use the helper"},
            {"ts": "3.0", "user": "U2", "text": "lesson: chain the pages"},
        ]}
        result = slack_feedback.collect(agents, fetch=lambda channel, ts: replies)
        kinds = sorted((e["kind"], e["source"], e["explicit"]) for e in result["new"])
        assert kinds == [("lesson", "slack", True), ("verdict", "slack", True)]
        assert json.loads((feedback_dir / "proposals.json").read_text())[0]["verdict"] == "edited"
        assert slack_feedback.collect(agents, fetch=lambda c, t: replies)["new"] == []   # idempotent

    def test_without_history_scope_it_says_so_and_stops(self, tmp_path):
        agents = self._run(tmp_path)
        said = []
        result = slack_feedback.collect(agents, log=said.append,
                                        fetch=lambda c, t: {"ok": False, "error": "missing_scope"})
        assert "missing_scope" in result["skipped"] and "channels:history" in said[0]

    def test_no_token_no_reading(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        assert "not set" in slack_feedback.collect(tmp_path)["skipped"]


# ── Precedents ────────────────────────────────────────────────────────────────

def _healing_run(agents, session, page="CartPage", verdict="LOCATOR_STALE"):
    audit = agents / "test-healing-agent" / "audit" / session
    audit.mkdir(parents=True)
    (audit / "01-fix.json").write_text(json.dumps({"fixes": [{
        "status": "success", "target_file": f"/tmp/w/src/{page}.java",
        "diagnosis": {"verdict": verdict}, "failed_selector": "#total",
        "root_cause": "total moved", "fix_diff": "--- a\n+++ b\n-#total\n+[data-test='total']"}]}))


class TestPrecedents:
    def test_only_explicitly_accepted_work_counts(self, tmp_path):
        agents = tmp_path / "agents"
        _healing_run(agents, "accepted")
        _healing_run(agents, "unjudged")
        _healing_run(agents, "rejected")
        feedback.person_verdict("test-healing-agent", "accepted", "accept", agents_dir=agents)
        feedback.person_verdict("test-healing-agent", "rejected", "reject", agents_dir=agents)
        assert [p["session"] for p in precedents.build("test-healing-agent", agents)] == ["accepted"]

    def test_an_edited_acceptance_shows_what_the_person_changed(self, tmp_path):
        agents = tmp_path / "agents"
        _healing_run(agents, "s1")
        feedback.person_verdict("test-healing-agent", "s1", "edited", agents_dir=agents)
        feedback.append([feedback.event("human_commit", source="github", key=("c",), explicit=False,
                                        agent="test-healing-agent", session="s1",
                                        diff="+[data-test='cart-total']")])
        (item,) = precedents.select("test-healing-agent", page="CartPage", agents_dir=agents)
        assert item["people_changed"] == "+[data-test='cart-total']"
        assert "as people corrected it" in precedents.render([item])

    def test_the_same_page_or_verdict_is_what_matches(self, tmp_path):
        agents = tmp_path / "agents"
        _healing_run(agents, "s1", page="CartPage")
        feedback.person_verdict("test-healing-agent", "s1", "accept", agents_dir=agents)
        assert precedents.select("test-healing-agent", page="LoginPage", agents_dir=agents) == []
        assert precedents.select("test-healing-agent", failure="LOCATOR_STALE", agents_dir=agents)
        assert precedents.select("test-healing-agent", "s1", page="CartPage", agents_dir=agents) == []

    def test_a_prompt_gets_them_only_when_lessons_are_on(self, tmp_path, monkeypatch):
        agents = tmp_path / "agents"
        _healing_run(agents, "s1")
        feedback.person_verdict("test-healing-agent", "s1", "accept", agents_dir=agents)
        monkeypatch.setattr(precedents, "AGENTS_DIR", agents)
        audit = tmp_path / "now"
        audit.mkdir()
        monkeypatch.setenv("AUDIT_DIR", str(audit))
        monkeypatch.setenv("STEP_KEY", "fix")
        monkeypatch.setenv("LESSONS_MODE", "shadow")
        assert precedents.block("test-healing-agent", page="CartPage") == ""
        assert json.loads((audit / "00-lessons.json").read_text())["steps"]["fix"]["precedents"] == ["s1"]
        monkeypatch.setenv("LESSONS_MODE", "on")
        assert "Accepted fixes for similar failures" in precedents.block("test-healing-agent",
                                                                         page="CartPage")


# ── Review outcomes and the routes ────────────────────────────────────────────

def _row(session, agent="test-healing-agent", status="completed", lesson_ids=()):
    return {"agent": agent, "session_id": session, "status": status, "started_at": time.time() - 60,
            "user_id": "user-admin", "cost_usd": 0.5, "input_tokens": 10, "output_tokens": 10,
            "lessons": list(lesson_ids), "outcomes": {"tests_fixed": 1}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "run_analytics.jsonl"
    monkeypatch.setenv("RUN_ANALYTICS_FILE", str(path))

    def write(rows):
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return write


def test_the_summary_counts_the_same_runs_as_analytics(store, tmp_path, monkeypatch):
    from qa_agents_server import analytics, review_outcomes
    store([_row("s1", lesson_ids=["L-healing-001"]), _row("s2", lesson_ids=["L-healing-001"]),
           _row("s3", status="cancelled"), _row("s4", agent="test-authoring-agent", status="failed")])
    feedback.person_verdict("test-healing-agent", "s1", "edited", agents_dir=tmp_path)
    feedback.append([feedback.design_generation("r1", [{"title": "A"}, {"title": "B"}], [],
                                                lessons=["L-design-001"]),
                     feedback.design_push("r1", "created", "C1", "generated", {"title": "A"},
                                          {"title": "A2"})])
    summary = review_outcomes.summary("7d")
    healing = summary["by_agent"]["test-healing-agent"]
    assert (healing["runs"], healing["edited"], healing["not_reviewed"]) == (2, 1, 1)
    assert healing["runs"] == analytics.query("7d")["by_agent"]["test-healing-agent"]["runs"]
    design = summary["by_agent"]["test-design-agent"]
    assert (design["tests_offered"], design["pushed_after_edits"]) == (2, 1)
    by_id = {l["id"]: l for l in summary["lessons"]}
    assert by_id["L-healing-001"]["runs"] == 2 and by_id["L-design-001"]["edited"] == 1
    assert summary["explicit_by_source"] == {"cli": 1, "studio": 1}


@pytest.fixture
def client(monkeypatch):
    from flask import Flask
    from qa_agents_server import routes
    app = Flask(__name__)
    app.register_blueprint(routes.qa_bp)
    return app.test_client()


def test_the_summary_route(client, store):
    store([_row("s1")])
    body = client.get("/feedback/summary?window=7d", headers={"X-User-Role": "admin"}).get_json()
    assert body["by_agent"]["test-healing-agent"]["runs"] == 1
    assert client.get("/feedback/summary?window=nope").status_code == 400


def test_the_lessons_route_follows_the_mode(client, tmp_path, monkeypatch):
    folder = tmp_path / "lessons"
    folder.mkdir()
    monkeypatch.setenv("LESSONS_DIR", str(folder))
    (folder / "test-design-agent.yaml").write_text(lessons.file_text("test-design-agent", [{
        "id": "L-design-001", "status": "active", "lever": "lesson",
        "statement": "Write one expected result per test, not a list.", "scope": {}, "evidence": ["e"]}]))
    monkeypatch.setenv("LESSONS_MODE", "shadow")
    body = client.get("/lessons/test-design-agent?step=generate").get_json()
    assert body == {"mode": "shadow", "ids": ["L-design-001"], "block": ""}
    monkeypatch.setenv("LESSONS_MODE", "on")
    assert "[L-design-001]" in client.get("/lessons/test-design-agent?step=generate").get_json()["block"]
    assert client.get("/lessons/test-nobody-agent").status_code == 404


def test_an_analytics_row_names_the_lessons_its_run_was_given(tmp_path, monkeypatch):
    from qa_agents_server import analytics
    audit = tmp_path / "audit" / "s1"
    audit.mkdir(parents=True)
    (audit / "00-lessons.json").write_text(json.dumps(
        {"steps": {"fix": {"mode": "on", "ids": ["L-healing-001"]},
                   "locate": {"mode": "shadow", "ids": ["L-healing-002"]}}}))
    monkeypatch.setattr(analytics, "AGENTS_DIR", tmp_path / "agents")
    (tmp_path / "agents" / "audit").mkdir(parents=True)
    (tmp_path / "agents" / "audit" / "run.sh").write_text("")
    record = analytics.build_record(audit, agent="audit")
    assert record["lessons"] == ["L-healing-001"]
