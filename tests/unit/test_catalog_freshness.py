"""Tests for reading the automation repo as it is *now*.

Every case here failed before the caches learned to notice their own staleness,
and each failed silently: the picker kept offering a test method that had been
deleted, and the only cure was restarting the server. A catalogue that is merely
out of date looks exactly like a correct one, so the guard has to be a test
rather than a careful reading of the code.

The shapes that matter are edit, delete and add — one per cache layer. An edit
is answered by read_source's per-file check; a delete and an add are invisible
to any per-file check and only clear when the tree-wide caches are dropped.
"""

import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared import blast_radius, code_analyzer, test_catalog


def _test_class(path: Path, name: str, methods=("first",)):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        f'    @Test(description = "{m}")\n    public void {m}() {{ }}\n'
        for m in methods
    )
    path.write_text(textwrap.dedent(f"""\
        package automation.demo;

        import org.testng.annotations.Test;

        public class {name} {{
        """) + body + "}\n")


def _bump_mtime(path: Path, seconds: int = 2):
    """Push a file's mtime forward.

    A rewrite that keeps the same byte count on a filesystem with coarse
    timestamps can land on the same (mtime, size) as the original, which would
    make this suite depend on how fast the machine running it is. The production
    signal is unchanged; only the test's timing is made deterministic.
    """
    stamp = path.stat().st_mtime_ns + seconds * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature automation repo, with the caches empty at the start."""
    monkeypatch.delenv("PAGE_OBJECT_DIRS", raising=False)
    tests = tmp_path / "src" / "test" / "java" / "automation" / "demo"
    _test_class(tests / "AlphaTest.java", "AlphaTest", ("one", "two"))
    _test_class(tests / "BetaTest.java", "BetaTest", ("only",))
    code_analyzer.reset_caches()
    test_catalog._cache.clear()
    blast_radius._cache.clear()
    yield tmp_path
    code_analyzer.reset_caches()
    test_catalog._cache.clear()
    blast_radius._cache.clear()


def _methods(repo_path: Path, class_name: str):
    catalog = test_catalog.list_tests(str(repo_path))
    for entry in catalog["classes"]:
        if entry["name"] == class_name:
            return sorted(m["name"] for m in entry["methods"])
    return None


def test_deleted_method_disappears_from_the_catalogue(repo):
    """The reported bug: a method removed from a file kept being offered."""
    assert _methods(repo, "AlphaTest") == ["one", "two"]

    _test_class(repo / "src/test/java/automation/demo/AlphaTest.java",
                "AlphaTest", ("one",))
    _bump_mtime(repo / "src/test/java/automation/demo/AlphaTest.java")

    assert _methods(repo, "AlphaTest") == ["one"]


def test_added_method_appears(repo):
    _test_class(repo / "src/test/java/automation/demo/BetaTest.java",
                "BetaTest", ("only", "extra"))
    _bump_mtime(repo / "src/test/java/automation/demo/BetaTest.java")

    assert _methods(repo, "BetaTest") == ["extra", "only"]


def test_deleted_file_disappears(repo):
    """A max-mtime stamp cannot see this: the newest file is the one that stays."""
    assert _methods(repo, "BetaTest") == ["only"]

    (repo / "src/test/java/automation/demo/BetaTest.java").unlink()

    assert _methods(repo, "BetaTest") is None
    assert _methods(repo, "AlphaTest") == ["one", "two"]


def test_new_file_appears(repo):
    assert _methods(repo, "GammaTest") is None

    _test_class(repo / "src/test/java/automation/demo/GammaTest.java",
                "GammaTest", ("fresh",))

    assert _methods(repo, "GammaTest") == ["fresh"]


def test_read_source_reflects_a_rewrite_without_being_told(repo):
    """invalidate_file() is belt-and-braces now, not the only line of defence."""
    path = repo / "src/test/java/automation/demo/AlphaTest.java"
    assert "public void two()" in code_analyzer.read_source(path)

    _test_class(path, "AlphaTest", ("one",))
    _bump_mtime(path)

    assert "public void two()" not in code_analyzer.read_source(path)


def test_signature_changes_for_edits_adds_and_deletes(repo):
    path = repo / "src/test/java/automation/demo/AlphaTest.java"
    original = code_analyzer.repo_signature(str(repo))

    assert code_analyzer.repo_signature(str(repo)) == original, "must be stable"

    _test_class(path, "AlphaTest", ("one",))
    _bump_mtime(path)
    edited = code_analyzer.repo_signature(str(repo))
    assert edited != original

    _test_class(repo / "src/test/java/automation/demo/GammaTest.java",
                "GammaTest", ("fresh",))
    added = code_analyzer.repo_signature(str(repo))
    assert added != edited

    (repo / "src/test/java/automation/demo/GammaTest.java").unlink()
    assert code_analyzer.repo_signature(str(repo)) == edited


def test_page_object_edit_moves_the_stamp(repo):
    """src/main/java is in scope: the intent panel's index reads page objects."""
    page = repo / "src" / "main" / "java" / "automation" / "demo" / "DemoPage.java"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("package automation.demo;\n\npublic class DemoPage { }\n")
    before = test_catalog.source_stamp(str(repo))

    page.write_text("package automation.demo;\n\npublic class DemoPage { int x; }\n")
    _bump_mtime(page)

    assert test_catalog.source_stamp(str(repo)) != before
