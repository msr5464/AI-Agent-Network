"""The baseline commit job must not churn a PR on every green build."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import commit_baselines  # noqa: E402

HEALING_BASELINE_DIR = "src/main/resources/baselines"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    directory = tmp_path / HEALING_BASELINE_DIR
    directory.mkdir(parents=True)
    (directory / "LoginPage.json").write_text(json.dumps({
        "pageObject": "LoginPage", "recordedAt": "2026-01-01T00:00:00",
        "coverage": {"loginButton": 1},
        "fingerprints": {"loginButton": {"tag": "button", "id": "login"}},
    }, indent=2))
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "seed"], tmp_path)
    return tmp_path


def _rewrite(repo, **changes):
    path = repo / HEALING_BASELINE_DIR / "LoginPage.json"
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data, indent=2))


def test_a_new_timestamp_alone_is_not_a_change(repo):
    """Two green runs differ only in recordedAt. Committing that on every build
    would put a diff in every PR for no information at all."""
    _rewrite(repo, recordedAt="2026-06-01T12:00:00")
    assert commit_baselines.changed_baselines(repo) == []


def test_a_changed_fingerprint_is_a_change(repo):
    _rewrite(repo, recordedAt="2026-06-01T12:00:00",
             fingerprints={"loginButton": {"tag": "a", "id": "login"}})
    changed = commit_baselines.changed_baselines(repo)
    assert [p.name for p in changed] == ["LoginPage.json"]


def test_a_new_page_object_is_a_change(repo):
    (repo / HEALING_BASELINE_DIR / "CartPage.json").write_text(
        json.dumps({"pageObject": "CartPage", "recordedAt": "2026-06-01T00:00:00"}))
    assert [p.name for p in commit_baselines.changed_baselines(repo)] == ["CartPage.json"]


def test_it_stages_only_the_baseline_directory(repo):
    """This job holds a write token; it has no business touching source."""
    source = repo / "src/main/java/Foo.java"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("class Foo {}")
    _rewrite(repo, fingerprints={"loginButton": {"tag": "a"}})

    done = subprocess.run([sys.executable, str(REPO / "scripts/commit_baselines.py"),
                           str(repo)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr

    committed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                               cwd=repo, capture_output=True, text=True).stdout
    assert HEALING_BASELINE_DIR in committed
    assert "Foo.java" not in committed, "source must never be staged by this job"


def test_it_never_fails_the_build(tmp_path):
    """A directory that is not a git tree is a no-op, not an error."""
    done = subprocess.run([sys.executable, str(REPO / "scripts/commit_baselines.py"),
                           str(tmp_path)], capture_output=True, text=True)
    assert done.returncode == 0


def test_a_pending_fingerprint_is_never_committed(repo):
    """`pending/` is the Java side's scratch space: record() writes there and
    promote()/discard() empties it. A file left behind is an interrupted run,
    not a good-run fingerprint, and its name is a test key rather than a page
    object — exactly the record a later diagnosis must not compare against."""
    pending = repo / HEALING_BASELINE_DIR / "pending"
    pending.mkdir()
    (pending / "SomeTest.someMethod__LoginPage.json").write_text(
        json.dumps({"pageObject": "LoginPage", "recordedAt": "2026-06-01T00:00:00"}))
    assert commit_baselines.changed_baselines(repo) == []


# ── The same answer, asked by the ship step ───────────────────────────────────
#
# The authoring agent's PR used to carry a generated page object and no
# fingerprint for it: the framework wrote NaukriLoginPage.json during the green
# run, and 05_ship's `checkout -f -B` wiped the untracked file off disk before
# there was a branch to commit it onto.

from shared import baseline  # noqa: E402


def test_a_baseline_read_before_the_checkout_survives_it(repo):
    """What ship actually does: read the files first, compare after branching.

    The comparison must work from content the caller kept, because by the time it
    runs the files are no longer on disk.
    """
    new = repo / HEALING_BASELINE_DIR / "NaukriLoginPage.json"
    new.write_text(json.dumps({"pageObject": "NaukriLoginPage",
                               "recordedAt": "2026-09-05T15:23:39",
                               "coverage": {"usernameField": 1}}))
    captured = {path.relative_to(repo).as_posix(): path.read_text()
                for path in baseline.promoted(repo)}

    _git(["checkout", "-f", "-B", "feature/new"], repo)   # what ship does next
    _git(["clean", "-fd", "--", HEALING_BASELINE_DIR], repo)
    assert not new.exists()

    changed = baseline.changed(repo, captured)
    assert list(changed) == [f"{HEALING_BASELINE_DIR}/NaukriLoginPage.json"]
    assert "usernameField" in changed[f"{HEALING_BASELINE_DIR}/NaukriLoginPage.json"]


def test_pending_fingerprints_are_never_committed(repo):
    """`pending/` holds records from a test that has not finished — an
    interrupted run, named by test key, never proof a page worked."""
    pending = repo / HEALING_BASELINE_DIR / "pending"
    pending.mkdir()
    (pending / "SomeTest_120000.json").write_text('{"pageObject": "SomeTest"}')
    assert baseline.changed(repo) == {}


def test_build_output_is_never_a_commit_target(tmp_path):
    """`directory()` also resolves to test-output/baselines. Staging that would
    commit build output into the repo."""
    (tmp_path / "test-output" / "baselines").mkdir(parents=True)
    (tmp_path / "test-output" / "baselines" / "LoginPage.json").write_text("{}")
    assert baseline.repo_directory(tmp_path) is None
    assert baseline.promoted(tmp_path) == []


def test_only_the_timestamp_changing_is_not_a_commit(repo):
    """Same guard as the CI job, reached through the shared helper."""
    _rewrite(repo, recordedAt="2026-09-05T15:23:39")
    assert baseline.changed(repo) == {}


# ── Every agent that raises a PR commits them ─────────────────────────────────
#
# Three separate commit paths, one shared answer. These pin the wiring, because
# the failure mode is silent: the PR looks complete, and the missing fingerprint
# is only noticed months later by a diagnosis that has nothing to compare against.

AGENT_COMMIT_PATHS = [
    "agents/test-authoring-agent/actions/05_ship.py",
    "agents/test-adaptation-agent/actions/05_ship.py",
    "agents/test-healing-agent/actions/01_fix.py",
]


@pytest.mark.parametrize("relative", AGENT_COMMIT_PATHS)
def test_every_pr_raising_agent_commits_baselines(relative):
    source = (REPO / relative).read_text()
    assert "baseline" in source and "changed(" in source, (
        f"{relative} builds a PR without committing the locator baselines the "
        f"run recorded")


@pytest.mark.parametrize("relative", AGENT_COMMIT_PATHS)
def test_no_agent_stages_everything(relative):
    """These steps hold a write token. Staging the whole tree would sweep up a
    minted login session or a developer's scratch file along with the fix."""
    source = (REPO / relative).read_text()
    assert '"add", "-A"' not in source and "'add', '-A'" not in source


def test_authoring_ship_reads_baselines_before_it_branches():
    """The ordering IS the fix: 05_ship's branch creation is a `checkout -f`, so
    reading the untracked baselines afterwards would find nothing there."""
    source = (REPO / "agents/test-authoring-agent/actions/05_ship.py").read_text()
    read_at = source.index("baseline_store.promoted(")
    branch_at = source.index("workspace_helper.checkout_base(")
    commit_at = source.index("baseline_store.changed(")
    assert read_at < branch_at < commit_at
