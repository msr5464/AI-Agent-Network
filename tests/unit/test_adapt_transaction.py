"""Tests for the change-item transaction.

The property under test is ordering, and ordering is exactly the kind of thing
that quietly stops being true. A change item spans several files; a failure at
*any* stage — guard, compile, or verification — must leave *every* one of them as
it was, not just the last one written.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load_transaction():
    """Load lib/transaction.py by path, without putting the agent dir on sys.path.

    Three agents ship a `lib` package. Inserting one of their directories at
    module scope makes every later test in the session resolve `lib.*` to that
    agent — which is how adding this file broke test_analyzer.py's
    `from lib.agent.analyzer import ...` while passing perfectly on its own.
    """
    import importlib.util
    path = ROOT / "agents" / "test-adaptation-agent" / "lib" / "transaction.py"
    spec = importlib.util.spec_from_file_location("adapt_transaction", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Transaction


Transaction = _load_transaction()


@pytest.fixture
def files(tmp_path):
    written = {}
    for name, body in (("PageA.java", "class A {\n    String x = \"one\";\n}\n"),
                       ("PageB.java", "class B {\n    String y = \"two\";\n}\n"),
                       ("PageC.java", "class C {\n    String z = \"three\";\n}\n")):
        path = tmp_path / name
        path.write_text(body)
        written[name] = path
    return written


def _edit(path, old, new):
    return {"file": str(path), "old_string": old, "new_string": new}


class TestStaging:
    def test_nothing_is_written_during_staging(self, tmp_path, files):
        txn = Transaction(tmp_path, log=lambda _m: None)
        assert txn.stage([_edit(files["PageA.java"], '"one"', '"ONE"')]) == ""
        assert files["PageA.java"].read_text() == 'class A {\n    String x = "one";\n}\n', (
            "staging computes the result; it must not touch the disk, or a bad "
            "third edit leaves the first two applied")

    def test_a_bad_edit_anywhere_fails_the_whole_item(self, tmp_path, files):
        txn = Transaction(tmp_path, log=lambda _m: None)
        error = txn.stage([
            _edit(files["PageA.java"], '"one"', '"ONE"'),
            _edit(files["PageB.java"], "text that is not there", "x"),
        ])
        assert error and "not found" in error
        assert files["PageA.java"].read_text().count('"one"') == 1

    def test_ambiguous_old_string_is_refused(self, tmp_path):
        path = tmp_path / "Dup.java"
        path.write_text("a();\na();\n")
        txn = Transaction(tmp_path, log=lambda _m: None)
        error = txn.stage([_edit(path, "a();", "b();")])
        assert "not unique" in error, (
            "guessing which occurrence was meant is how an autofix corrupts a file")


class TestApplyAndRollback:
    def test_apply_writes_every_file_and_records_a_snapshot(self, tmp_path, files):
        txn = Transaction(tmp_path, log=lambda _m: None)
        txn.stage([_edit(files["PageA.java"], '"one"', '"ONE"'),
                   _edit(files["PageB.java"], '"two"', '"TWO"')])
        txn.apply()
        assert '"ONE"' in files["PageA.java"].read_text()
        assert '"TWO"' in files["PageB.java"].read_text()
        snapshot = json.loads((tmp_path / ".snapshots.json").read_text())
        assert len(snapshot) == 2, (
            "the snapshot is on disk, not in memory, so run.sh's ERR trap can "
            "restore it when the process dies partway through")

    def test_rollback_restores_every_file_not_just_the_last(self, tmp_path, files):
        before = {n: p.read_text() for n, p in files.items()}
        txn = Transaction(tmp_path, log=lambda _m: None)
        txn.stage([_edit(files["PageA.java"], '"one"', '"ONE"'),
                   _edit(files["PageB.java"], '"two"', '"TWO"'),
                   _edit(files["PageC.java"], '"three"', '"THREE"')])
        txn.apply()
        txn.rollback("verification failed")
        for name, path in files.items():
            assert path.read_text() == before[name], f"{name} was not restored"
        assert not (tmp_path / ".snapshots.json").exists()

    def test_commit_keeps_changes_and_clears_the_snapshot(self, tmp_path, files):
        txn = Transaction(tmp_path, log=lambda _m: None)
        txn.stage([_edit(files["PageA.java"], '"one"', '"ONE"')])
        txn.apply()
        txn.commit()
        assert '"ONE"' in files["PageA.java"].read_text()
        assert not (tmp_path / ".snapshots.json").exists()

    def test_untouched_files_are_never_in_the_snapshot(self, tmp_path, files):
        txn = Transaction(tmp_path, log=lambda _m: None)
        txn.stage([_edit(files["PageA.java"], '"one"', '"ONE"')])
        txn.apply()
        snapshot = json.loads((tmp_path / ".snapshots.json").read_text())
        assert len(snapshot) == 1
        assert str(files["PageC.java"]) not in snapshot


class TestCompileGate:
    def test_a_failing_compiler_is_reported_not_raised(self, tmp_path):
        txn = Transaction(tmp_path, log=lambda _m: None)
        ok, output = txn.compile(tmp_path, "false")
        assert ok is False and isinstance(output, str)

    def test_a_missing_compiler_is_reported_not_raised(self, tmp_path):
        txn = Transaction(tmp_path, log=lambda _m: None)
        ok, output = txn.compile(tmp_path, "definitely-not-a-real-command-xyz")
        assert ok is False and "could not run the compiler" in output

    def test_a_passing_compiler_reports_ok(self, tmp_path):
        txn = Transaction(tmp_path, log=lambda _m: None)
        ok, output = txn.compile(tmp_path, "true")
        assert ok is True and output == ""


class TestRestoreScript:
    def test_the_err_trap_script_restores_from_the_snapshot(self, tmp_path, files,
                                                            monkeypatch):
        """A crash mid-item must not leave the shared checkout dirty."""
        txn = Transaction(tmp_path, log=lambda _m: None)
        before = files["PageA.java"].read_text()
        txn.stage([_edit(files["PageA.java"], '"one"', '"ONE"')])
        txn.apply()                                   # process "dies" here

        monkeypatch.setenv("AUDIT_DIR", str(tmp_path))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "restore", ROOT / "agents" / "test-adaptation-agent" / "actions"
            / "restore_snapshots.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.main() == 0
        assert files["PageA.java"].read_text() == before
