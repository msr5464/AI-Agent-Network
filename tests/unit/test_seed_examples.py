"""Tests for qa_agents_server/seed_examples.py.

The rules that matter are the ones that stop a restart from resurrecting work:
never overwrite, never re-create something already processed, never seed twice.
"""

import os
from dataclasses import replace

import pytest

from qa_agents_server import seed_examples
from qa_agents_server.agents import AGENTS


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A txt-queue agent whose queue and examples live under tmp_path."""
    examples = tmp_path / "examples" / "test-authoring-agent"
    examples.mkdir(parents=True)
    (examples / "demo.txt").write_text("Module: demo\nType: web\n\nSteps:\n1. Go\n")
    (examples / "other.txt").write_text("Module: other\nType: api\n\nSteps:\n1. Get\n")
    monkeypatch.setattr(seed_examples, "EXAMPLES_DIR", tmp_path / "examples")
    return replace(AGENTS["test-authoring-agent"], queue_dir=tmp_path / "queue")


class TestSeedAgent:
    def test_copies_examples_into_an_empty_queue(self, agent):
        assert sorted(seed_examples.seed_agent(agent)) == ["demo.txt", "other.txt"]
        assert (agent.queue_dir / "demo.txt").read_text().startswith("Module: demo")

    def test_is_a_no_op_on_the_second_boot(self, agent):
        seed_examples.seed_agent(agent)
        assert seed_examples.seed_agent(agent) == []

    def test_a_deleted_example_stays_deleted(self, agent):
        seed_examples.seed_agent(agent)
        (agent.queue_dir / "demo.txt").unlink()
        assert seed_examples.seed_agent(agent) == []
        assert not (agent.queue_dir / "demo.txt").exists()

    def test_never_overwrites_a_queued_file(self, agent):
        agent.queue_dir.mkdir(parents=True)
        (agent.queue_dir / "demo.txt").write_text("my own edits")
        copied = seed_examples.seed_agent(agent)
        assert copied == ["other.txt"]
        assert (agent.queue_dir / "demo.txt").read_text() == "my own edits"

    def test_never_recreates_something_already_processed(self, agent):
        processed = agent.queue_dir / "processed"
        processed.mkdir(parents=True)
        (processed / "demo.txt").write_text("already run")
        assert seed_examples.seed_agent(agent) == ["other.txt"]
        assert not (agent.queue_dir / "demo.txt").exists()

    def test_leaves_no_partial_files_behind(self, agent):
        seed_examples.seed_agent(agent)
        assert not list(agent.queue_dir.glob("*.seeding"))

    def test_marker_is_not_listed_as_a_queue_item(self, agent):
        seed_examples.seed_agent(agent)
        assert seed_examples.MARKER_NAME not in {
            p.name for p in agent.queue_dir.glob("*.txt")}

    def test_a_json_queue_takes_json_examples_only(self, tmp_path, monkeypatch):
        examples = tmp_path / "examples" / "test-healing-agent"
        examples.mkdir(parents=True)
        (examples / "Build-1.json").write_text('{"build_tag": "Build-1"}')
        (examples / "notes.txt").write_text("ignored")
        monkeypatch.setattr(seed_examples, "EXAMPLES_DIR", tmp_path / "examples")
        spec = replace(AGENTS["test-healing-agent"], queue_dir=tmp_path / "q")
        assert seed_examples.seed_agent(spec) == ["Build-1.json"]

    def test_an_agent_with_no_examples_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(seed_examples, "EXAMPLES_DIR", tmp_path / "nothing")
        spec = replace(AGENTS["test-authoring-agent"], queue_dir=tmp_path / "q")
        assert seed_examples.seed_agent(spec) == []
        assert not (tmp_path / "q").exists()


class TestSeedAll:
    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("QA_SEED_EXAMPLES", "false")
        assert seed_examples.seed_all(log=lambda _m: None) == {}

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE", " off "])
    def test_falsey_values_all_disable(self, monkeypatch, value):
        monkeypatch.setenv("QA_SEED_EXAMPLES", value)
        assert seed_examples.enabled() is False

    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("QA_SEED_EXAMPLES", raising=False)
        assert seed_examples.enabled() is True

    def test_a_failing_agent_does_not_stop_the_others(self, tmp_path, monkeypatch):
        monkeypatch.delenv("QA_SEED_EXAMPLES", raising=False)
        monkeypatch.setattr(seed_examples, "EXAMPLES_DIR", tmp_path / "examples")
        calls = []

        def boom(spec):
            calls.append(spec.name)
            if len(calls) == 1:
                raise OSError("read-only filesystem")
            return ["x.txt"]

        monkeypatch.setattr(seed_examples, "seed_agent", boom)
        logged = []
        result = seed_examples.seed_all(log=logged.append)
        assert len(calls) == len(AGENTS)
        assert result                      # the later agents still seeded
        assert any("read-only" in m for m in logged)


class TestRealExamples:
    """The shipped examples must actually be seedable by the shipped registry."""

    def test_every_agent_with_examples_seeds_them(self, tmp_path):
        for name, spec in AGENTS.items():
            source = seed_examples.EXAMPLES_DIR / name
            if not source.is_dir():
                continue
            suffix = ".txt" if spec.queue_kind == "txt" else ".json"
            expected = sorted(p.name for p in source.glob(f"*{suffix}"))
            copied = seed_examples.seed_agent(
                replace(spec, queue_dir=tmp_path / name))
            assert copied == expected, name
            assert expected, f"{name} has an examples dir but no {suffix} files"
