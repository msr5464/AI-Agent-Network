"""Tests for shared/assertion_graph.py.

Once an agent may add and remove steps, "the test went green" stops being
evidence of anything — the cheapest way to make an assertion pass is to stop
running it. These cases are the ways that happens in practice, and each one is
invisible to a diff of the test file alone.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import assertion_graph as ag
from shared import blast_radius as br


def _write(root: Path, package: str, name: str, body: str):
    parts = package.split(".")
    path = root / "src" / "main" / "java" / Path(*parts) / f"{name}.java"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"package {package};\n\npublic class {name} {{\n{body}\n}}\n")
    return path


def _index(tmp_path):
    br._cache.clear()
    return ag.member_index(str(tmp_path))


TEST_BODY = """\
    CheckoutHelper helper;

    public void placeOrder() {
        logStep(testConfig, "Place an order and verify the confirmation number");
        helper.completeCheckout();
    }
"""

HELPER_STRONG = """\
    ConfirmPage confirm;

    public void completeCheckout() {
        confirm.verifyTotal();
    }
"""

PAGE_STRONG = """\
    public void verifyTotal() {
        AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
    }
"""


def _repo(tmp_path, page_body=PAGE_STRONG, helper_body=HELPER_STRONG):
    _write(tmp_path, "automation.checkout", "CheckoutTest", TEST_BODY)
    _write(tmp_path, "automation.checkout", "CheckoutHelper", helper_body)
    _write(tmp_path, "automation.checkout", "ConfirmPage", page_body)
    return _index(tmp_path)


class TestReachability:
    def test_assertion_two_hops_down_is_found(self, tmp_path):
        index = _repo(tmp_path)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        sites = {i["site"] for i in fps["asserts"].values()}
        assert "ConfirmPage#verifyTotal" in sites, (
            "the assertion lives in a page object the test never names; "
            "comparing the test file's own diff would prove nothing")
        # Both the project wrapper (confirm.verifyTotal()) and the
        # AssertHelper call inside it are recorded. Redundant on purpose:
        # each is independently a check, and removing either has to be caught.
        callees = {i["callee"] for i in fps["asserts"].values()}
        assert {"verifyTotal", "AssertHelper.assertEquals"} <= callees

    def test_log_steps_are_collected_for_the_intent_contract(self, tmp_path):
        index = _repo(tmp_path)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        assert fps["log_steps"] == [
            "Place an order and verify the confirmation number"]

    def test_brace_inside_a_selector_string_does_not_tear_a_member(self, tmp_path):
        page = '''\
    public void verifyTotal() {
        String sel = "div[data-x='}'] .total";
        AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
    }
'''
        index = _repo(tmp_path, page_body=page)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        found = [i for i in fps["asserts"].values()
                 if i["callee"] == "AssertHelper.assertEquals"]
        assert found, (
            "a } inside a CSS selector string is common in this codebase; a "
            "regex splitter truncates the method and reports a phantom loss")
        assert '"42.00"' in found[0]["literals"], (
            "the expected value must survive into the fingerprint, or a "
            "weakened assertion is indistinguishable from an intact one")


class TestConservation:
    def test_unchanged_code_conserves(self, tmp_path):
        index = _repo(tmp_path)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        report = ag.conserved(fps, fps)
        assert report["ok"] is True
        assert "OK" in ag.describe(report)

    def test_assertion_deleted_two_hops_down_is_caught(self, tmp_path, tmp_path_factory):
        before = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        after_root = tmp_path_factory.mktemp("after")
        index = _repo(after_root, page_body="    public void verifyTotal() {\n    }\n")
        after = ag.fingerprints("CheckoutTest", "placeOrder", index)
        report = ag.conserved(before, after)
        assert report["ok"] is False
        assert report["lost"], "a deleted assertion must be named, not merely counted"
        assert "42.00" in report["lost"][0]

    def test_weakened_assertion_is_caught(self, tmp_path, tmp_path_factory):
        before = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        weaker = '''\
    public void verifyTotal() {
        AssertHelper.assertNotNull(testConfig, "Order total", total);
    }
'''
        after_root = tmp_path_factory.mktemp("weak")
        after = ag.fingerprints("CheckoutTest", "placeOrder",
                                _repo(after_root, page_body=weaker))
        report = ag.conserved(before, after)
        assert report["ok"] is False
        assert report["weakened"], (
            "assertEquals -> assertNotNull still asserts something, which is "
            "exactly why a call-site count would wave it through")

    def test_assertion_made_conditional_is_caught(self, tmp_path, tmp_path_factory):
        before = ag.fingerprints("CheckoutTest", "placeOrder", _repo(tmp_path))
        guarded = '''\
    public void verifyTotal() {
        if (Element.isElementDisplayed(testConfig, total)) {
            AssertHelper.assertEquals(testConfig, "Order total", total, "42.00");
        }
    }
'''
        after_root = tmp_path_factory.mktemp("cond")
        after = ag.fingerprints("CheckoutTest", "placeOrder",
                                _repo(after_root, page_body=guarded))
        report = ag.conserved(before, after)
        assert report["ok"] is False
        assert report["conditionalised"], (
            "an assertion that only runs when it would pass is a deleted "
            "assertion wearing a disguise")


class TestHoles:
    def test_unresolvable_receiver_is_reported_not_dropped(self, tmp_path):
        helper = '''\
    public void completeCheckout() {
        somethingUnknown.doTheThing();
    }
'''
        index = _repo(tmp_path, helper_body=helper)
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        assert any("somethingUnknown" in u for u in fps["unresolved"]), (
            "a call we could not follow is a hole in the guarantee; ignoring it "
            "turns 'no assertion was lost' into 'none that I looked at'")

    def test_new_holes_downgrade_to_plausible_rather_than_failing(self, tmp_path):
        index = _repo(tmp_path)
        before = ag.fingerprints("CheckoutTest", "placeOrder", index)
        after = {"asserts": dict(before["asserts"]),
                 "unresolved": ["CheckoutHelper#completeCheckout -> x.y()"],
                 "log_steps": []}
        report = ag.conserved(before, after)
        assert report["ok"] is True
        assert report["verdict"] == "PLAUSIBLE"
        assert "could not be resolved" in ag.describe(report)


class TestMessageVersusExpectedValue:
    """The distinction that decides whether this guard is usable at all.

    An assertion's last argument is its human-readable failure message. Including
    it in the fingerprint meant improving the wording of a message registered as a
    *weakened assertion* — and a guard that cries wolf over a copy edit is one
    people learn to override, which costs far more than it saves.
    """

    PAGE = ('    public void verifyTotal() {\n'
            '        AssertHelper.assertEquals(testConfig, total, "42.00", '
            '"Order total should be 42.00");\n'
            '    }\n')

    def _report(self, tmp_path, factory, mutated):
        before = ag.fingerprints("CheckoutTest", "placeOrder",
                                 _repo(tmp_path, page_body=self.PAGE))
        after_root = factory.mktemp("after")
        after = ag.fingerprints("CheckoutTest", "placeOrder",
                                _repo(after_root, page_body=mutated))
        return ag.conserved(before, after)

    def test_rewording_the_message_is_allowed(self, tmp_path, tmp_path_factory):
        reworded = self.PAGE.replace("Order total should be 42.00",
                                     "Order grand total should be 42.00")
        assert self._report(tmp_path, tmp_path_factory, reworded)["ok"] is True

    def test_changing_the_expected_value_is_blocked(self, tmp_path, tmp_path_factory):
        changed = self.PAGE.replace('"42.00", "Order total', '"43.00", "Order total')
        report = self._report(tmp_path, tmp_path_factory, changed)
        assert report["ok"] is False, (
            "the expected value is the whole point of the assertion; only the "
            "message is cosmetic")


class TestWhatCountsAsAHole:
    """`unresolved` is what downgrades conservation to PLAUSIBLE, so it has to
    mean something. It used to fire on ordinary framework calls in every single
    test, which trains people to skim past it — and a warning nobody reads is
    worse than no warning, because it still claims to be a guarantee.
    """

    def _write_pair(self, tmp_path, body):
        _write(tmp_path, "automation.checkout", "CheckoutTest", body)
        return _index(tmp_path)

    def test_a_call_on_a_parameter_of_a_framework_type_is_not_a_hole(self, tmp_path):
        # `Config` is not defined in this repo. A parameter declares its type
        # just as firmly as an assignment, but the local-declaration regex needs
        # an `=` and so never saw one.
        index = self._write_pair(tmp_path, textwrap.dedent("""\
            public void placeOrder(Config config) {
                config.logStep("Place an order");
            }
        """))
        fps = ag.fingerprints("CheckoutTest", "placeOrder", index)
        assert fps["unresolved"] == []
        assert fps["log_steps"] == ["Place an order"]

    def test_a_call_on_a_field_inherited_from_an_unreadable_base_is_not_a_hole(self, tmp_path):
        path = _write(tmp_path, "automation.checkout", "CheckoutTest", "")
        path.write_text(textwrap.dedent("""\
            package automation.checkout;

            public class CheckoutTest extends TestBase {
                public void placeOrder() {
                    page.locator("#buy").click();
                }
            }
        """))
        index = _index(tmp_path)
        assert ag.fingerprints("CheckoutTest", "placeOrder", index)["unresolved"] == []

    def test_a_genuinely_missing_method_is_still_reported(self, tmp_path):
        index = self._write_pair(tmp_path, textwrap.dedent("""\
            public void placeOrder() {
                logStep(testConfig, "Place an order");
            }
        """))
        fps = ag.fingerprints("CheckoutTest", "noSuchMethod", index)
        assert any("noSuchMethod" in u for u in fps["unresolved"]), (
            "over-reporting was the bug; under-reporting would be worse")


class TestCommentsAreNotCode:
    def test_a_commented_out_assertion_is_not_counted_as_live(self, tmp_path):
        """Commenting a check out is the cheapest disguise of all.

        This module already refuses an assertion hidden behind an `if`. Scanning
        raw text meant `// assertEquals(...)` fingerprinted identically to the
        real thing, so conservation compared equal and approved an edit that had
        stopped the test proving anything.
        """
        _write(tmp_path, "automation.checkout", "CheckoutTest", textwrap.dedent("""\
            public void placeOrder() {
                // AssertHelper.assertEquals(testConfig, total, "42.00", "total is right");
            }
        """))
        index = _index(tmp_path)
        assert ag.fingerprints("CheckoutTest", "placeOrder", index)["asserts"] == {}

    def test_a_javadoc_example_is_not_a_call(self, tmp_path):
        _write(tmp_path, "automation.checkout", "CheckoutTest", textwrap.dedent("""\
            /**
             * Usage:
             *   PostData created = api.execute(PostApi.CreatePost, post);
             */
            public void placeOrder() {
                logStep(testConfig, "Place an order");
            }
        """))
        index = _index(tmp_path)
        assert ag.fingerprints("CheckoutTest", "placeOrder", index)["unresolved"] == []
