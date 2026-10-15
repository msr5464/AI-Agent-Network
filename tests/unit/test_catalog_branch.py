"""Tests for listing the tests of a branch that is not the one checked out.

The picker used to read the automation repo's working tree whatever branch the
run was about to be based on. With auto-push on, that run executes in a worktree
cut from origin/<base>, so the panel offered tests the run would not find and
hid the ones it would. Nothing failed: a list of the wrong branch's tests looks
exactly like a list of the right one's.

Two things are held here. The catalogue of a named branch is that branch's, for
every agent that has a catalogue. And reading it leaves the checkout exactly
where it was, because a developer, or a run with auto-push off, may be standing
in it.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qa_agents_server import routes
from shared import blast_radius, code_analyzer, test_catalog, workspace

CATALOGUE_AGENTS = ["test-healing-agent", "test-adaptation-agent"]
OTHER_BRANCH = "feature/checkout"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _test_class(repo: Path, name: str, methods=("first",), check: str = ""):
    path = repo / "src" / "test" / "java" / "automation" / "demo" / f"{name}.java"
    path.parent.mkdir(parents=True, exist_ok=True)
    statement = f'Assert.assertEquals(actual(), "ok", "{check}");' if check else ""
    body = "\n".join(
        f'    @Test(description = "{m}", groups = {{"web"}})\n'
        f'    public void {m}() {{ {statement} }}\n'
        for m in methods
    )
    path.write_text(textwrap.dedent(f"""\
        package automation.demo;

        import org.testng.Assert;
        import org.testng.annotations.Test;

        public class {name} {{
        """) + body + "}\n")


def _commit(repo: Path, message: str):
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", message, cwd=repo)


def _head(repo: Path, ref: str = "HEAD") -> str:
    return _git("rev-parse", ref, cwd=repo).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An automation repo on `main`, with a second branch that adds a test class.

    GitHub is left unconfigured, so the branch is resolved from the refs on
    disk — no network, no credentials, and the same code path a developer
    without a token takes.
    """
    for name in ("FRAMEWORK_DIR", "PAGE_OBJECT_DIRS", "GITHUB_ORG", "GITHUB_TOKEN",
                 "AUDIT_DIR"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "automation"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@t", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _test_class(root, "AlphaTest", ("one", "two"))
    _commit(root, "init")
    _git("checkout", "-q", "-b", OTHER_BRANCH, cwd=root)
    _test_class(root, "CheckoutTest", ("pay",), check="Payment should be accepted")
    _commit(root, "checkout flow")
    _git("checkout", "-q", "main", cwd=root)

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPO_AUTOMATION", "automation")
    monkeypatch.setattr(routes, "_snapshot_root", lambda: tmp_path / "snapshots")
    _forget()
    yield root
    _forget()


def _forget():
    code_analyzer.reset_caches()
    test_catalog._cache.clear()
    blast_radius._cache.clear()
    routes._BRANCH_TIPS.clear()
    routes._REPO_STATE.clear()
    routes._MEMBER_INDEX.clear()


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(routes.qa_bp)
    return app.test_client()


def _classes(response) -> list:
    return sorted(c["name"] for c in response.get_json()["classes"])


@pytest.mark.parametrize("agent", CATALOGUE_AGENTS)
def test_without_a_branch_the_checkout_is_listed(repo, client, agent):
    """The auto-push-off case, and every caller that predates the parameter."""
    response = client.get(f"/agents/{agent}/tests")
    assert response.status_code == 200
    assert _classes(response) == ["AlphaTest"]
    body = response.get_json()
    assert (body["branch"], body["source"]) == ("main", "checkout")
    assert body["workspace"] == str(repo)


@pytest.mark.parametrize("agent", CATALOGUE_AGENTS)
def test_a_named_branch_lists_its_own_tests(repo, client, agent):
    """The reported bug: a class that exists only on the base branch was missing."""
    response = client.get(f"/agents/{agent}/tests?branch={OTHER_BRANCH}")
    assert response.status_code == 200
    assert _classes(response) == ["AlphaTest", "CheckoutTest"]
    body = response.get_json()
    assert (body["branch"], body["source"], body["dirty"]) == (OTHER_BRANCH, "branch", False)
    assert body["sha"] == _head(repo, OTHER_BRANCH)
    # The repo, not wherever its files were unpacked.
    assert body["workspace"] == str(repo)


@pytest.mark.parametrize("agent", CATALOGUE_AGENTS)
def test_reading_a_branch_leaves_the_checkout_alone(repo, client, agent):
    """A developer, or a run with auto-push off, may be working in it."""
    (repo / "notes.txt").write_text("uncommitted\n")
    before = (_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout,
              _head(repo), _git("status", "--porcelain", cwd=repo).stdout)

    assert client.get(f"/agents/{agent}/tests?branch={OTHER_BRANCH}").status_code == 200

    after = (_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout,
             _head(repo), _git("status", "--porcelain", cwd=repo).stdout)
    assert after == before
    worktrees = _git("worktree", "list", "--porcelain", cwd=repo).stdout
    assert worktrees.count("worktree ") == 1, worktrees
    assert not list(repo.rglob(workspace.BASE_MARKER)), (
        "a catalogue read recorded itself as a run's base branch")
    # ...and the checkout's own catalogue is still the checkout's.
    assert _classes(client.get(f"/agents/{agent}/tests")) == ["AlphaTest"]


def test_a_branch_that_does_not_exist_is_an_answer_not_a_crash(repo, client):
    response = client.get("/agents/test-adaptation-agent/tests?branch=no/such-branch")
    assert response.status_code == 404
    body = response.get_json()
    assert body["error"] and "no/such-branch" in body["detail"]


@pytest.mark.parametrize("bad", ["--upload-pack=x", "a..b", "-rf", "a b"])
def test_a_branch_name_git_would_read_as_something_else_is_refused(repo, client, bad):
    response = client.get("/agents/test-adaptation-agent/tests",
                          query_string={"branch": bad})
    assert response.status_code == 400
    assert response.get_json()["error"].startswith("branch:")


def test_a_new_commit_appears_once_the_remembered_tip_expires(repo, client, monkeypatch):
    url = f"/agents/test-healing-agent/tests?branch={OTHER_BRANCH}"
    assert _classes(client.get(url)) == ["AlphaTest", "CheckoutTest"]

    _git("checkout", "-q", OTHER_BRANCH, cwd=repo)
    _test_class(repo, "RefundTest", ("refund",))
    _commit(repo, "refunds")
    _git("checkout", "-q", "main", cwd=repo)

    # Inside the window the tip is not asked for again — that is the point of it.
    assert _classes(client.get(url)) == ["AlphaTest", "CheckoutTest"]

    monkeypatch.setattr(routes, "_BRANCH_CACHE_TTL", 0.0)
    assert _classes(client.get(url)) == ["AlphaTest", "CheckoutTest", "RefundTest"]


def test_intent_is_read_from_the_same_branch_as_the_pickers(repo, client):
    test = "automation.demo.CheckoutTest%23pay"
    base = f"/agents/test-adaptation-agent/tests/intent?test={test}"

    on_branch = client.get(f"{base}&branch={OTHER_BRANCH}")
    assert on_branch.status_code == 200
    assert on_branch.get_json()["verifies"] == ["Payment should be accepted"]

    # The checkout has no such class, so read from there the same test proves
    # nothing — an empty answer that looks like a real one, which is what the
    # pane showed beside a picker that had been filled from another branch.
    in_checkout = client.get(base)
    assert in_checkout.status_code == 200
    assert in_checkout.get_json()["verifies"] == []

    assert client.get(f"{base}&branch=no/such-branch").status_code == 404


@pytest.mark.parametrize("agent", CATALOGUE_AGENTS)
def test_a_workspace_that_is_not_a_git_checkout_still_lists_its_tests(
        tmp_path, monkeypatch, client, agent):
    """It has no branches to read, and its tests are still worth offering."""
    for name in ("FRAMEWORK_DIR", "PAGE_OBJECT_DIRS", "GITHUB_ORG", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    _test_class(plain, "AlphaTest", ("one",))
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPO_AUTOMATION", "plain")
    monkeypatch.setattr(routes, "_snapshot_root", lambda: tmp_path / "snapshots")
    _forget()
    try:
        response = client.get(f"/agents/{agent}/tests?branch=main")
        assert response.status_code == 200
        assert _classes(response) == ["AlphaTest"]
        assert response.get_json()["source"] == "checkout"
    finally:
        _forget()


@pytest.mark.parametrize("query", ["", f"?branch={OTHER_BRANCH}"])
def test_an_agent_without_a_catalogue_has_none_on_any_branch(repo, client, query):
    """Authoring picks a module file, not a test; the parameter changes nothing."""
    assert client.get(f"/agents/test-authoring-agent/tests{query}").status_code == 404
    assert not (repo.parent / "snapshots").exists()


def test_only_the_most_recently_used_snapshots_are_kept(repo, client, monkeypatch):
    monkeypatch.setattr(routes, "_SNAPSHOT_KEEP", 2)
    monkeypatch.setattr(routes, "_BRANCH_CACHE_TTL", 0.0)
    url = f"/agents/test-healing-agent/tests?branch={OTHER_BRANCH}"
    _git("checkout", "-q", OTHER_BRANCH, cwd=repo)
    for n in range(4):
        _test_class(repo, f"Extra{n}Test", ("go",))
        _commit(repo, f"extra {n}")
        assert client.get(url).status_code == 200
    newest = _head(repo)
    _git("checkout", "-q", "main", cwd=repo)

    kept = sorted(d.name for d in (repo.parent / "snapshots").iterdir())
    assert len(kept) == 2 and newest in kept, kept
    assert set(routes._REPO_STATE) <= {str(repo.parent / "snapshots" / k) for k in kept}
    # The survivor is still readable after the caches under it were dropped.
    assert "Extra3Test" in _classes(client.get(url))


class TestExportTree:
    def test_writes_the_tree_of_that_commit(self, repo, tmp_path):
        dest = tmp_path / "out" / "tree"
        result = workspace.export_tree(repo, _head(repo, OTHER_BRANCH), dest)
        assert result == {"ok": True, "reason": ""}
        assert (dest / "src/test/java/automation/demo/CheckoutTest.java").is_file()
        assert not (dest / ".git").exists()
        assert [p.name for p in dest.parent.iterdir()] == ["tree"], "staging left behind"

    @pytest.mark.parametrize("not_a_sha", ["", "main", "HEAD", "--output=/tmp/x", "abc123"])
    def test_takes_a_commit_id_and_nothing_else(self, repo, tmp_path, not_a_sha):
        dest = tmp_path / "out" / "tree"
        result = workspace.export_tree(repo, not_a_sha, dest)
        assert not result["ok"] and "not a commit id" in result["reason"]
        assert not dest.parent.exists()

    def test_a_commit_the_repo_does_not_have_leaves_nothing_behind(self, repo, tmp_path):
        dest = tmp_path / "out" / "tree"
        result = workspace.export_tree(repo, "0" * 40, dest)
        assert not result["ok"] and result["reason"]
        assert not dest.exists()
        assert list(dest.parent.iterdir()) == [], "a failed export left a partial tree"
