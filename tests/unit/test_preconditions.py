"""Unit tests for shared/preconditions.py.

The case these are written around: an expired session usually still holds plenty
of valid cookies. A count of what is still good proves nothing, so every finding
has to name the specific cookie and its date.
"""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import preconditions

DAY = 86400


def _storage(tmp_path, cookies, name="Session.json"):
    path = tmp_path / "src/test/resources/mod/loginStorage"
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps({"cookies": cookies, "origins": []}))
    return path / name


def _log(relative_path):
    return f"[00:00:49] Loaded stored session: {relative_path}\n"


class TestAuditStorageState:
    def test_all_valid(self, tmp_path):
        report = preconditions.audit_storage_state(_storage(tmp_path, [
            {"name": "auth", "domain": "x.com", "expires": time.time() + DAY}]))
        assert report["valid"] is True and report["expired"] == []

    def test_expired_cookie_is_named_with_its_date(self, tmp_path):
        report = preconditions.audit_storage_state(_storage(tmp_path, [
            {"name": "user_session", "domain": "x.com", "expires": time.time() - DAY}]))
        assert report["valid"] is False
        assert report["expired"][0]["name"] == "user_session"
        assert report["expired"][0]["expired_at"]

    def test_session_cookies_are_not_expired(self, tmp_path):
        report = preconditions.audit_storage_state(_storage(tmp_path, [
            {"name": "tz", "domain": "x.com", "expires": -1},
            {"name": "sid", "domain": "x.com"}]))
        assert report["valid"] is True
        assert report["session_cookies"] == 2

    def test_valid_cookies_do_not_mask_a_dead_auth_cookie(self, tmp_path):
        # The exact trap from the reported bug: logged_in and the username cookie
        # are good for another year while the two that authenticate have died.
        report = preconditions.audit_storage_state(_storage(tmp_path, [
            {"name": "logged_in", "domain": "x.com", "expires": time.time() + 365 * DAY},
            {"name": "dotcom_user", "domain": "x.com", "expires": time.time() + 365 * DAY},
            {"name": "user_session", "domain": "x.com", "expires": time.time() - 120 * DAY}]))
        assert report["valid"] is False
        assert [c["name"] for c in report["expired"]] == ["user_session"]

    def test_auth_like_cookies_are_reported_first(self, tmp_path):
        report = preconditions.audit_storage_state(_storage(tmp_path, [
            {"name": "prefs", "domain": "x.com", "expires": time.time() - 2 * DAY},
            {"name": "user_session", "domain": "x.com", "expires": time.time() - DAY}]))
        assert report["expired"][0]["name"] == "user_session"

    def test_missing_file_is_unknown_not_valid(self, tmp_path):
        report = preconditions.audit_storage_state(tmp_path / "nope.json")
        assert report["valid"] is None and report["exists"] is False

    def test_corrupt_file_is_unknown_not_valid(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert preconditions.audit_storage_state(path)["valid"] is None


class TestCheck:
    def test_only_artifacts_the_run_referenced_are_examined(self, tmp_path):
        # A stale file some other test owns is not evidence about this one.
        _storage(tmp_path, [{"name": "user_session", "domain": "x.com",
                             "expires": time.time() - DAY}])
        result = preconditions.check("nothing was loaded", tmp_path)
        assert result["checked"] == 0 and result["problems"] == []

    def test_expired_session_is_reported_with_remediation(self, tmp_path):
        _storage(tmp_path, [{"name": "user_session", "domain": "x.com",
                             "expires": time.time() - DAY}])
        result = preconditions.check(
            _log("src/test/resources/mod/loginStorage/Session.json"), tmp_path)
        assert result["checked"] == 1
        problem = result["problems"][0]
        assert problem["kind"] == "session_expired"
        assert "user_session" in problem["detail"]
        assert problem["remediation"]

    def test_missing_session_the_test_asked_for_is_a_problem(self, tmp_path):
        log = "[00:00:49] Session file not found, starting fresh: some/Session.json\n"
        problems = preconditions.check(log, tmp_path)["problems"]
        assert problems[0]["kind"] == "session_missing"

    def test_valid_session_yields_no_problems(self, tmp_path):
        _storage(tmp_path, [{"name": "user_session", "domain": "x.com",
                             "expires": time.time() + DAY}])
        result = preconditions.check(
            _log("src/test/resources/mod/loginStorage/Session.json"), tmp_path)
        assert result["checked"] == 1 and result["problems"] == []
