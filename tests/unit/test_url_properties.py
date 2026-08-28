"""A URL belongs in parameters/{environment}-{country}.properties, never in Java.

The case these guard: a generated Naukri module shipped with
`private static final String LOGIN_URL = "https://www.naukri.com/nlogin/login"`
and no naukari entry in the properties file at all.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import properties_file, url_properties  # noqa: E402


class TestKeyNaming:
    def test_the_host_alone_is_the_module_base_url(self):
        assert url_properties.derive_key("naukari", "https://www.naukri.com") == "naukari.url"

    def test_a_path_is_named_for_the_page_it_opens(self):
        """`naukari.login.url` says what it opens; `naukari.url2` says nothing."""
        assert url_properties.derive_key(
            "naukari", "https://www.naukri.com/nlogin/login") == "naukari.login.url"

    def test_an_id_segment_is_not_used_as_a_name(self):
        """A key named after one record's id is a key nobody can reuse."""
        assert url_properties.derive_key(
            "orders", "https://app.io/orders/849213/detail") == "orders.detail.url"

    def test_a_collision_widens_rather_than_numbering(self):
        taken = {"app.detail.url": "https://app.io/orders/detail"}
        assert url_properties.derive_key(
            "app", "https://app.io/settings/detail", taken) == "app.settings.detail.url"


class TestCollectingUrls:
    def test_it_finds_the_urls_that_were_hardcoded_in_the_naukri_module(self):
        plan = {"feature_name": "naukari", "web_base_url": "https://www.naukri.com",
                "api_base_url": "",
                "web_steps_for_validation": [
                    "Navigate to https://www.naukri.com/nlogin/login"]}
        web = {"steps_passed": ["Navigate to https://www.naukri.com/nlogin/login",
                                "Navigate to https://www.naukri.com/mnjuser/profile"]}
        assert url_properties.collect_urls(plan, web) == {
            "naukari.url":         "https://www.naukri.com",
            "naukari.login.url":   "https://www.naukri.com/nlogin/login",
            "naukari.profile.url": "https://www.naukri.com/mnjuser/profile",
        }

    def test_a_trailing_slash_is_not_a_second_url(self):
        plan = {"feature_name": "app", "web_base_url": "https://app.io/",
                "web_steps_for_validation": ["Open https://app.io"]}
        assert list(url_properties.collect_urls(plan, {})) == ["app.url"]

    def test_prose_punctuation_is_not_part_of_the_url(self):
        plan = {"feature_name": "app",
                "web_steps_for_validation": ["Go to https://app.io/login."]}
        assert url_properties.collect_urls(plan, {}) == {"app.login.url": "https://app.io/login"}

    def test_the_local_cdp_endpoint_is_not_an_application_url(self):
        plan = {"feature_name": "app",
                "web_steps_for_validation": ["connect http://localhost:9222"]}
        assert url_properties.collect_urls(plan, {}) == {}


class TestTheGuard:
    HELPER = '''
public class NaukriProfileSummaryHelper extends ApiHelper
{
    private static final String BASE_URL  = "https://www.naukri.com";
    private static final String LOGIN_URL = "https://www.naukri.com/nlogin/login";
}
'''

    def test_it_catches_the_constant_that_shipped(self):
        assert url_properties.hardcoded_urls(self.HELPER) == [
            "https://www.naukri.com", "https://www.naukri.com/nlogin/login"]

    def test_a_url_in_a_comment_is_documentation_not_a_violation(self):
        source = ('/** Navigates to https://www.naukri.com/nlogin/login */\n'
                  '// see https://docs.io/x\n'
                  'String u = config.getRunTimeProperty("naukari.login.url");')
        assert url_properties.hardcoded_urls(source) == []

    def test_stripping_comments_does_not_slice_a_url_literal_in_half(self):
        """The "//" in "https://" is the reason a naive comment strip cannot be used —
        it would hide exactly the violations this looks for."""
        assert url_properties.hardcoded_urls('String u = "https://real.io/login";') == [
            "https://real.io/login"]

    def test_a_property_lookup_passes(self):
        source = 'super(config, config.getRunTimeProperty("naukari.api.url"));'
        assert url_properties.hardcoded_urls(source) == []

    def test_a_fix_that_adds_a_literal_url_is_rejected(self):
        before = "String u = config.getRunTimeProperty(\"a.url\");\n"
        after  = "String u = \"https://real.io/login\";\n"
        ok, reason = url_properties.no_hardcoded_url(before, after)
        assert not ok and "properties" in reason

    def test_a_literal_already_in_the_file_does_not_block_an_unrelated_fix(self):
        """Judged on added lines only — otherwise every fix to a legacy file is
        rejected for a URL the fix never touched."""
        before = 'String u = "https://legacy.io";\nint x = 1;\n'
        after  = 'String u = "https://legacy.io";\nint x = 2;\n'
        assert url_properties.no_hardcoded_url(before, after) == (True, "")


class TestWritingTheProperties:
    def _fw(self, tmp_path, monkeypatch, body=""):
        monkeypatch.setenv("AUTOCREATE_ENVIRONMENT", "staging")
        monkeypatch.setenv("AUTOCREATE_COUNTRY", "SG")
        (tmp_path / "parameters").mkdir(parents=True, exist_ok=True)
        path = tmp_path / "parameters" / "staging-sg.properties"
        path.write_text(body)
        return path

    def test_keys_are_written_to_the_env_country_file(self, tmp_path, monkeypatch):
        path = self._fw(tmp_path, monkeypatch, "saucedemo.url=https://www.saucedemo.com/\n")
        status = url_properties.write_url_properties(
            tmp_path, {"naukari.login.url": "https://www.naukri.com/nlogin/login"}, "naukari")
        assert status == "written"
        body = path.read_text()
        assert "naukari.login.url=https://www.naukri.com/nlogin/login" in body
        assert "saucedemo.url=https://www.saucedemo.com/" in body, "must not clobber"

    def test_a_value_someone_changed_is_left_alone(self, tmp_path, monkeypatch):
        path = self._fw(tmp_path, monkeypatch, "naukari.url=https://qa.naukri.internal\n")
        assert url_properties.write_url_properties(
            tmp_path, {"naukari.url": "https://www.naukri.com"}, "naukari") == "already present"
        assert "qa.naukri.internal" in path.read_text()

    def test_an_empty_value_is_filled_in_place_not_duplicated(self, tmp_path, monkeypatch):
        path = self._fw(tmp_path, monkeypatch, "naukari.url=\n")
        assert url_properties.write_url_properties(
            tmp_path, {"naukari.url": "https://www.naukri.com"}, "naukari") == "written"
        body = path.read_text()
        assert body.count("naukari.url") == 1
        assert "naukari.url=https://www.naukri.com" in body

    def test_ship_can_build_the_committed_file_without_touching_disk(self):
        """05_ship.py commits HEAD's copy plus the URL keys — never the working
        copy, which holds the run's real credentials."""
        committed = "github.username=demo@yopmail.com\n"
        updated, filled, appended = properties_file.apply(
            committed, {"naukari.url": "https://www.naukri.com"}, "Naukari URLs")
        assert appended == {"naukari.url": "https://www.naukri.com"}
        assert "github.username=demo@yopmail.com" in updated
        assert "naukari.username" not in updated


class TestReferencedKeys:
    def test_a_url_key_the_code_reads_is_reported(self):
        source = ('config.getRunTimeProperty("naukari.login.url");'
                  'config.getRunTimeProperty("naukari.username");')
        assert url_properties.referenced_keys(source) == ["naukari.login.url"]


class TestStep03Enforcement:
    """The repair pass in 03_generate.py, driven end to end with a stubbed model."""

    HELPER = '''package automation.modules.naukari;

public class NaukriProfileSummaryHelper extends ApiHelper
{
    private static final String BASE_URL  = "https://www.naukri.com";
    private static final String LOGIN_URL = "https://www.naukri.com/nlogin/login";

    public NaukriProfileSummaryHelper(Config config)
    {
        super(config, BASE_URL);
    }

    public NaukriProfilePage doLogin(String username, String password)
    {
        BrowserHelper.navigateTo(config, LOGIN_URL);
        return new NaukriLoginPage(config).doLogin(username, password);
    }
}
'''
    REPAIRED = '''package automation.modules.naukari;

public class NaukriProfileSummaryHelper extends ApiHelper
{
    public NaukriProfileSummaryHelper(Config config)
    {
        super(config, config.getRunTimeProperty("naukari.url"));
    }

    public NaukriProfilePage doLogin(String username, String password)
    {
        BrowserHelper.navigateTo(config, config.getRunTimeProperty("naukari.login.url"));
        return new NaukriLoginPage(config).doLogin(username, password);
    }
}
'''
    PATH = "src/main/java/automation/modules/naukari/NaukriProfileSummaryHelper.java"

    def _step03(self, tmp_path, monkeypatch):
        import importlib.util
        root = Path(__file__).resolve().parents[2]
        monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
        monkeypatch.setenv("REPO_ROOT", str(root))
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("GITHUB_REPO_AUTOMATION", "fw")
        monkeypatch.setenv("AUTOCREATE_ENVIRONMENT", "staging")
        monkeypatch.setenv("AUTOCREATE_COUNTRY", "SG")
        path = root / "agents" / "test-authoring-agent" / "actions" / "03_generate.py"
        spec = importlib.util.spec_from_file_location("authoring_03_url", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (tmp_path / "fw" / "parameters").mkdir(parents=True, exist_ok=True)
        return module

    def test_a_hardcoded_url_is_repaired_into_a_property_lookup(self, tmp_path, monkeypatch):
        step03 = self._step03(tmp_path, monkeypatch)
        import json as _json
        monkeypatch.setattr(step03, "call_claude",
                            lambda prompt, label="": _json.dumps({self.PATH: self.REPAIRED}))

        files, remaining = step03._repair_hardcoded_urls(
            {self.PATH: self.HELPER},
            {"naukari.url": "https://www.naukri.com",
             "naukari.login.url": "https://www.naukri.com/nlogin/login"},
            "naukari", "staging-sg.properties")

        assert remaining == {}, "nothing should still be hardcoded"
        assert 'getRunTimeProperty("naukari.login.url")' in files[self.PATH]
        assert "https://www.naukri.com" not in files[self.PATH]

    def test_a_repair_that_drops_a_method_is_refused(self, tmp_path, monkeypatch):
        """The repair is allowed to move a URL, not to rewrite the class around it."""
        step03 = self._step03(tmp_path, monkeypatch)
        import json as _json
        gutted = 'public class NaukriProfileSummaryHelper extends ApiHelper\n{\n}\n'
        monkeypatch.setattr(step03, "call_claude",
                            lambda prompt, label="": _json.dumps({self.PATH: gutted}))

        files, remaining = step03._repair_hardcoded_urls(
            {self.PATH: self.HELPER}, {"naukari.url": "https://www.naukri.com"},
            "naukari", "staging-sg.properties")

        assert files[self.PATH] == self.HELPER, "the original must be kept"
        assert remaining[self.PATH], "and the violation must stay visible"

    def test_a_url_the_model_invented_still_gets_a_property(self, tmp_path, monkeypatch):
        """The repair needs a key to point at, even for a URL nothing harvested."""
        step03 = self._step03(tmp_path, monkeypatch)
        monkeypatch.setattr(step03, "call_claude", lambda prompt, label="": "")

        step03._repair_hardcoded_urls(
            {self.PATH: 'String u = "https://www.naukri.com/mnjuser/profile";'},
            {}, "naukari", "staging-sg.properties")

        body = (tmp_path / "fw" / "parameters" / "staging-sg.properties").read_text()
        assert "naukari.profile.url=https://www.naukri.com/mnjuser/profile" in body
