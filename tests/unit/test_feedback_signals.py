"""Feedback a run, the server or a person records outside a PR (shared/feedback.py):
FORCE overrides, edits to an input or to a draft an agent wrote, cancels and
retries from the Studio, and corrections to triaging's classifications.
"""

import dataclasses
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from shared import feedback

ROOT = Path(__file__).resolve().parents[2]


def _session(audit: Path, name: str, **markers) -> Path:
    path = audit / name
    path.mkdir(parents=True)
    for marker, value in markers.items():
        (path / f".{marker.replace('_', '-')}").write_text(f"{value}\n")
    return path


class TestInputEdits:
    @pytest.fixture
    def queue(self, tmp_path):
        folder = tmp_path / "queue" / "user-x"
        (folder / "processed").mkdir(parents=True)
        return folder

    def test_an_input_rewritten_after_a_run_is_recorded_with_how_that_run_ended(
            self, queue, tmp_path):
        (queue / "processed" / "shop.txt").write_text("Module: shop\n1. Pay by card\n")
        (queue / "shop.txt").write_text("Module: shop\n1. Pay by card on the cart page\n")
        audit = tmp_path / "audit"
        _session(audit, "20261001-100000-create-shop", fix_passed="false", verdict="NEEDS-REVIEW")
        _session(audit, "20260901-100000-create-shop", fix_passed="true")
        _session(audit, "20261001-110000-create-other", fix_passed="true")

        event = feedback.note_input("test-authoring-agent", "20261002-090000-create-shop",
                                    queue / "shop.txt", audit)
        assert event["kind"] == "input_edit" and event["explicit"] is False
        assert event["artifact"]["previous_session"] == "20261001-100000-create-shop"
        assert event["artifact"]["previous_ended"] == {"fix-passed": "false",
                                                       "verdict": "NEEDS-REVIEW"}
        assert "+1. Pay by card on the cart page" in event["diff"]
        # The same edit seen by a second run is the same event.
        assert feedback.note_input("test-authoring-agent", "20261003-090000-create-shop",
                                   queue / "shop.txt", audit) is None
        assert len(feedback.read()) == 1

    def test_an_unchanged_or_first_input_records_nothing(self, queue, tmp_path):
        (queue / "shop.txt").write_text("same\n")
        assert feedback.note_input("test-authoring-agent", "s", queue / "shop.txt", tmp_path) is None
        (queue / "processed" / "shop.txt").write_text("same\n")
        assert feedback.note_input("test-authoring-agent", "s", queue / "shop.txt", tmp_path) is None
        assert feedback.read() == []

    def test_a_missing_input_never_raises(self, tmp_path):
        assert feedback.note_input("test-authoring-agent", "s", tmp_path / "gone.txt", tmp_path) is None


class TestDraftEdits:
    def test_a_person_s_edit_to_healing_s_draft_is_attributed_to_healing(self, tmp_path):
        draft = "# DRAFT\nModule: shop\nWhat changed:\n1. <describe what the page looks like now>\n"
        feedback.record_quietly([feedback.draft("shop-rebuilt.txt", draft,
                                                "20261001-100000-fix-CartTest", "CartTest.pay")])
        note = tmp_path / "shop-rebuilt.txt"
        note.write_text("Module: shop\nWhat changed:\n1. The cart is now a side panel\n")
        event = feedback.note_input("test-adaptation-agent", "20261002-100000-adapt-shop-rebuilt",
                                    note, tmp_path)
        assert event["kind"] == "draft_edit"
        assert event["agent"] == "test-healing-agent"
        assert event["session"] == "20261001-100000-fix-CartTest"
        assert event["artifact"]["used_in"] == "20261002-100000-adapt-shop-rebuilt"
        assert "+1. The cart is now a side panel" in event["diff"]

    def test_a_draft_run_as_written_records_nothing(self, tmp_path):
        feedback.record_quietly([feedback.draft("shop-rebuilt.txt", "as drafted\n", "h")])
        note = tmp_path / "shop-rebuilt.txt"
        note.write_text("as drafted\n")
        assert feedback.note_input("test-adaptation-agent", "a", note, tmp_path) is None


class TestOverrides:
    def test_an_override_is_explicit_and_says_how_it_ended(self):
        event = feedback.override("test-healing-agent", "s1", "WRONG_PAGE", "verified",
                                  test="CartTest.pay", detail="FORCE=true")
        assert event["kind"] == "override" and event["explicit"] is True
        assert event["artifact"] == {"test": "CartTest.pay", "overridden": "WRONG_PAGE",
                                     "outcome": "verified"}

    def test_recording_never_raises_inside_a_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FEEDBACK_FILE", str(tmp_path))      # a directory: unwritable
        assert feedback.record_quietly([feedback.override("a", "s", "x", "y")]) == 0

    def test_the_healing_fix_step_records_a_forced_fix_and_nothing_else(self):
        path = ROOT / "agents" / "test-healing-agent" / "actions" / "01_fix.py"
        spec = importlib.util.spec_from_file_location("healing_fix_feedback", path)
        fix = importlib.util.module_from_spec(spec)
        sys.modules["healing_fix_feedback"] = fix
        with mock.patch.dict(os.environ, {"AUDIT_DIR": tempfile.mkdtemp(prefix="fix-fb-"),
                                          "HANDOFF_FILE": str(ROOT / "tests" / "none.json")}):
            spec.loader.exec_module(fix)
        fix._record_override({"test_name": "CartTest.pay"}, "verified")
        assert feedback.read() == []
        fix._record_override({"test_name": "CartTest.pay", "_forced_over_verdict": "WRONG_PAGE"},
                             "failed")
        (event,) = feedback.read()
        assert event["artifact"]["overridden"] == "WRONG_PAGE"
        assert event["artifact"]["outcome"] == "failed"


class TestEnded:
    def test_reads_the_markers_a_run_left(self, tmp_path):
        session = _session(tmp_path, "s", fix_passed="stuck", skip_reason="stuck", cancelled="true")
        assert feedback.ended(session) == {"fix-passed": "stuck", "skip-reason": "stuck",
                                           "cancelled": "true"}
        assert feedback.ended(tmp_path / "missing") == {}


# ── Cancels and retries from the Studio ───────────────────────────────────────

@pytest.fixture
def server(tmp_path, monkeypatch):
    from flask import Flask
    from qa_agents_server import agents as agents_mod, routes, runner

    spec = dataclasses.replace(agents_mod.AGENTS["test-authoring-agent"],
                               audit_dir=tmp_path / "audit")
    monkeypatch.setitem(agents_mod.AGENTS, "test-authoring-agent", spec)
    monkeypatch.setattr(routes, "_owns", lambda session_id: True)
    app = Flask(__name__)
    app.register_blueprint(routes.qa_bp)
    return SimpleNamespace(client=app.test_client(), runner=runner, audit=tmp_path / "audit",
                           headers={"X-User-ID": "aaaaaaaaaaaa"})


def test_a_cancel_records_the_step_it_stopped(server, monkeypatch):
    run = SimpleNamespace(agent="test-authoring-agent", module="shop", started_at=0.0,
                          step_progress={"parse": "done", "validate_web": "running"})
    monkeypatch.setattr(server.runner, "get_run", lambda sid: run)
    monkeypatch.setattr(server.runner, "cancel_run", lambda sid: True)
    response = server.client.post("/agents/test-authoring-agent/run/s1/cancel",
                                  headers=server.headers)
    assert response.status_code == 200
    (event,) = feedback.read()
    assert (event["kind"], event["source"], event["explicit"]) == ("cancel", "studio", False)
    assert event["artifact"]["step"] == "validate_web" and event["who"]


def test_a_cancel_of_a_finished_run_records_nothing(server, monkeypatch):
    monkeypatch.setattr(server.runner, "get_run", lambda sid: None)
    monkeypatch.setattr(server.runner, "cancel_run", lambda sid: False)
    assert server.client.post("/agents/test-authoring-agent/run/s1/cancel",
                              headers=server.headers).status_code == 404
    assert feedback.read() == []


def test_a_retry_records_the_step_and_how_the_run_had_ended(server, monkeypatch):
    _session(server.audit, "s1", fix_passed="false")
    started = SimpleNamespace(session_id="s1", module="shop", auto_push=False, base_branch="",
                              status="running", started_at=1.0)
    monkeypatch.setattr(server.runner, "start_run", lambda *a, **k: started)
    response = server.client.post("/agents/test-authoring-agent/sessions/s1/retry",
                                  json={"from_step": 4, "auto_push": False},
                                  headers=server.headers)
    assert response.status_code == 201
    (event,) = feedback.read()
    assert event["kind"] == "retry" and event["artifact"]["from_step"] == 4
    assert event["artifact"]["previous_ended"] == {"fix-passed": "false"}


def test_a_retry_the_server_refuses_records_nothing(server, monkeypatch):
    def refuse(*a, **k):
        raise server.runner.RunnerError("no such session", 404)
    monkeypatch.setattr(server.runner, "start_run", refuse)
    response = server.client.post("/agents/test-authoring-agent/sessions/s1/retry",
                                  json={"from_step": 4, "auto_push": False},
                                  headers=server.headers)
    assert response.status_code == 404
    assert feedback.read() == []


# ── Corrections to triaging ───────────────────────────────────────────────────

def _load_reclassify():
    spec = importlib.util.spec_from_file_location("reclassify", ROOT / "scripts" / "reclassify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reclassify = _load_reclassify()


@pytest.fixture
def triage_audit(tmp_path):
    session = tmp_path / "20261001-100000-Shop-Nightly-12"
    session.mkdir(parents=True)
    (session / "03-classify.json").write_text(json.dumps({"classifications": [
        {"test_name": "CartTest.checkout", "classification": "AUTOMATION_ISSUE",
         "confidence": "HIGH", "root_cause_category": "ELEMENT_NOT_FOUND"}]}))
    return tmp_path


class TestReclassify:
    @pytest.mark.parametrize("asked", ["CartTest#checkout", "CartTest.checkout",
                                       "automation.shop.CartTest.checkout"])
    def test_finds_the_agent_s_classification_however_the_test_is_named(self, triage_audit, asked):
        session, entry = reclassify.find("Shop-Nightly-12", asked, triage_audit)
        assert session == "20261001-100000-Shop-Nightly-12"
        assert entry["classification"] == "AUTOMATION_ISSUE"

    def test_records_the_correction_beside_what_the_agent_said(self, triage_audit):
        reclassify.main(["--build-tag", "Shop-Nightly-12", "--test", "CartTest#checkout",
                         "--as", "PRODUCT_BUG", "--category", "assertion_failure",
                         "--note", "the total is wrong"], audit=triage_audit)
        (event,) = feedback.read()
        assert event["kind"] == "reclassification" and event["explicit"] is True
        assert event["artifact"]["agent_said"]["classification"] == "AUTOMATION_ISSUE"
        assert event["artifact"]["person_said"] == {"classification": "PRODUCT_BUG",
                                                    "category": "ASSERTION_FAILURE"}
        assert event["artifact"]["agrees"] is False and event["text"] == "the total is wrong"

    def test_a_confirmation_is_recorded_as_agreement(self, triage_audit):
        reclassify.main(["--build-tag", "Shop-Nightly-12", "--test", "CartTest.checkout",
                         "--as", "AUTOMATION_ISSUE"], audit=triage_audit)
        assert feedback.read()[0]["artifact"]["agrees"] is True

    def test_a_build_not_on_this_machine_is_still_recorded(self, tmp_path, capsys):
        reclassify.main(["--build-tag", "Elsewhere-1", "--test", "CartTest.checkout",
                         "--as", "PRODUCT_BUG"], audit=tmp_path)
        (event,) = feedback.read()
        assert event["session"] == "" and event["artifact"]["agent_said"]["classification"] == ""
        assert "no triaging session" in capsys.readouterr().out
