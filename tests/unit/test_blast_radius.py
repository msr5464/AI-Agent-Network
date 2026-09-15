"""Tests for shared/blast_radius.py.

The failure mode this guards against is not a crash — it is a plausible-looking
answer. A blast radius that returns the whole suite and one that returns nothing
are both wrong, and both look like a working feature until someone checks. Each
case below is a real mistake the first implementation made against the Jarvis
repo before it was fixed.
"""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import blast_radius as br


def _java(path: Path, package: str, name: str, body: str = "", extends: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = f" extends {extends}" if extends else ""
    path.write_text(textwrap.dedent(f"""\
        package {package};

        public class {name}{ext} {{
        {body}
        }}
        """))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature automation repo with the shapes that actually matter."""
    main = tmp_path / "src" / "main" / "java" / "automation"
    test = tmp_path / "src" / "test" / "java" / "automation"

    # Framework: used from every module, so never an edit candidate.
    _java(main / "core" / "Element.java", "automation.core", "Element",
          "    public static void click() {}")
    _java(main / "core" / "BasePage.java", "automation.core", "BasePage",
          "    protected Element element;")

    # Checkout module — the change lands here.
    _java(main / "modules" / "checkout" / "web" / "CartPage.java",
          "automation.modules.checkout.web", "CartPage",
          "    Element e;", extends="BasePage")
    _java(main / "modules" / "checkout" / "CheckoutHelper.java",
          "automation.modules.checkout", "CheckoutHelper",
          "    CartPage cart;")

    # Unrelated module, reaching the framework but not checkout.
    _java(main / "modules" / "billing" / "BillingHelper.java",
          "automation.modules.billing", "BillingHelper", "    Element e;")

    def _test(pkg_dir, package, name, body):
        _java(test / pkg_dir / f"{name}.java", package, name, body)

    _test("checkout", "automation.checkout", "CheckoutWebTest",
          "    CheckoutHelper helper;\n"
          "    @Test public void placeOrder() {}")
    # Shares CheckoutHelper — green today, would break with the change.
    _test("checkout", "automation.checkout", "CheckoutSmokeTest",
          "    CheckoutHelper helper;\n"
          "    @Test public void smoke() {}")
    # Only reaches checkout through the framework — must be excluded, and named.
    _test("billing", "automation.billing", "BillingTest",
          "    BillingHelper helper;\n    Element e;\n"
          "    @Test public void charge() {}")

    # No hub-threshold patching: at 8 classes nothing crosses the count
    # threshold, which is deliberate — it leaves module locality to do the work
    # and proves that rule holds on its own rather than being masked by the
    # count rule.
    br._cache.clear()
    return str(tmp_path)


class TestIndex:
    def test_same_package_reference_without_import_is_an_edge(self, repo):
        graph = br.index(repo, use_cache=False)
        helper = "automation.modules.checkout.CheckoutHelper"
        cart = "automation.modules.checkout.web.CartPage"
        assert cart in graph["classes"][helper]["references"], (
            "a page object used from its own package has no import line; "
            "an import-only graph misses the commonest edge in this codebase")

    def test_mention_in_a_comment_is_not_a_dependency(self, tmp_path, monkeypatch):
        main = tmp_path / "src" / "main" / "java" / "automation"
        _java(main / "core" / "ApiHelper.java", "automation.core", "ApiHelper",
              "    // See GitHubData for an example\n"
              '    String s = "GitHubData";')
        _java(main / "modules" / "github" / "GitHubData.java",
              "automation.modules.github", "GitHubData")
        br._cache.clear()
        graph = br.index(str(tmp_path), use_cache=False)
        refs = graph["classes"]["automation.core.ApiHelper"]["references"]
        assert "automation.modules.github.GitHubData" not in refs, (
            "a javadoc example created an edge from the API base class into a "
            "module, and since every helper extends it that one comment made "
            "every API test look related to every other one")


class TestResolve:
    def test_named_tier_holds_the_glob_matches(self, repo):
        result = br.resolve(repo, affects=["automation.checkout.*"])
        named = {row["test"] for row in result["tiers"]["named"]}
        assert "automation.checkout.CheckoutWebTest#placeOrder" in named
        assert "automation.checkout.CheckoutSmokeTest#smoke" in named

    def test_sibling_test_sharing_a_helper_is_shared_surface(self, repo):
        result = br.resolve(repo, named_tests=[
            "automation.checkout.CheckoutWebTest#placeOrder"])
        shared = {row["test"] for row in result["tiers"]["shared_surface"]}
        assert "automation.checkout.CheckoutSmokeTest#smoke" in shared, (
            "a test that passes today but shares the changed helper is the "
            "whole reason this runs before the tests go red")

    def test_framework_only_neighbour_is_excluded_and_reported(self, repo):
        result = br.resolve(repo, affects=["automation.checkout.*"])
        verify = {r["test"] for r in
                  result["tiers"]["named"] + result["tiers"]["shared_surface"]}
        assert "automation.billing.BillingTest#charge" not in verify
        distant = {r["test"] for r in result["tiers"]["distant"]}
        assert "automation.billing.BillingTest#charge" in distant, (
            "excluded and not-related are different answers; only one of them "
            "is a judgement call, so it has to be visible")

    def test_framework_classes_are_never_edit_candidates(self, repo):
        result = br.resolve(repo, affects=["automation.checkout.*"])
        roles = {c["fqcn"] for c in result["edit_candidates"]}
        assert "automation.core.Element" not in roles
        assert "automation.core.BasePage" not in roles
        assert "automation.modules.checkout.web.CartPage" in roles

    def test_module_fallback_when_affects_is_absent(self, repo):
        result = br.resolve(repo, module="checkout")
        assert result["tiers"]["named"], "Module: must work when Affects: is omitted"
        assert "no Affects given" in result["selection"], (
            "a derived scope is a weaker claim than a stated one and has to say so")

    def test_hop_bound_is_honoured(self, repo):
        near = br.resolve(repo, affects=["automation.checkout.*"], max_hops=1)
        far = br.resolve(repo, affects=["automation.checkout.*"], max_hops=3)
        assert len(near["edit_candidates"]) <= len(far["edit_candidates"])

    def test_budget_flags_a_change_too_big_for_one_run(self, repo, monkeypatch):
        monkeypatch.setattr(br, "MAX_TESTS", 1)
        result = br.resolve(repo, affects=["automation.checkout.*"])
        assert result["budget"]["over_limit"] is True
        assert "escalate" in br.describe(result)

    def test_nothing_matched_is_an_empty_answer_not_a_crash(self, repo):
        result = br.resolve(repo, affects=["automation.nosuch.*"])
        assert result["tiers"]["named"] == []
        assert result["budget"]["tests_to_verify"] == 0
