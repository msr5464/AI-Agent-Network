"""Credentials the run header masks must be credentials the pipeline can find.

The case these guard: an input file whose steps read

    2. Do login by using the credentials given below:
    username=ms00000raj@gmail.com
    password=SingIsKing@1234

had both lines masked in the run header (credential_masking accepts `:` and `=`)
and was then rejected by step 02 with "no credentials found in input file",
because step 01's and step 02's own regexes only accepted `:` or whitespace.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.credential_extraction import (  # noqa: E402
    credentials_from_plan, extract_credentials, has_login_credentials)
from shared.credential_masking import mask_credential_lines  # noqa: E402

NAUKRI_INPUT = """Module: Naukari
Type: web

Steps:
1. Navigate to https://www.naukri.com/nlogin/login
2. Do login by using the credentials given below:
username=ms00000raj@gmail.com
password=SingIsKing@1234
3. Save the profile
"""


class TestSeparators:
    def test_equals_is_a_separator(self):
        """The exact shape that produced the false 'no credentials' error."""
        assert extract_credentials(NAUKRI_INPUT) == {
            "username": "ms00000raj@gmail.com",
            "password": "SingIsKing@1234",
        }

    def test_colon_is_a_separator(self):
        assert extract_credentials("Username: alice\nPassword: s3cr3t\n") == {
            "username": "alice", "password": "s3cr3t"}

    def test_a_label_prefix_does_not_hide_the_label(self):
        creds = extract_credentials("Demo Username: alice\nDemo Password: s3cr3t\nDemo OTP: 123456\n")
        assert creds == {"username": "alice", "password": "s3cr3t", "otp": "123456"}

    def test_prose_with_no_separator_at_all(self):
        assert extract_credentials("1. Login using username bob and password hunter2") == {
            "username": "bob", "password": "hunter2"}

    def test_a_quoted_value_keeps_only_the_value(self):
        creds = extract_credentials('username = "dave"\npassword = "p@ss,word"\n')
        assert creds == {"username": "dave", "password": "p@ss,word"}

    def test_a_trailing_dot_belongs_to_the_password(self):
        """Sentence punctuation is stripped; a character a password can end with is not."""
        assert extract_credentials("password=Zz9!.")["password"] == "Zz9!."


class TestFalsePositives:
    def test_a_step_that_only_names_the_fields_yields_nothing(self):
        text = "1. Login as Admin user\n2. Open the username field and type the password\n"
        assert extract_credentials(text) == {}
        assert has_login_credentials(text) is False

    def test_no_text_yields_nothing(self):
        assert extract_credentials("") == {}


class TestLoginReadiness:
    def test_both_halves_are_required(self):
        assert has_login_credentials("Username: eve\n") is False
        assert has_login_credentials("Username: eve\nPassword: p\n") is True


class TestMaskingParity:
    """Anything the masker treats as a credential line must be extractable —
    the two disagreeing is exactly what caused the false error."""

    @pytest.mark.parametrize("line", [
        "username=carl@x.io", "Username: carl@x.io", "user name = carl@x.io",
        "email=carl@x.io", "USERNAME:carl@x.io",
    ])
    def test_every_masked_username_line_is_also_extracted(self, line):
        assert "***MASKED***" in mask_credential_lines(line)
        assert extract_credentials(line).get("username") == "carl@x.io"

    @pytest.mark.parametrize("line", [
        "password=hunter2", "Password: hunter2", "pwd = hunter2", "passwd:hunter2",
    ])
    def test_every_masked_password_line_is_also_extracted(self, line):
        assert "***MASKED***" in mask_credential_lines(line)
        assert extract_credentials(line).get("password") == "hunter2"


class TestCredentialsFromPlan:
    """Every step that needs credentials reads them through this, so a plan that
    reached step 02/03/04 without them still gets what the input file states."""

    def _input(self, tmp_path, text=NAUKRI_INPUT):
        path = tmp_path / "Naukari-profile-update-flow.txt"
        path.write_text(text)
        return str(path)

    def test_a_plan_with_credentials_is_left_alone(self, tmp_path):
        plan = {"demo_credentials": {"username": "planned", "password": "planned-pw"},
                "_input_file": self._input(tmp_path)}
        assert credentials_from_plan(plan) == {"username": "planned", "password": "planned-pw"}

    def test_a_plan_without_them_falls_back_to_its_input_file(self, tmp_path):
        """The TESTING_MODE-cached plan case: 01-parse.json has no
        demo_credentials at all, but the queue file it names has both."""
        plan = {"_input_file": self._input(tmp_path)}
        assert credentials_from_plan(plan) == {
            "username": "ms00000raj@gmail.com", "password": "SingIsKing@1234"}

    def test_a_half_filled_plan_keeps_its_own_value(self, tmp_path):
        plan = {"demo_credentials": {"username": "planned"},
                "_input_file": self._input(tmp_path)}
        creds = credentials_from_plan(plan)
        assert creds["username"] == "planned"          # the plan wins
        assert creds["password"] == "SingIsKing@1234"  # the file fills the gap

    def test_a_missing_input_file_is_not_an_error(self, tmp_path):
        plan = {"_input_file": str(tmp_path / "gone.txt")}
        assert credentials_from_plan(plan) == {}

    def test_it_follows_the_queue_file_into_processed(self, tmp_path):
        """A session resumed from step 04 runs after the queue file has moved."""
        processed = tmp_path / "processed"
        processed.mkdir()
        (processed / "module.txt").write_text(NAUKRI_INPUT)
        plan = {"_input_file": str(tmp_path / "module.txt")}   # no longer there
        assert credentials_from_plan(plan)["username"] == "ms00000raj@gmail.com"

    def test_an_input_file_without_credentials_yields_nothing(self, tmp_path):
        plan = {"_input_file": self._input(tmp_path, "Module: x\nSteps:\n1. Open the page\n")}
        assert credentials_from_plan(plan) == {}

    def test_an_explicit_input_file_overrides_the_plans(self, tmp_path):
        plan = {"_input_file": str(tmp_path / "gone.txt")}
        assert credentials_from_plan(plan, self._input(tmp_path))["username"] == "ms00000raj@gmail.com"
