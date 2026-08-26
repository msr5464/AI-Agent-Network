"""Tests for cause-aware clustering in lib/failure_clusters.py.

The behaviour that matters: a build where one environment outage fails thirty
tests must produce one unit of work, not thirty. Under the old key those thirty
carried thirty different elements, so they each consumed a slot and the run then
truncated most of them into "deferred" — thirty reports of nothing useful.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def clusters():
    """Import the agent's lib under its own name (both agents ship a `lib`)."""
    import importlib.util
    saved_path, saved = list(sys.path), {
        n: m for n, m in sys.modules.items() if n == "lib" or n.startswith("lib.")}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(ROOT / "agents" / "test-healing-agent"))
    try:
        spec = importlib.util.spec_from_file_location(
            "failure_clusters",
            ROOT / "agents" / "test-healing-agent" / "lib" / "failure_clusters.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name in [n for n in sys.modules if n == "lib" or n.startswith("lib.")]:
            del sys.modules[name]
        sys.modules.update(saved)
        sys.path[:] = saved_path


def _ctx(test, element, page_object, verdict=None):
    context = {"test_name": test, "element_names": [element],
               "failed_selector": f"#{element}",
               "page_objects": [{"path": page_object}]}
    if verdict:
        context["diagnosis"] = {"verdict": verdict}
    return context


class TestCauseAwareKeys:
    def test_same_element_same_page_object_is_one_fix(self, clusters):
        a = _ctx("T1", "avatar", "Dashboard.java", "LOCATOR_STALE")
        b = _ctx("T2", "avatar", "Dashboard.java", "LOCATOR_STALE")
        assert clusters.cluster_key(a) == clusters.cluster_key(b)

    def test_same_element_different_page_objects_stay_apart(self, clusters):
        a = _ctx("T1", "avatar", "Dashboard.java", "LOCATOR_STALE")
        b = _ctx("T2", "avatar", "Settings.java", "LOCATOR_STALE")
        assert clusters.cluster_key(a) != clusters.cluster_key(b)

    def test_one_outage_across_many_elements_is_one_cause(self, clusters):
        keys = {clusters.cluster_key(
            _ctx(f"T{i}", f"element{i}", f"Page{i}.java", "ENV_UNREACHABLE"))
            for i in range(30)}
        assert len(keys) == 1

    def test_different_causes_do_not_merge(self, clusters):
        a = _ctx("T1", "x", "A.java", "ENV_UNREACHABLE")
        b = _ctx("T2", "x", "A.java", "WRONG_PAGE")
        assert clusters.cluster_key(a) != clusters.cluster_key(b)

    def test_abstention_falls_back_to_the_original_key(self, clusters):
        undiagnosed = _ctx("T1", "avatar", "Dashboard.java")
        abstained = _ctx("T2", "avatar", "Dashboard.java", "INSUFFICIENT_EVIDENCE")
        assert clusters.cluster_key(undiagnosed) == clusters.cluster_key(abstained)


class TestEvidenceRank:
    def test_a_diagnosed_member_represents_its_cluster(self, clusters):
        diagnosed = _ctx("T1", "avatar", "A.java", "WRONG_PAGE")
        rich = _ctx("T2", "avatar", "A.java")
        rich["dom_snapshot_path"] = "/tmp/x.html"
        rich["trace_timeline"] = "..."
        assert clusters.evidence_rank(diagnosed) > clusters.evidence_rank(rich)

    def test_artefacts_still_rank_among_undiagnosed_members(self, clusters):
        withdom = _ctx("T1", "a", "A.java")
        withdom["dom_snapshot_path"] = "/tmp/x.html"
        bare = _ctx("T2", "a", "A.java")
        assert clusters.evidence_rank(withdom) > clusters.evidence_rank(bare)
