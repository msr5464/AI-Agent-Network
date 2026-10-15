"""Tests for shared/entry_path.py.

The adaptation agent edits an existing test, and that test states how it signs
itself in. Deriving login from repo-wide convention instead is what put GitHub's
`#login_field` in front of a Naukri password, and no convention could have
avoided it: the framework spells the page object `LoginPage` twice and
`NaukriLoginPage` once, the URL key `saucedemo.url` twice and `githubUrl` once,
and GitHubLoginTest signs in two different ways in one file.

So what is pinned here is that all three shapes are read out of the source.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import entry_path


def _write(workspace, relative, body):
    path = Path(workspace) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


CREDENTIAL_TEST = """
package automation.naukari;
import automation.modules.naukari.NaukriProfileSummaryHelper;
public class NaukriTest extends TestBase {
    public void toggleDot(Config config) {
        String username = config.getRunTimeProperty("naukari.username");
        String password = config.getRunTimeProperty("naukari.password");
        NaukriProfileSummaryHelper naukri = new NaukriProfileSummaryHelper(config);
        NaukriProfilePage profilePage = naukri.doLogin(username, password);
        AssertHelper.assertTrue(config, profilePage.isLoaded(), "loaded");
    }
}
"""

SESSION_TEST = """
package automation.github;
import automation.modules.github.GitHubHelper;
public class GitHubTest extends TestBase {
    public void usingStoredSession(Config config) {
        GitHubHelper github = new GitHubHelper(config);
        DashboardPage dashboard = github.loginWithStoredSession();
    }
}
"""

GITHUB_HELPER = """
package automation.modules.github;
public class GitHubHelper extends ApiHelper {
    private static final String SESSION_FILE = "GitHubLoginStorage.json";
    public DashboardPage loginWithStoredSession() {
        BrowserHelper.initBrowserWithStoredSession(config, ProjectName.GitHub, SESSION_FILE);
        return new DashboardPage(config);
    }
}
"""

NAUKRI_HELPER = """
package automation.modules.naukari;
public class NaukriProfileSummaryHelper extends ApiHelper {
    public NaukriProfilePage doLogin(String username, String password) {
        BrowserHelper.navigateTo(config, LOGIN_URL);
        return new NaukriLoginPage(config).doLogin(username, password);
    }
}
"""


@pytest.fixture
def workspace(tmp_path):
    _write(tmp_path, "src/test/java/automation/naukari/NaukriTest.java", CREDENTIAL_TEST)
    _write(tmp_path, "src/test/java/automation/github/GitHubTest.java", SESSION_TEST)
    _write(tmp_path, "src/main/java/automation/modules/github/GitHubHelper.java", GITHUB_HELPER)
    _write(tmp_path, "src/main/java/automation/modules/naukari/NaukriProfileSummaryHelper.java",
           NAUKRI_HELPER)
    return tmp_path


def test_credential_mode_reads_the_keys_the_test_reads(workspace):
    """Not `{module}.username` by convention — the keys in the source."""
    entry = entry_path.extract(workspace, "automation.naukari.NaukriTest#toggleDot")
    assert entry["mode"] == "credential"
    assert entry["arg_keys"] == ["naukari.username", "naukari.password"]
    assert entry["method"] == "doLogin"
    assert entry["helper"] == "automation.modules.naukari.NaukriProfileSummaryHelper"


def test_stored_session_mode_resolves_the_file_the_test_loads(workspace):
    """Following the constant, not guessing the filename."""
    entry = entry_path.extract(workspace, "automation.github.GitHubTest#usingStoredSession")
    assert entry["mode"] == "stored_session"
    assert entry["session"]["path"] == \
        "src/test/resources/github/loginStorage/GitHubLoginStorage.json"


def test_a_test_that_never_signs_in_reports_none(workspace):
    """So exploration can proceed unauthenticated instead of hard-stopping."""
    _write(workspace, "src/test/java/automation/naukari/PublicTest.java", """
    package automation.naukari;
    public class PublicTest extends TestBase {
        public void browsePublicPage(Config config) {
            AssertHelper.assertTrue(config, true, "no login here");
        }
    }
    """)
    entry = entry_path.extract(workspace, "automation.naukari.PublicTest#browsePublicPage")
    assert entry["mode"] == "none"


def test_an_unresolvable_argument_is_named_not_glossed(workspace):
    """GitHub's OTP test passes a literal; saying "never signs in" would mislead."""
    _write(workspace, "src/test/java/automation/github/OtpTest.java", """
    package automation.github;
    import automation.modules.github.GitHubHelper;
    public class OtpTest extends TestBase {
        public void withOtp(Config config) {
            String username = config.getRunTimeProperty("github.username");
            String password = config.getRunTimeProperty("github.password");
            String otp = "123456";
            GitHubHelper github = new GitHubHelper(config);
            DashboardPage d = github.doLoginWithOtp(username, password, otp);
        }
    }
    """)
    entry = entry_path.extract(workspace, "automation.github.OtpTest#withOtp")
    assert entry["mode"] == "none"
    assert "otp" in entry["reason"] and "doLoginWithOtp" in entry["reason"]


def test_credentials_fetched_through_the_helper_are_followed(workspace):
    """SauceDemo loads a CSV row with getCredentials(...) and passes the Map on —
    reading that as "never signs in" sent exploration off unauthenticated."""
    _write(workspace, "src/main/java/automation/modules/saucedemo/SauceDemoHelper.java", """
    package automation.modules.saucedemo;
    public class SauceDemoHelper {
        public ProductsPage doLogin(Map<String, String> credentials) { return null; }
    }
    """)
    _write(workspace, "src/test/java/automation/saucedemo/SauceDemoWebTest.java", """
    package automation.saucedemo;
    import automation.modules.saucedemo.SauceDemoHelper;
    public class SauceDemoWebTest extends TestBase {
        public void addProductToCart(Config config) {
            SauceDemoHelper sauceDemo = new SauceDemoHelper(config);
            Map<String, String> credentials = sauceDemo.getCredentials("add_to_cart");
            ProductsPage products = sauceDemo.doLogin(credentials);
        }
    }
    """)
    entry = entry_path.extract(workspace,
                               "automation.saucedemo.SauceDemoWebTest#addProductToCart")
    assert entry["mode"] == "credential"
    assert entry["helper"] == "automation.modules.saucedemo.SauceDemoHelper"
    assert (entry["method"], entry["data_method"], entry["data_arg"]) == \
        ("doLogin", "getCredentials", "add_to_cart")
    assert 'SauceDemoHelper.doLogin(getCredentials("add_to_cart"))' in entry_path.describe(entry)


def test_method_body_stops_at_the_matching_brace():
    source = """
    public void first(Config config) { if (x) { a(); } b(); }
    public void second(Config config) { c(); }
    """
    body = entry_path.method_body(source, "first")
    assert "b();" in body and "c();" not in body


# ── What the test does today, and the data it types ───────────────────────────

WALK_TEST = """
package automation.shop;
public class ShopWebTest extends TestBase {
    @Test
    public void payByCard(Config config) {
        ShopHelper shop = new ShopHelper(config);
        ShopData order = buildOrder();
        config.logStep("Open the shop and add the item to the cart");
        shop.cartPage = shop.addToCart(order);
        if (order.hasCoupon()) {
            config.logStep("Apply the coupon");
        }
        config.logStep("%s");
    }

    public void refundOrder(Config config) {
        Map<String, String> row = TestDataReader.loadCsvRowByColumnValue(
            "shop", "orders", "scenario", "refund");
    }

    private ShopData buildOrder() {
        Map<String, String> data = TestDataReader.loadCsvRowByColumnValue(
            "shop", "orders", "scenario", "card");
        return new ShopData(data);
    }
}
"""
ORDERS = ("scenario,amount,card_number,password,bank_otp,phone,note\n"
          "card,500,4111 1111,hunter2,112233,{randomNumber:10},\n"
          "refund,90,4000 0000,x,1,2,later\n")
WALK_ID = "automation.shop.ShopWebTest#payByCard"


def _shop(tmp_path, last_step="Pay by card and enter the bank code", orders=ORDERS):
    _write(tmp_path, "src/test/java/automation/shop/ShopWebTest.java",
           WALK_TEST % last_step)
    if orders is not None:
        _write(tmp_path, "src/test/resources/shop/data/orders.csv", orders)
    return entry_path.test_walk(tmp_path, WALK_ID)


class TestWalk:
    """An explorer handed neither the test's steps nor its data invented both."""

    def test_the_steps_are_the_tests_own_log_in_order(self, tmp_path):
        assert _shop(tmp_path)["steps"] == [
            "Open the shop and add the item to the cart", "Apply the coupon",
            "Pay by card and enter the bank code"]

    def test_the_row_is_the_one_this_test_loads(self, tmp_path):
        # buildOrder() is called by the test; refundOrder() is another test's read.
        data = _shop(tmp_path)["data"]
        assert [d["file"] for d in data] == ["src/test/resources/shop/data/orders.csv"]
        assert data[0]["row"] == {"scenario": "card", "amount": "500",
                                  "card_number": "4111 1111", "password": "hunter2",
                                  "bank_otp": "112233", "phone": "{randomNumber:10}"}

    def test_a_login_is_in_the_row_like_the_rest_of_it(self, tmp_path):
        """An explorer told to sign in as one of the test's steps was shown the row
        without its password."""
        signs_in = _shop(tmp_path, "Log in and pay by card")["data"][0]["row"]
        assert signs_in["password"] and signs_in["bank_otp"] == "112233"
        assert signs_in["amount"] == "500"

    @pytest.mark.parametrize("orders", [
        None,                                                   # no such file
        "scenario,amount\ncard,1\ncard,2\n",                   # two rows hold the key
        "scenario,amount\nrefund,1\n",                         # no row holds it
    ])
    def test_no_row_is_sent_unless_it_is_certain(self, tmp_path, orders):
        assert _shop(tmp_path, orders=orders)["data"] == []

    def test_two_files_of_one_name_are_not_guessed_between(self, tmp_path):
        _write(tmp_path, "src/test/resources/shop/archive/orders.csv", ORDERS)
        assert _shop(tmp_path)["data"] == []

    def test_a_test_that_is_not_there_has_nothing_to_say(self, tmp_path):
        walk = entry_path.test_walk(tmp_path, "automation.shop.GoneTest#flow")
        assert (walk["steps"], walk["data"]) == ([], [])

    def test_where_a_test_would_be_declared(self):
        assert entry_path.source_candidates(WALK_ID) == [
            "src/test/java/automation/shop/ShopWebTest.java",
            "src/main/java/automation/shop/ShopWebTest.java"]


# ── A test that signs in through its helper and types what a builder holds ────

PORTAL_TEST = """
package automation.portal;
import automation.modules.portal.PortalBuilder;
import automation.modules.portal.PortalData;
import automation.modules.portal.PortalHelper;
public class PortalWebTest extends TestBase {
    public void addMember(Config config) {
        PortalHelper portal = new PortalHelper(config);
        PortalData member = new PortalBuilder().withCity("Pune").build();
        config.logStep("Sign in to the portal and validate the dashboard is shown");
        portal.dashboardPage = portal.login();
        config.logStep("Add the member and validate the name is shown");
        portal.addMember(member);
    }
}
"""

PORTAL_HELPER = """
package automation.modules.portal;
import automation.modules.portal.web.SignInPage;
public class PortalHelper extends ApiHelper {
    public SignInPage signInPage;
    public DashboardPage login() {
        BrowserHelper.navigateTo(config, config.getRunTimeProperty("portal.url"));
        signInPage = new SignInPage(config);
        dashboardPage = signInPage.login("admin", "s3cret, x");
        return dashboardPage;
    }
}
"""

SIGN_IN_PAGE = """
package automation.modules.portal.web;
public class SignInPage extends BasePage {
    public DashboardPage login(String username, String password) {
        fillText(usernameField, username, "Username");
        return new DashboardPage(config);
    }
}
"""

PORTAL_BUILDER = """
package automation.modules.portal;
public class PortalBuilder {
    private String firstName;
    private String city;
    private String tier = "Standard";
    private String memberId;
    public PortalBuilder withCity(String city) { this.city = city; return this; }
    public PortalBuilder withDefaults() {
        if (firstName == null) firstName = "Asha";
        if (city == null) city = "Delhi";
        if (memberId == null) memberId = "M_" + DataGenerator.randomAlphaString(5);
        return this;
    }
    public PortalData build() {
        withDefaults();
        return new PortalData();
    }
}
"""

PORTAL_ID = "automation.portal.PortalWebTest#addMember"


@pytest.fixture
def portal(tmp_path):
    _write(tmp_path, "src/test/java/automation/portal/PortalWebTest.java", PORTAL_TEST)
    _write(tmp_path, "src/main/java/automation/modules/portal/PortalHelper.java", PORTAL_HELPER)
    _write(tmp_path, "src/main/java/automation/modules/portal/web/SignInPage.java",
           SIGN_IN_PAGE)
    _write(tmp_path, "src/main/java/automation/modules/portal/PortalBuilder.java",
           PORTAL_BUILDER)
    return tmp_path


def test_a_login_the_helper_holds_itself_is_still_a_sign_in(portal):
    # Read as "never signs in", its explorer was sent to a login form with nothing
    # to type and told the flow had no login in it.
    entry = entry_path.extract(portal, PORTAL_ID)
    assert entry["mode"] == "none", "there is no session to load and none to mint"
    assert entry["login_call"] == "login"
    assert "signs in through login()" in entry["reason"]
    assert "never signs in" not in entry["reason"]


def test_what_the_helpers_login_types_is_read_with_the_names_it_is_passed_as(portal):
    walk = entry_path.test_walk(portal, PORTAL_ID)
    signed = [row for row in walk["data"] if row.get("typed_by")]
    assert signed == [{"file": "src/main/java/automation/modules/portal/PortalHelper.java",
                       "row": {"username": "admin", "password": "s3cret, x"},
                       "typed_by": "PortalHelper.login()"}]


def test_a_login_read_from_the_frameworks_properties_is_resolved(portal, monkeypatch):
    for name in ("ADAPT_ENVIRONMENT", "ADAPT_COUNTRY"):
        monkeypatch.delenv(name, raising=False)
    _write(portal, "src/main/java/automation/modules/portal/PortalHelper.java",
           PORTAL_HELPER.replace(
               'dashboardPage = signInPage.login("admin", "s3cret, x");',
               'String user = config.getRunTimeProperty("portal.username");\n'
               '        String pass = config.getRunTimeProperty("portal.password");\n'
               '        dashboardPage = signInPage.login(user, pass);'))
    _write(portal, "parameters/staging-sg.properties",
           "portal.username=asha\nportal.password=pw-1\n")
    signed = [row["row"] for row in entry_path.test_walk(portal, PORTAL_ID)["data"]
              if row.get("typed_by")]
    assert signed == [{"username": "asha", "password": "pw-1"}]


def test_a_login_value_that_cannot_be_resolved_is_left_out(portal):
    _write(portal, "src/main/java/automation/modules/portal/PortalHelper.java",
           PORTAL_HELPER.replace('"s3cret, x"', "vault.secret()"))
    assert [row for row in entry_path.test_walk(portal, PORTAL_ID)["data"]
            if row.get("typed_by")] == []


def test_the_fields_of_an_object_the_test_builds_are_its_values(portal):
    # Shown the steps without these, an explorer typed a name with one part
    # missing and reported the check on it as failing on the new flow.
    built = [row for row in entry_path.test_walk(portal, PORTAL_ID)["data"]
             if not row.get("typed_by")]
    assert built == [{"file": "src/main/java/automation/modules/portal/PortalBuilder.java",
                      "row": {"tier": "Standard", "firstName": "Asha", "city": "Pune"}}], (
        "a literal default, a literal given when unset, and the test's own override; "
        "nothing for a value made up when the test runs")


def test_a_test_that_builds_nothing_and_signs_in_nowhere_reads_as_before(tmp_path):
    _write(tmp_path, "src/test/java/automation/portal/PlainTest.java", """
package automation.portal;
public class PlainTest extends TestBase {
    public void opens(Config config) {
        config.logStep("Open the home page");
        HomePage home = new HomePage(config);
    }
}
""")
    walk = entry_path.test_walk(tmp_path, "automation.portal.PlainTest#opens")
    assert walk["steps"] == ["Open the home page"] and walk["data"] == []
    entry = entry_path.extract(tmp_path, "automation.portal.PlainTest#opens")
    assert "never signs in" in entry["reason"] and "login_call" not in entry


def test_a_sign_in_through_the_helper_is_not_described_as_no_login(portal):
    described = entry_path.describe(entry_path.extract(portal, PORTAL_ID))
    assert described.startswith("no saved session — the test signs in through login()")
