"""Whether a generated test narrates its steps or summarises them in one line.

The bug this pins: the authoring agent shipped a four-step Naukri test whose whole
scenario sat behind one helper call, introduced by a single run-on logStep. Every
existing check passed — logStep present, test class, plain English — and the run
report still showed one line for the entire test, so a failure could not be traced
to a step.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from shared import logstep_narration as ln

# The shipped test from the report, narrated once.
UNDER_NARRATED = '''
package automation.naukari;

public class NaukriProfileSummaryWebTest extends TestBase {

    /**
     * Toggle the trailing dot in the Naukri Profile Summary.
     */
    @Test(description = "Toggle trailing dot in Profile Summary",
            dataProvider = "getConfig", groups = {GROUP_REGRESSION, GROUP_WEB})
    @TestVariables(automatedBy = QA.Mukesh)
    public void toggleProfileSummaryDotAndVerify(Config config)
    {
        String username = config.getRunTimeProperty("naukari.username");
        String password = config.getRunTimeProperty("naukari.password");

        NaukriProfileSummaryHelper helper = new NaukriProfileSummaryHelper(config);

        config.logStep("Login to Naukri, toggle the trailing dot in Profile Summary, save the change, and verify it persists after page reload");
        String[] result = helper.toggleProfileSummaryDot(username, password);
        String modifiedSummary  = result[0];
        String displayedSummary = result[1];

        AssertHelper.assertEquals(config, displayedSummary, modifiedSummary,
            "Profile Summary displayed after page reload should match the saved modified summary");
    }
}
'''

# The shape the same scenario should have.
WELL_NARRATED = '''
public class SauceDemoWebTest extends TestBase {

    @Test(enabled = false, description = "verify cart contains the product that was added",
            dataProvider = "getConfig", groups = {GROUP_REGRESSION, GROUP_WEB})
    @TestVariables(automatedBy = QA.Mukesh)
    public void verifyProductAppearsInCart(Config config) {
        SauceDemoHelper sauceDemo = new SauceDemoHelper(config);
        Map<String, String> credentials = sauceDemo.getCredentials("verify_cart");

        config.logStep("Nagivate to sauce lab site and perform login");
        ProductsPage products = sauceDemo.doLogin(credentials);

        config.logStep("Now, add Sauce Labs Bike Light to cart, and navigate to cart");
        products.addProductToCart("sauce-labs-bike-light");
        CartPage cart = products.goToCart();

        config.logStep("Verify Sauce Labs Bike Light is present in the cart");
        AssertHelper.assertTrue(config, cart.getCartItemCount() > 0, "Cart should contain at least one item");
        AssertHelper.assertTrue(config, cart.isProductInCart("Sauce Labs Bike Light"), "Bike Light should be in cart");
    }
}
'''

PLAN = {
    "web_test_methods": [
        {"method_name": "toggleProfileSummaryDotAndVerify",
         "steps": ["read naukri credentials",
                   "doLogin -> ProfilePage",
                   "toggle the trailing dot in Profile Summary and save",
                   "reload the profile page",
                   "assertEquals displayed summary matches saved  [source: user]"]},
        {"method_name": "verifyProductAppearsInCart",
         "steps": ["doLogin -> ProductsPage",
                   "add Sauce Labs Bike Light to the cart and go to the cart",
                   "assertTrue the product is in the cart  [source: user]"]},
    ]
}


class TestPlanSteps:

    def test_setup_steps_are_not_narratable(self):
        """Nobody reads 'allocate Admin user' in a report, and no failure of it is
        interesting on its own — so it must not raise the expected count."""
        steps = ln.narratable_steps(
            ["allocate Admin user", "build PaymentData", "setAuthToken",
             "doLogin -> DashboardPage", "assertTrue isSuccessMessageVisible"])
        assert steps == ["doLogin -> DashboardPage", "assertTrue isSuccessMessageVisible"]

    def test_source_tag_is_stripped(self):
        assert ln.narratable_steps(["verify the total  [source: user]"]) == \
            ["verify the total"]

    def test_interleaved_steps_are_dict_shaped(self):
        plan = {"flow_style": "interleaved",
                "interleaved_test_method_name": "createViaApiThenVerifyOnWeb",
                "interleaved_steps": [
                    {"step": 1, "interface": "api", "description": "Create a payment via POST"},
                    {"step": 2, "interface": "web", "description": "Verify it appears in the list"}]}
        assert ln.expected_from_plan(plan) == {
            "createViaApiThenVerifyOnWeb": ["Create a payment via POST",
                                            "Verify it appears in the list"]}


class TestActingStatements:

    def test_plumbing_is_not_an_acting_statement(self):
        """Reading a property, constructing a helper and slicing a returned array
        are how the test is wired, not what it does."""
        body = ln.test_bodies(UNDER_NARRATED)["toggleProfileSummaryDotAndVerify"]
        acting = ln.acting_statements(body)
        assert len(acting) == 2                       # the helper call and the assertion
        assert not any("getRunTimeProperty" in s for s in acting)
        assert not any("new NaukriProfileSummaryHelper" in s for s in acting)

    def test_semicolon_inside_a_string_does_not_split_a_statement(self):
        acting = ln.acting_statements(
            'page.fillText(field, "a; b; c");\n')
        assert acting == ['page.fillText(field, "a; b; c")']

    def test_log_calls_never_count_as_work(self):
        assert ln.acting_statements('config.logStep("do the thing");') == []


class TestAudit:

    def test_one_logstep_for_a_multi_step_scenario_is_flagged(self):
        finding = ln.audit(UNDER_NARRATED, ln.expected_from_plan(PLAN))
        assert set(finding) == {"toggleProfileSummaryDotAndVerify"}
        assert finding["toggleProfileSummaryDotAndVerify"]["log_steps"] == 1
        assert finding["toggleProfileSummaryDotAndVerify"]["expected"] >= 2

    def test_a_step_per_logstep_passes(self):
        assert ln.audit(WELL_NARRATED, ln.expected_from_plan(PLAN)) == {}

    def test_flagged_without_a_plan_entry_too(self):
        """A method the model named differently still cannot narrate two acting
        statements with one line."""
        assert set(ln.audit(UNDER_NARRATED, {})) == {"toggleProfileSummaryDotAndVerify"}

    def test_more_plan_steps_than_calls_does_not_demand_the_impossible(self):
        """The expectation is capped by what the method has to narrate: a test with
        two acting statements cannot carry five logSteps, and asking for them would
        be a guard that fires on correct code."""
        finding = ln.audit(UNDER_NARRATED, ln.expected_from_plan(PLAN))
        assert finding["toggleProfileSummaryDotAndVerify"]["expected"] == 2

    def test_single_step_test_is_not_a_finding(self):
        source = '''
        public class OneStepTest extends TestBase {
            @Test(dataProvider = "getConfig")
            public void loadsTheHomePage(Config config) {
                config.logStep("Open the home page and verify the banner is shown");
                AssertHelper.assertTrue(config, home.isBannerDisplayed(), "Banner should show");
            }
        }
        '''
        assert ln.audit(source, {}) == {}

    def test_a_commented_out_test_annotation_is_not_a_test(self):
        source = '''
        public class NotATest extends TestBase {
            // @Test — used to be one, kept for reference
            public void oldFlow(Config config) {
                helper.doLogin(user, pass);
                AssertHelper.assertTrue(config, page.isLoaded(), "loaded");
            }
        }
        '''
        assert ln.audit(source, {}) == {}
