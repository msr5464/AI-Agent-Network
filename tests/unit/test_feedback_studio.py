"""Feedback given in the Studio: a person's verdict on an agent run, and what test
design generated and a person then pushed to TestRail (shared/feedback.py and the
routes that record them).
"""

import dataclasses
import json
from types import SimpleNamespace

import pytest

from shared import feedback


class TestPersonVerdict:
    def test_records_an_explicit_verdict_with_who_and_where(self, tmp_path):
        result = feedback.person_verdict("test-authoring-agent", "s1", "edited",
                                         ["no-reuse"], note="use the cart helper",
                                         who="user-admin", source="studio", agents_dir=tmp_path)
        event = result["event"]
        assert (event["kind"], event["source"], event["explicit"]) == ("verdict", "studio", True)
        assert event["reasons"] == ["no-reuse"] and event["who"] == "user-admin"

    @pytest.mark.parametrize("agent, verdict, reasons", [
        ("test-design-agent", "accept", []),        # not an agent run
        ("test-healing-agent", "maybe", []),
        ("test-healing-agent", "reject", ["too-slow"]),
    ])
    def test_refuses_what_it_cannot_record(self, tmp_path, agent, verdict, reasons):
        with pytest.raises(ValueError):
            feedback.person_verdict(agent, "s1", verdict, reasons, agents_dir=tmp_path)
        assert feedback.read() == []

    def test_the_newest_verdict_wins_whoever_gave_it(self, tmp_path):
        feedback.person_verdict("test-healing-agent", "s1", "reject", ["wrong-locator"],
                                agents_dir=tmp_path, source="cli")
        feedback.person_verdict("test-healing-agent", "s1", "accept", agents_dir=tmp_path,
                                source="studio", who="user-admin")
        latest = feedback.latest_verdict("test-healing-agent", "s1")
        assert latest["verdict"] == "accept" and latest["source"] == "studio"
        assert feedback.latest_verdict("test-healing-agent", "other") is None


class TestDesignEvents:
    CASE = {"title": "Pay by card", "priority": "p1", "steps": "1. Add item\r\n2. Pay",
            "expected_result": "Order placed"}

    def test_a_test_pushed_as_written_is_accepted(self):
        event = feedback.design_push("r1", "created", "C9", "generated", self.CASE,
                                     {**self.CASE, "priority": "P1", "steps": "1. Add item\n2. Pay"})
        assert event["verdict"] == "accept" and event["diff"] == ""
        assert event["explicit"] is True and event["agent"] == feedback.DESIGN_AGENT

    def test_a_test_edited_before_the_push_carries_the_edit(self):
        final = {**self.CASE, "expected_result": "Order placed and a receipt is shown"}
        event = feedback.design_push("r1", "updated", "C9", "suggested_update", self.CASE, final,
                                     target={"kind": "generated", "req_id": "R1", "index": 0})
        assert event["verdict"] == "edited"
        assert event["artifact"]["edited_fields"] == ["expected_result"]
        assert "+Order placed and a receipt is shown" in event["diff"]

    def test_a_generation_lists_what_was_offered(self):
        event = feedback.design_generation(
            "r1", [{"target": {"kind": "e2e", "index": 0}, "title": "Checkout", "priority": "P0",
                    "steps": "not kept"}],
            [{"req_id": "R1", "testrail_id": "C3", "title": "Login"}])
        assert event["explicit"] is False
        assert event["artifact"]["tests"] == [{"target": {"kind": "e2e", "index": 0},
                                               "title": "Checkout", "priority": "P0"}]


@pytest.fixture
def server(tmp_path, monkeypatch):
    from flask import Flask
    from qa_agents_server import agents as agents_mod, audit_reader, routes

    for name in ("test-healing-agent", "test-adaptation-agent"):
        spec = dataclasses.replace(agents_mod.AGENTS[name], audit_dir=tmp_path / name / "audit")
        monkeypatch.setitem(agents_mod.AGENTS, name, spec)
    (tmp_path / "test-healing-agent" / "audit" / "s1").mkdir(parents=True)
    monkeypatch.setattr(routes, "_owns", lambda session_id: True)
    monkeypatch.setattr(audit_reader, "get_session",
                        lambda sid, agent=None: {"session_id": sid, "test_name": "CartTest.pay"})
    app = Flask(__name__)
    app.register_blueprint(routes.qa_bp)
    return SimpleNamespace(client=app.test_client(), routes=routes,
                           headers={"X-User-ID": "aaaaaaaaaaaa"})


class TestVerdictRoute:
    URL = "/agents/test-healing-agent/sessions/s1/verdict"

    def test_a_verdict_given_in_the_studio_is_recorded_and_read_back(self, server, tmp_path,
                                                                     monkeypatch):
        monkeypatch.setattr(feedback, "REPO_ROOT", tmp_path)          # known-issues stays scratch
        response = server.client.post(self.URL, headers=server.headers,
                                      json={"verdict": "reject", "reasons": ["wrong-flow"],
                                            "note": "it never reached the cart"})
        assert response.status_code == 201
        body = response.get_json()
        assert body["verdict"]["verdict"] == "reject" and body["wrong_diagnosis"] is True
        assert server.client.get(self.URL, headers=server.headers).get_json()["verdict"]["text"] \
            == "it never reached the cart"
        detail = server.client.get("/agents/test-healing-agent/sessions/s1",
                                   headers=server.headers).get_json()
        assert detail["reviewer_verdict"]["verdict"] == "reject"

    def test_a_bad_verdict_is_a_400_and_records_nothing(self, server):
        response = server.client.post(self.URL, headers=server.headers, json={"verdict": "meh"})
        assert response.status_code == 400 and feedback.read() == []

    def test_an_unknown_session_is_a_404(self, server):
        response = server.client.post("/agents/test-healing-agent/sessions/nope/verdict",
                                      headers=server.headers, json={"verdict": "accept"})
        assert response.status_code == 404


class TestDesignRoute:
    def test_records_a_push_from_the_studio_backend(self, server):
        response = server.client.post("/feedback/test-design", headers=server.headers, json={
            "kind": "test_push", "session": "r1", "action": "created", "testrail_id": "C9",
            "source": "generated", "original": {"title": "A"}, "final": {"title": "B"}})
        assert response.status_code == 201
        (event,) = feedback.read()
        assert event["kind"] == "test_push" and event["verdict"] == "edited"

    def test_refuses_a_caller_without_the_proxy_secret(self, server, monkeypatch):
        monkeypatch.setenv("QA_AGENT_PROXY_SECRET", "s3cret")
        payload = {"kind": "generation", "session": "r1", "tests": []}
        assert server.client.post("/feedback/test-design", json=payload).status_code == 403
        assert server.client.post("/feedback/test-design", json=payload,
                                  headers={"X-Proxy-Secret": "s3cret"}).status_code == 201

    def test_an_unknown_kind_is_a_400(self, server):
        assert server.client.post("/feedback/test-design", json={"kind": "x"}).status_code == 400
