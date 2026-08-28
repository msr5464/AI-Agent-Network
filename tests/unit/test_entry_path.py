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


def test_method_body_stops_at_the_matching_brace():
    source = """
    public void first(Config config) { if (x) { a(); } b(); }
    public void second(Config config) { c(); }
    """
    body = entry_path.method_body(source, "first")
    assert "b();" in body and "c();" not in body
