"""The step cache helpers in the authoring and adaptation run.sh, run in bash.

Each test extracts the real functions from run.sh and runs them against temporary
audit and cache folders, with shared/session.sh sourced for log() and record_stage.
"""

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHORING = REPO_ROOT / "agents" / "test-authoring-agent" / "run.sh"
ADAPTATION = REPO_ROOT / "agents" / "test-adaptation-agent" / "run.sh"


def _bash(tmp_path, run_sh: Path, functions, body: str) -> str:
    audit, cache = tmp_path / "audit", tmp_path / "cache"
    audit.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    (tmp_path / "input.txt").write_text("Module: shop\n")
    extract = "".join(f'eval "$(sed -n \'/^{fn}() {{/,/^}}/p\' "{run_sh}")"\n'
                      for fn in functions)
    script = (f'set -euo pipefail\nsource "{REPO_ROOT}/shared/session.sh"\n'
              'declare -a STEP_NAMES=() STEP_DURATIONS=()\n'
              f'{extract}{body}\nflush_step_done\n')
    env = {"PATH": os.environ["PATH"], "REPO_ROOT": str(REPO_ROOT),
           "AUDIT_DIR": str(audit), "CACHE_DIR": str(cache), "CACHE_STEPS": "true",
           "INPUT_FILE": str(tmp_path / "input.txt"), "START_FROM_STEP": "1"}
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                            env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestAuthoringSave:
    def test_a_step_is_cached_with_its_report_and_companion(self, tmp_path):
        audit = tmp_path / "audit"
        audit.mkdir()
        (audit / "02-validate-web.json").write_text(json.dumps({"status": "ok"}))
        (audit / "02-validate-web.md").write_text("# report")
        (audit / "02-known-selectors.json").write_text("[]")
        _bash(tmp_path, AUTHORING, ["_cache_save"],
              '_cache_save "02-validate-web.json" "02-known-selectors.json"')
        cached = sorted(p.name for p in (tmp_path / "cache").iterdir())
        assert cached == ["02-known-selectors.json", "02-validate-web.json",
                          "02-validate-web.json.input", "02-validate-web.md"]

    def test_a_companion_the_new_run_lacks_is_not_left_behind(self, tmp_path):
        """Seeds from an older step 02 must not be read as this one's."""
        (tmp_path / "audit").mkdir()
        (tmp_path / "cache").mkdir()
        (tmp_path / "audit" / "02-validate-web.json").write_text(json.dumps({"status": "ok"}))
        (tmp_path / "cache" / "02-known-selectors.json").write_text('[{"name": "old"}]')
        _bash(tmp_path, AUTHORING, ["_cache_save"],
              '_cache_save "02-validate-web.json" "02-known-selectors.json"')
        assert not (tmp_path / "cache" / "02-known-selectors.json").exists()

    def test_a_skipped_step_is_not_cached(self, tmp_path):
        (tmp_path / "audit").mkdir()
        (tmp_path / "audit" / "01-parse.json").write_text(json.dumps({"status": "skipped"}))
        _bash(tmp_path, AUTHORING, ["_cache_save"], '_cache_save "01-parse.json"')
        assert list((tmp_path / "cache").iterdir()) == []


class TestRestore:
    def test_the_report_comes_back_with_the_step(self, tmp_path):
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "01-parse.json").write_text("{}")
        (tmp_path / "cache" / "01-parse.md").write_text("# plan")
        _bash(tmp_path, AUTHORING, ["_cache_restore"], '_cache_restore "01-parse.json"')
        assert (tmp_path / "audit" / "01-parse.md").read_text() == "# plan"

    def test_the_restore_line_follows_the_previous_steps_done_line(self, tmp_path):
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "02-validate-web.json").write_text("{}")
        out = _bash(tmp_path, AUTHORING, ["_cache_restore"],
                    '_STEP_DONE_LINE="✓ [02/05] Validate API — 0s"\n'
                    '_cache_restore "02-validate-web.json"')
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert "✓ [02/05] Validate API" in lines[0]
        assert "Step cache: restored 02-validate-web.json" in lines[1]


class TestTestOutcome:
    """What step 04 learned about step 02's locators, kept in the cache."""

    def _outcome(self, tmp_path, gate, proven=False, failed=False, cached_failed=False):
        audit, cache = tmp_path / "audit", tmp_path / "cache"
        audit.mkdir()
        cache.mkdir()
        (audit / ".fix-passed").write_text(gate)
        if proven:
            (audit / "04-proven-locators.json").write_text('{"locators": []}')
        if failed:
            (audit / "04-failed-locators.json").write_text('{"locators": []}')
        if cached_failed:
            (cache / "04-failed-locators.json").write_text('{"old": true}')
        _bash(tmp_path, AUTHORING, ["_cache_test_outcome"], "_cache_test_outcome")
        return sorted(p.name for p in cache.iterdir())

    def test_a_passing_proof_replaces_a_recorded_failure(self, tmp_path):
        assert self._outcome(tmp_path, "true", proven=True, cached_failed=True) == [
            "04-proven-locators.json"]

    def test_a_failure_is_kept_when_the_run_ended_red(self, tmp_path):
        assert self._outcome(tmp_path, "stuck", failed=True) == ["04-failed-locators.json"]

    def test_a_failure_the_fix_loop_got_past_is_not_kept(self, tmp_path):
        """The initial run failed on it, but the run ended green: its proof says more."""
        assert self._outcome(tmp_path, "true", proven=True, failed=True) == [
            "04-proven-locators.json"]

    def test_a_product_defect_or_an_infra_skip_is_not_a_locator_failure(self, tmp_path):
        assert self._outcome(tmp_path, "defect", failed=True) == []


class TestAdaptationRestoredStep:
    def test_a_restored_step_gets_a_done_line_and_a_skipped_stage(self, tmp_path):
        out = _bash(tmp_path, ADAPTATION, ["_cache_skipped"],
                    '_cache_skipped explore "[03/05] Explore"')
        assert "✓ [03/05] Explore — skipped (step cache hit)" in out
        stage = json.loads((tmp_path / "audit" / "metrics" / "stages.jsonl").read_text())
        assert (stage["key"], stage["label"], stage["skipped"]) == (
            "explore", "[03/05] Explore", True)


class TestAdaptationSandboxState:
    """An exploration made with the sandbox assertion off stops at the first control
    the checker flags. Restored after the assertion is turned on, it reported the
    same refusal and the switch seemed to do nothing."""

    FUNCTIONS = ["_cache_sandbox_ok", "_cache_hit", "_cache_save"]
    HIT = 'if _cache_hit "{name}"; then echo HIT; else echo MISS; fi'

    def _save(self, tmp_path, state):
        (tmp_path / "audit").mkdir(exist_ok=True)
        for name in ("03-explore.json", "01-parse-change.json"):
            (tmp_path / "audit" / name).write_text(json.dumps(
                {"status": "partial", "flow": {"pages": {}}}))
        _bash(tmp_path, ADAPTATION, self.FUNCTIONS,
              f'CACHE_VERSION=2; SANDBOX_STATE={state}\n'
              '_cache_save "03-explore.json"; _cache_save "01-parse-change.json"')

    def _hit(self, tmp_path, state, name="03-explore.json"):
        out = _bash(tmp_path, ADAPTATION, self.FUNCTIONS,
                    f'CACHE_VERSION=2; SANDBOX_STATE={state}\n' + self.HIT.format(name=name))
        return out.strip().splitlines()[-1]

    def test_the_same_state_is_a_hit(self, tmp_path):
        self._save(tmp_path, "off")
        assert self._hit(tmp_path, "off") == "HIT"

    def test_a_different_state_is_a_miss_either_way(self, tmp_path):
        self._save(tmp_path, "off")
        assert self._hit(tmp_path, "on") == "MISS"
        self._save(tmp_path, "on")
        assert self._hit(tmp_path, "off") == "MISS"

    def test_a_cache_from_before_the_state_was_recorded_counts_as_off(self, tmp_path):
        self._save(tmp_path, "off")
        (tmp_path / "cache" / "03-explore.json.sandbox").unlink()
        assert self._hit(tmp_path, "off") == "HIT"
        assert self._hit(tmp_path, "on") == "MISS"

    def test_steps_the_assertion_does_not_touch_stay_cached(self, tmp_path):
        self._save(tmp_path, "off")
        assert self._hit(tmp_path, "on", "01-parse-change.json") == "HIT"
