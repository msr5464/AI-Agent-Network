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

BASELINE_DIR = "src/main/resources/baselines"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    directory = tmp_path / BASELINE_DIR
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
    path = repo / BASELINE_DIR / "LoginPage.json"
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
    (repo / BASELINE_DIR / "CartPage.json").write_text(
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
    assert BASELINE_DIR in committed
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
    pending = repo / BASELINE_DIR / "pending"
    pending.mkdir()
    (pending / "SomeTest.someMethod__LoginPage.json").write_text(
        json.dumps({"pageObject": "LoginPage", "recordedAt": "2026-06-01T00:00:00"}))
    assert commit_baselines.changed_baselines(repo) == []
