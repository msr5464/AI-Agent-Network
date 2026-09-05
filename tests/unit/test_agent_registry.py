"""Structural invariants every registered agent must satisfy.

These exist because the failure they catch is silent. A fourth agent registered
without a session parser did not crash — it returned structurally empty session
rows that looked like a run which had done nothing. Each assertion here is a
mistake that was actually made or actively invited by the code as it stood.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from qa_agents_server import audit_reader, runner
from qa_agents_server.agents import AGENTS, AgentConfigError


@pytest.mark.parametrize("name", sorted(AGENTS))
class TestEveryAgent:
    def test_has_a_registered_session_parser(self, name):
        spec = AGENTS[name]
        assert spec.summary_kind in audit_reader._SESSION_PARSERS, (
            f"{name} has summary_kind={spec.summary_kind!r} with no parser. This "
            f"used to be 'authoring if default agent else healing', so a new "
            f"agent silently got another agent's shape and reported empty runs.")

    def test_run_sh_exists_and_is_executable(self, name):
        run_sh = AGENTS[name].run_sh
        assert run_sh.exists(), f"{name}: {run_sh} missing"

    def test_agent_directory_ends_in_agent(self, name):
        # runner._is_our_agent_process matches on "-agent/" to decide whether an
        # orphaned process is ours to reap on boot.
        assert AGENTS[name].run_sh.parent.name.endswith("-agent")

    def test_step_files_match_their_action_filenames(self, name):
        """`04_run_and_fix.py` writes `04-run-and-fix.json`: underscores in the
        action, hyphens in the output. A mismatch means the server polls forever
        for a file nothing ever writes."""
        spec = AGENTS[name]
        actions = spec.run_sh.parent / "actions"
        if not actions.is_dir():
            pytest.skip(f"{name} has no actions directory")
        available = {p.stem for p in actions.glob("*.py")}
        for key, filename, _label in spec.steps:
            stem = filename[:-len(".json")]
            expected = stem.replace("-", "_")
            # A combined/summary file has no action of its own; that is fine as
            # long as *something* in the step's numeric slot exists.
            number = stem.split("-", 1)[0]
            slot = {a for a in available if a.startswith(number + "_")}
            assert expected in available or slot, (
                f"{name} step {key!r} polls for {filename}, but no action in "
                f"{actions} writes it")

    def test_run_sh_honours_a_preset_session_id(self, name):
        """The server pre-computes SESSION_ID/AUDIT_DIR and then watches that
        directory. An agent that generates its own regardless is invisible to it —
        which is precisely why test-triaging-agent is not registered."""
        text = AGENTS[name].run_sh.read_text()
        assert re.search(r'SESSION_ID="\$\{SESSION_ID:-', text), (
            f"{name}/run.sh overwrites SESSION_ID; the server would watch the "
            f"wrong audit directory forever")
        assert re.search(r'AUDIT_DIR="\$\{AUDIT_DIR:-', text)

    def test_queue_kind_is_known(self, name):
        assert AGENTS[name].queue_kind in ("txt", "json")


class TestShipStep:
    def test_runner_reads_the_ship_file_from_the_spec(self):
        """runner._wait_and_reap hardcoded '05-ship.json', so a healing run —
        whose ship step is 02-ship.json — never carried pr_url on the live SSE
        stream. Only the replay path compensated."""
        source = (ROOT / "qa_agents_server" / "runner.py").read_text()
        assert 'run.audit_dir / "05-ship.json"' not in source
        assert "steps[-1][1]" in source

    @pytest.mark.parametrize("name", sorted(AGENTS))
    def test_last_step_is_a_ship_step(self, name):
        assert AGENTS[name].steps[-1][0] == "ship"


class TestNoNameBasedDispatch:
    @pytest.mark.parametrize("module", ["audit_reader", "routes", "runner"])
    def test_capability_flags_replaced_agent_name_comparisons(self, module):
        """`spec.name != DEFAULT_AGENT` meant "is not authoring, so treat it as
        healing". Invisible with two agents, wrong with three."""
        source = (ROOT / "qa_agents_server" / f"{module}.py").read_text()
        # Comments describing the old dispatch are not the old dispatch. Strip
        # them, or this fails on the note explaining why it was removed.
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        offenders = re.findall(r"spec\.name\s*[!=]=\s*DEFAULT_AGENT", code)
        assert not offenders, (
            f"{module}.py still branches on the agent name: {offenders}")


class TestStepProgressIsAlwaysTerminal:
    """A run that stops early must not leave a step chip spinning.

    `_audit_watcher` marks the *next* step "running" as soon as the previous one
    lands — right while a run is in flight, wrong once it has ended. A run that
    stops early (EXPLORE_ONLY, an escalation, a skip) left that optimistic chip
    running forever in the live UI, while the replayed stream showed only the
    steps that really ran. The two views disagreed about the same run.
    """

    def test_the_terminal_sweep_resolves_unfinished_steps(self):
        source = (ROOT / "qa_agents_server" / "runner.py").read_text()
        assert 'run.step_progress[_key] = "skipped"' in source, (
            "the sweep before the terminal event must give every step a final "
            "state, not just the ones that produced a file")

    def test_skipped_is_distinct_from_failed(self):
        """A step that never ran is not a step that went wrong."""
        from qa_agents_server.audit_reader import _step_has_error
        assert _step_has_error({"status": "skipped"}) is False
        assert _step_has_error({"status": "no_session"}) is True
        assert _step_has_error({"status": "unsafe"}) is True
        assert _step_has_error({"status": "partial"}) is False, (
            "a flow map that recorded most of a journey is a usable result, "
            "not a failed step")


# ── The optional per-run base branch ──────────────────────────────────────────
#
# One property carries this feature, and it is the one that was got wrong for
# AUTO_PUSH: a field the caller did not fill in must not be exported. Because
# shared/load_env.sh lets caller-exported vars beat config/.env, exporting a
# blank-or-defaulted value silently overrides the admin's setting for every run.

@pytest.mark.parametrize("name", sorted(AGENTS))
class TestBaseBranchEnv:
    @staticmethod
    def _payload(name, **extra):
        """The minimum each agent's build_env accepts, plus whatever is under test."""
        base = {"test-healing-agent": {"test": "LoginTest#testLogin"}}.get(
            name, {"module": "checkout"})
        return {**base, **extra}

    @pytest.mark.parametrize("absent", [{}, {"base_branch": None},
                                        {"base_branch": ""}, {"base_branch": "   "}])
    def test_an_unfilled_field_exports_nothing(self, name, absent):
        env = AGENTS[name].build_env(self._payload(name, **absent))
        assert "GITHUB_DEFAULT_BRANCH" not in env, (
            f"{name} exported a base branch for {absent!r}. A caller-exported var "
            f"beats config/.env, so this is the AUTO_PUSH bug again: an untouched "
            f"field would override the branch an admin configured.")

    def test_a_named_branch_overrides_the_configured_default(self, name):
        env = AGENTS[name].build_env(self._payload(name, base_branch="release/2.3"))
        assert env["GITHUB_DEFAULT_BRANCH"] == "release/2.3"

    def test_the_name_is_trimmed(self, name):
        env = AGENTS[name].build_env(self._payload(name, base_branch=" release/2.3 "))
        assert env["GITHUB_DEFAULT_BRANCH"] == "release/2.3"

    def test_a_flag_shaped_name_is_refused_with_a_400(self, name):
        """The value reaches git and `gh pr create --base` as an argv element."""
        with pytest.raises(AgentConfigError) as raised:
            AGENTS[name].build_env(self._payload(name, base_branch="--upload-pack=x"))
        assert raised.value.status == 400
        assert "base_branch" in raised.value.message

    def test_the_override_reuses_the_var_the_pr_base_is_read_from(self, name):
        """Not a stylistic choice. All three ship steps pass
        base=GITHUB_DEFAULT_BRANCH to `gh pr create`, so overriding that one
        variable is what keeps the branch point and the PR base identical. A
        separate BASE_BRANCH would let them diverge, and a PR opened against the
        wrong base carries the whole delta between two branches."""
        env = AGENTS[name].build_env(self._payload(name, base_branch="release/2.3"))
        assert "BASE_BRANCH" not in env
        assert env.get("GITHUB_DEFAULT_BRANCH") == "release/2.3"
