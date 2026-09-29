"""Unit tests for shared/proven_locators.py and run.sh's use of it.

Step 02 is the only step that reads what a passing test proved, and a step-cache hit
skips step 02. So a cached step 02 that a later passing run corrected must not be
restored, or every run hands step 03 the selector step 04 already had to replace.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import proven_locators  # noqa: E402

RUN_SH = ROOT / "agents" / "test-authoring-agent" / "run.sh"

PLAN = {"web_pages": [
    {"class_name": "CartPage", "locators_needed": ["buyButton", "amountField", "total"]},
    {"class_name": "ResultPage", "locators_needed": ["total", "message"]},
]}


def _cache(tmp_path, cached: dict, proven: dict, proven_newer=True) -> Path:
    """A cache folder holding a step 02 map and, unless `proven` is None, a proven
    file written after it (or before it, with proven_newer=False)."""
    d = tmp_path / "cache" / "cli" / "shop"
    d.mkdir(parents=True)
    web = d / proven_locators.STEP_02
    web.write_text(json.dumps({"status": "ok", "selectors": cached}))
    os.utime(web, (1000, 1000))
    if proven is not None:
        p = d / proven_locators.FILE
        p.write_text(json.dumps({"status": "ok", "locators": [
            {"name": n, "selector": s} for n, s in proven.items()]}))
        t = 2000 if proven_newer else 500
        os.utime(p, (t, t))
    return d


class TestSuperseded:
    def test_no_proven_file_keeps_the_cache(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy"}, None)
        assert proven_locators.superseded(d, PLAN) == []

    def test_agreeing_proof_keeps_the_cache(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy", "amountField": "#amt"},
                   {"buyButton": "a.buy", "amountField": "#amt"})
        assert proven_locators.superseded(d, PLAN) == []

    def test_a_replaced_selector_supersedes_it(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "button.buy", "amountField": "#amt"},
                   {"buyButton": "a.buy", "amountField": "#amt"})
        assert proven_locators.superseded(d, PLAN) == ["buyButton"]

    def test_a_locator_step_02_never_confirmed_supersedes_it(self, tmp_path):
        d = _cache(tmp_path, {"amountField": "#amt"},
                   {"buyButton": "a.buy", "amountField": "#amt"})
        assert proven_locators.superseded(d, PLAN) == ["buyButton"]

    def test_a_proof_older_than_the_cached_step_02_is_already_in_it(self, tmp_path):
        """Step 02 re-ran with the proof as a seed and was cached again: what it
        reported stands, or a name it cannot confirm would re-run it every time."""
        d = _cache(tmp_path, {"amountField": "#amt"},
                   {"buyButton": "a.buy"}, proven_newer=False)
        assert proven_locators.superseded(d, PLAN) == []

    def test_a_name_the_plan_never_asks_step_02_for_is_ignored(self, tmp_path):
        d = _cache(tmp_path, {"amountField": "#amt"}, {"helperOnlyField": "#x"})
        assert proven_locators.superseded(d, PLAN) == []

    def test_a_proof_step_02_was_already_seeded_with_keeps_the_cache(self, tmp_path):
        """Seeded with it, step 02 reported another selector, or none. Every passing
        run writes its proof again, so counting it would re-run step 02 every time."""
        d = _cache(tmp_path, {"amountField": "div.cart input.amt"},
                   {"amountField": "input.amt", "buyButton": "a.buy"})
        (d / proven_locators.SEEDS).write_text(json.dumps([
            {"owner": "known", "name": "amountField", "selector": "input.amt", "proven": True},
            {"owner": "known", "name": "buyButton", "selector": "a.buy", "proven": True}]))
        assert proven_locators.superseded(d, PLAN) == []

    def test_a_selector_seeded_under_another_name_covers_the_proof(self, tmp_path):
        """One seed per selector: two pages' amount sharing one is listed once."""
        d = _cache(tmp_path, {"CartPage.total": ".total"},
                   {"CartPage.total": ".total", "ResultPage.total": ".total"})
        (d / proven_locators.SEEDS).write_text(json.dumps([
            {"owner": "known", "name": "CartPage.total", "selector": ".total"}]))
        assert proven_locators.superseded(d, PLAN) == []

    def test_a_seed_with_another_selector_does_not_cover_the_proof(self, tmp_path):
        d = _cache(tmp_path, {"amountField": "#amt"}, {"buyButton": "a.buy"})
        (d / proven_locators.SEEDS).write_text(json.dumps([
            {"owner": "known", "name": "buyButton", "selector": "button.buy"}]))
        assert proven_locators.superseded(d, PLAN) == ["buyButton"]

    def test_page_qualified_names_are_compared_as_step_02_names_them(self, tmp_path):
        d = _cache(tmp_path, {"CartPage.total": ".cart-total", "ResultPage.total": ".old"},
                   {"CartPage.total": ".cart-total", "ResultPage.total": ".headline"})
        assert proven_locators.superseded(d, PLAN) == ["ResultPage.total"]


def _failed(cache_dir: Path, failed: dict, newer=True) -> None:
    """A failed-locators file written after the cached step 02 (or before it)."""
    f = cache_dir / proven_locators.FAILED_FILE
    f.write_text(json.dumps({"status": "failed", "locators": [
        {"name": n, "selector": s, "element": n} for n, s in failed.items()]}))
    t = 2000 if newer else 500
    os.utime(f, (t, t))


class TestFailed:
    """A selector a test failed on, still handed out by the cached step 02."""

    def test_a_selector_a_test_failed_on_reruns_step_02(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy", "amountField": "#amt"}, None)
        _failed(d, {"buyButton": "a.buy"})
        assert proven_locators.failed(d, PLAN) == ["buyButton"]

    def test_a_failure_older_than_the_cached_step_02_is_already_answered(self, tmp_path):
        """Step 02 ran again after it: what that run reported stands."""
        d = _cache(tmp_path, {"buyButton": "a.buy"}, None)
        _failed(d, {"buyButton": "a.buy"}, newer=False)
        assert proven_locators.failed(d, PLAN) == []

    def test_a_selector_the_cache_no_longer_hands_out_is_ignored(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy-now"}, None)
        _failed(d, {"buyButton": "a.buy"})
        assert proven_locators.failed(d, PLAN) == []


class TestCli:
    def _run(self, *args):
        return subprocess.run([sys.executable, "-m", "shared.proven_locators", *args],
                              cwd=ROOT, capture_output=True, text=True)

    def _plan(self, tmp_path) -> Path:
        path = tmp_path / "01-parse.json"
        path.write_text(json.dumps(PLAN))
        return path

    def test_exit_0_when_the_cache_agrees(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy"}, {"buyButton": "a.buy"})
        result = self._run("current", str(d), str(self._plan(tmp_path)))
        assert (result.returncode, result.stdout) == (0, "")

    def test_exit_1_names_what_was_superseded(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "button.buy"},
                   {"buyButton": "a.buy", "message": ".msg"})
        result = self._run("current", str(d), str(self._plan(tmp_path)))
        assert result.returncode == 1
        assert result.stdout.strip() == "a passing run has since proved buyButton, message"

    def test_both_reasons_are_given(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy", "amountField": "#amt"},
                   {"message": ".msg"})
        _failed(d, {"amountField": "#amt"})
        result = self._run("current", str(d), str(self._plan(tmp_path)))
        assert result.stdout.strip() == ("a passing run has since proved message; "
                                         "a test failed on its amountField")

    def test_a_check_that_cannot_be_made_does_not_keep_the_cache(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy"}, {"buyButton": "a.buy"})
        result = self._run("current", str(d), str(tmp_path / "missing-plan.json"))
        assert result.returncode != 0


class TestRunShGate:
    """The `_proven_agrees` function in run.sh itself, extracted and run in bash."""

    def _gate(self, tmp_path, cache_dir: Path) -> subprocess.CompletedProcess:
        audit = tmp_path / "audit"
        audit.mkdir()
        (audit / "01-parse.json").write_text(json.dumps(PLAN))
        script = (
            'log() { echo "$*"; }\nflush_step_done() { :; }\n'
            f'eval "$(sed -n \'/^_proven_agrees() {{/,/^}}/p\' "{RUN_SH}")"\n'
            'if _proven_agrees; then echo RESTORE; else echo RERUN; fi\n'
        )
        env = {"PATH": os.environ["PATH"], "REPO_ROOT": str(ROOT),
               "CACHE_DIR": str(cache_dir), "AUDIT_DIR": str(audit)}
        return subprocess.run(["bash", "-euo", "pipefail", "-c",
                               'CACHE_DIR="$CACHE_DIR" AUDIT_DIR="$AUDIT_DIR"\n' + script],
                              capture_output=True, text=True, env=env)

    def test_an_agreeing_cache_is_restored(self, tmp_path):
        d = _cache(tmp_path, {"buyButton": "a.buy"}, {"buyButton": "a.buy"})
        out = self._gate(tmp_path, d).stdout
        assert out.strip().splitlines()[-1] == "RESTORE"

    def test_a_superseded_cache_reruns_step_02_and_says_why(self, tmp_path):
        d = _cache(tmp_path, {"amountField": "#amt"}, {"buyButton": "a.buy"})
        out = self._gate(tmp_path, d).stdout
        assert out.strip().splitlines()[-1] == "RERUN"
        assert "a passing run has since proved buyButton" in out
        # Step 02's known_selectors skips sessions whose log says step 02 was restored.
        assert "restored 02-validate-web.json" not in out
