"""Which tests a change to one area of the product actually reaches.

test-healing-agent never needed this: a stale locator breaks the tests that were
already failing, and the failure list *is* the work. Adapting to a product change
is the opposite problem — the change is known before anything goes red, and the
question is which tests will break when it ships.

Answering it needs an edge the repo does not currently have. `CodeAnalyzer.
get_related_files` walks *forward* from one file and stops at three results: it
is a prompt-context helper, not a graph. What a blast radius needs is the reverse
edge — given `CheckoutPage`, which tests end up executing it, however indirectly.

Three things make this honest rather than merely big:

  * **References, not just imports.** A page object sitting in the same package as
    its helper is used with no import line at all, so an import-only graph misses
    the most common edge in this codebase. Capitalised identifiers in a class body,
    intersected with the set of class names the repo actually defines, catch those.

  * **Hub suppression.** Every test transitively reaches `Element`, `Config` and
    `BasePage`. Propagating backwards through those returns the entire suite,
    which is the same as returning nothing. Types referenced by more files than
    ADAPT_HUB_THRESHOLD do not propagate — but they are *reported with the count
    they would have added*, never silently dropped, because "excluded as a hub" and
    "not related" are different answers and only one of them is a judgement call.

  * **A cost estimate, before anything is spent.** The verify set is the expensive
    part of an adaptation run and it holds the server's single global run slot.
    Printing "14 tests ≈ 42 minutes" is what lets a human stop the run at step 02
    instead of discovering it at step 04.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

from shared.code_analyzer import (CodeAnalyzer, _iter_source_files,
                                  invalidate_tree, read_source, repo_signature,
                                  source_roots)
from shared.code_analyzer import without_comments
from shared.test_catalog import list_tests

# How far a change is allowed to propagate in each direction. Three hops covers
# test -> helper -> page object -> component; beyond that the relationship is
# usually incidental.
MAX_HOPS = int(os.environ.get("ADAPT_BLAST_MAX_HOPS", "3"))

# A type referenced by more files than this is shared infrastructure, not a
# neighbour. Expressed as a floor plus a proportion so it scales with the repo.
#
# Tuned against a real 102-class repo, where the first attempt at 20 was useless:
# TestBase (16 referencing files) and AssertHelper (13) both slipped under it, so
# the backward walk crossed them and returned all 55 tests in the suite. "Every
# test is affected" is the same answer as "no idea". A tenth of the repo is the
# line where a type has stopped being a neighbour and started being plumbing.
_HUB_FLOOR = int(os.environ.get("ADAPT_HUB_THRESHOLD", "8"))
_HUB_RATIO = 0.10

# Types to force back in despite looking like hubs, comma-separated.
_FORCE_INCLUDE = {n.strip() for n in
                  os.environ.get("ADAPT_BLAST_INCLUDE", "").split(",") if n.strip()}

# Rough wall-clock cost of verifying one test, for the budget line.
SECONDS_PER_TEST = int(os.environ.get("ADAPT_SECONDS_PER_TEST", "180"))

# Beyond this the change is bigger than one agent run and must escalate.
MAX_TESTS = int(os.environ.get("ADAPT_BLAST_MAX_TESTS", "40"))

_CAPITALISED = re.compile(r"\b([A-Z]\w*)\b")
_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"' + r"|'(?:\\.|[^'\\])*'")


def _code_only(content: str) -> str:
    """Source with comments and string literals removed.

    Reference extraction without this is worse than useless: `ApiHelper`'s
    javadoc names `GitHubData` as an example, which manufactured an edge from
    the API base class into the GitHub module — and since every module's helper
    extends ApiHelper, that one comment made every API test look related to
    every other one. A mention in prose is not a dependency.
    """
    return _STRING_LITERAL.sub('""', without_comments(content))
_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", re.MULTILINE)

# What a file is, decided from its path and name. Only used to explain the result
# and to pick edit candidates — never to decide reachability.
_ROLE_SUFFIXES = (
    ("Page", "page_object"), ("Screen", "page_object"), ("Component", "page_object"),
    ("View", "page_object"), ("Helper", "helper"), ("Builder", "builder"),
    ("Data", "data"), ("Api", "api"), ("Test", "test"),
)

# Roles a product change can plausibly be *about*. A test reaches plenty of other
# things — base classes, enums, loggers — and those are never what gets edited.
EDITABLE_ROLES = ("page_object", "helper", "builder", "data", "api")

_cache: Dict[str, dict] = {}


def _role(path: Path, simple: str) -> str:
    parts = {p.lower() for p in path.parts}
    if "test" in parts and simple.endswith("Test"):
        return "test"
    for suffix, role in _ROLE_SUFFIXES:
        if simple.endswith(suffix):
            return role
    if "web" in parts or "pages" in parts:
        return "page_object"
    return "other"


def _module_key(package: str) -> str:
    """Which feature area a package belongs to, across both source layouts.

    `automation.saucedemo` (tests) and `automation.modules.saucedemo.web`
    (page objects) are the same area and must key alike, or every page object
    looks like it belongs to a different module than the test that drives it.
    """
    parts = [p for p in (package or "").split(".") if p]
    if parts and parts[0] == "automation":
        parts = parts[1:]
    if parts and parts[0] == "modules":
        parts = parts[1:]
    return parts[0].lower() if parts else "root"


def index(repo_path: str, use_cache: bool = True) -> dict:
    """Every class in the repo, what it references, and what references it.

    One walk of the tree. Cached against `repo_signature` — the same rule
    `test_catalog` uses, so an edited file shows up without a restart but the
    picker does not re-walk on every call.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        raise FileNotFoundError(f"automation repo not found: {repo_path}")

    stamp = repo_signature(str(repo))
    roots = source_roots(str(repo))
    cached = _cache.get(str(repo))
    if use_cache and cached and cached["stamp"] == stamp:
        return cached["payload"]

    invalidate_tree()

    analyzer = CodeAnalyzer()
    classes: Dict[str, dict] = {}
    by_simple: Dict[str, List[str]] = {}
    seen: Set[str] = set()

    for root in roots:
        for path in _iter_source_files(root):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            content = read_source(path)
            if not content:
                continue
            simple = analyzer._extract_class_name(content) or path.stem
            package = analyzer._extract_package(content) or ""
            fqcn = f"{package}.{simple}" if package else simple
            try:
                rel = str(path.relative_to(repo))
            except ValueError:
                rel = str(path)
            classes[fqcn] = {
                "fqcn": fqcn, "simple": simple, "package": package, "path": rel,
                "role": _role(path, simple),
                "imports": _IMPORT.findall(content),
                "_body": content,
                "references": set(),
            }
            by_simple.setdefault(simple, []).append(fqcn)

    # Resolve references only once every class name is known: a mention only
    # counts as an edge if this repo actually defines that type. Without the
    # intersection, every `String`, `List` and `Override` becomes a node.
    defined = set(by_simple)
    for fqcn, entry in classes.items():
        mentioned = set(_CAPITALISED.findall(_code_only(entry.pop("_body")))) & defined
        mentioned.discard(entry["simple"])
        refs: Set[str] = set()
        for name in mentioned:
            candidates = by_simple.get(name, [])
            if len(candidates) == 1:
                refs.add(candidates[0])
                continue
            # Ambiguous simple name: prefer one this file imports, then one in the
            # same package. An unresolvable name is recorded rather than guessed.
            imported = [c for c in candidates if c in entry["imports"]]
            same_pkg = [c for c in candidates
                        if c.rsplit(".", 1)[0] == entry["package"]]
            chosen = (imported or same_pkg or [])
            if chosen:
                refs.add(chosen[0])
        entry["references"] = refs

    referenced_by: Dict[str, Set[str]] = {fqcn: set() for fqcn in classes}
    for fqcn, entry in classes.items():
        for target in entry["references"]:
            referenced_by.setdefault(target, set()).add(fqcn)

    hub_threshold = max(_HUB_FLOOR, int(len(classes) * _HUB_RATIO))
    hubs = {fqcn: len(users) for fqcn, users in referenced_by.items()
            if len(users) > hub_threshold
            and classes.get(fqcn, {}).get("simple") not in _FORCE_INCLUDE}

    # Shared infrastructure, by module locality rather than by raw popularity.
    # A helper used only inside `saucedemo` is part of that feature and may well
    # be what a change edits; one used from `saucedemo`, `github` AND `demo` is
    # plumbing, however few files that adds up to. This catches what the count
    # threshold misses: ApiHelper and BrowserHelper have modest reference counts
    # but sit under every module, so walking back through them turns a
    # two-page change into the whole regression suite.
    infrastructure: Dict[str, List[str]] = {}
    for fqcn, users in referenced_by.items():
        if fqcn not in classes:
            continue
        own = _module_key(classes[fqcn]["package"])
        keys = {_module_key(classes[u]["package"]) for u in users if u in classes}
        keys.discard(own)
        keys.discard("")
        if len(keys) > 1 and classes[fqcn]["simple"] not in _FORCE_INCLUDE:
            infrastructure[fqcn] = sorted(keys)

    payload = {
        "workspace": str(repo),
        "classes": classes,
        "by_simple": by_simple,
        "referenced_by": referenced_by,
        "hubs": hubs,
        "infrastructure": infrastructure,
        "hub_threshold": hub_threshold,
        "total_classes": len(classes),
    }
    _cache[str(repo)] = {"stamp": stamp, "payload": payload}
    return payload


def _walk(seeds: Set[str], edges: Dict[str, Set[str]], hubs: Dict[str, int],
          max_hops: int) -> tuple:
    """Transitive closure that refuses to cross a hub. Returns (reached, blocked)."""
    reached: Set[str] = set(seeds)
    blocked: Dict[str, int] = {}
    frontier = set(seeds)
    for _ in range(max_hops):
        nxt: Set[str] = set()
        for node in frontier:
            for neighbour in edges.get(node, ()):  # type: ignore[arg-type]
                if neighbour in hubs:
                    blocked[neighbour] = hubs[neighbour]
                    continue
                if neighbour not in reached:
                    nxt.add(neighbour)
        if not nxt:
            break
        reached |= nxt
        frontier = nxt
    return reached, blocked


def _match_tests(catalog: dict, affects: List[str], named: List[str],
                 module: str) -> tuple:
    """Test methods selected by glob, explicit name, or module fallback."""
    selected, how = [], ""
    globs = [g.strip() for g in (affects or []) if g.strip()]
    named_set = {n.strip() for n in (named or []) if n.strip()}

    for klass in catalog["classes"]:
        for method in klass["methods"]:
            ident = f"{klass['qualified_name']}#{method['name']}"
            alt = f"{klass['qualified_name']}.{method['name']}"
            if named_set & {ident, alt, klass["qualified_name"], klass["name"],
                            f"{klass['name']}#{method['name']}"}:
                selected.append((klass, method))
                continue
            if any(fnmatch.fnmatch(ident, g) or fnmatch.fnmatch(alt, g)
                   or fnmatch.fnmatch(klass["qualified_name"], g) for g in globs):
                selected.append((klass, method))

    if selected:
        how = "affects glob / named test"
    elif module:
        # A human will omit `Affects:`. Falling back to the module keeps the run
        # possible, but it is a weaker claim and the caller must say so.
        for klass in catalog["classes"]:
            if klass["module"].lower() == module.lower():
                for method in klass["methods"]:
                    selected.append((klass, method))
        how = f"derived from Module: {module} (no Affects given)"
    return selected, how


def resolve(repo_path: str, affects: Optional[List[str]] = None,
            named_tests: Optional[List[str]] = None, module: str = "",
            change_nouns: Optional[List[str]] = None,
            max_hops: int = MAX_HOPS) -> dict:
    """The full blast radius for one change. Never raises on an empty result."""
    catalog = list_tests(repo_path)
    graph = index(repo_path)
    classes, hubs = graph["classes"], graph["hubs"]

    seeds, how = _match_tests(catalog, affects or [], named_tests or [], module)
    seed_classes = {k["qualified_name"] for k, _ in seeds}
    seed_idents = {f"{k['qualified_name']}#{m['name']}" for k, m in seeds}

    # Infrastructure blocks traversal as well as editing: a change to checkout
    # must not reach `payments` by way of the shared API base class.
    impassable = {**hubs,
                  **{f: len(graph["referenced_by"].get(f, ()))
                     for f in graph["infrastructure"]}}
    forward, fwd_blocked = _walk(
        {c for c in seed_classes if c in classes},
        {f: e["references"] for f, e in classes.items()}, impassable, max_hops)

    # Walk backwards from the things that could actually be *edited*, not from
    # everything the seed tests touch. Seeding it from `forward` wholesale was
    # the difference between "6 named + 4 shared" and "6 named + 49 shared":
    # every test extends TestBase, so one step back from TestBase is the entire
    # suite. Nobody edits TestBase to adapt to a checkout redesign.
    # An edit candidate must live in the same feature area as the change. A
    # checkout redesign edits checkout's page objects, helpers and builders — it
    # does not edit the framework's WaitHelper, and treating that as editable is
    # what let a root-package smoke test into a SauceDemo blast radius. If the
    # framework genuinely has to change, that is an escalation, not an edit.
    seed_modules = {_module_key(classes[c]["package"])
                    for c in seed_classes if c in classes}
    editable = {f for f in forward - seed_classes
                if classes[f]["role"] in EDITABLE_ROLES
                and f not in graph["infrastructure"]
                and _module_key(classes[f]["package"]) in seed_modules}
    backward, bwd_blocked = _walk(editable, graph["referenced_by"], impassable, max_hops)
    backward |= seed_classes

    # Only real, enabled test methods can be verified.
    tiers: Dict[str, List[dict]] = {"named": [], "shared_surface": [], "distant": []}
    for klass in catalog["classes"]:
        fq = klass["qualified_name"]
        for method in klass["methods"]:
            if not method["enabled"]:
                continue
            ident = f"{fq}#{method['name']}"
            row = {"test": ident, "path": klass["path"], "is_web": method["is_web"]}
            if ident in seed_idents:
                row["reason"] = how or "named"
                tiers["named"].append(row)
            elif fq in backward and fq not in seed_classes:
                via = sorted((classes[fq]["references"] & editable))[:2]
                row["reason"] = ("via " + ", ".join(classes[v]["simple"] for v in via)
                                 if via else "reaches the changed area")
                tiers["shared_surface"].append(row)

    # Tests that only reach the change through a hub. Reported so "excluded" is a
    # visible decision rather than an absence.
    blocked = {**fwd_blocked, **bwd_blocked}
    for fqcn, keys in graph["infrastructure"].items():
        if fqcn in forward and classes[fqcn]["role"] in EDITABLE_ROLES:
            blocked.setdefault(fqcn, len(graph["referenced_by"].get(fqcn, ())))
    hub_users: Set[str] = set()
    for hub in blocked:
        hub_users |= graph["referenced_by"].get(hub, set())
    for klass in catalog["classes"]:
        fq = klass["qualified_name"]
        if fq in backward or fq in seed_classes or fq not in hub_users:
            continue
        for method in klass["methods"]:
            if method["enabled"]:
                tiers["distant"].append({
                    "test": f"{fq}#{method['name']}", "path": klass["path"],
                    "reason": "only reachable via " + ", ".join(
                        sorted(classes[h]["simple"] for h in blocked)[:3])})

    edit_candidates = [
        {"path": classes[f]["path"], "fqcn": f, "role": classes[f]["role"]}
        for f in sorted(editable)]

    nouns = [n.lower() for n in (change_nouns or []) if n]
    page_object_candidates = [
        c for c in edit_candidates
        if c["role"] == "page_object"
        and (not nouns or any(n in c["fqcn"].lower() for n in nouns))]

    verify = tiers["named"] + tiers["shared_surface"]
    return {
        "workspace": repo_path,
        "selection": how,
        "tiers": tiers,
        "edit_candidates": edit_candidates,
        "page_object_candidates": page_object_candidates,
        "hubs_suppressed": [
            {"type": classes[h]["simple"] if h in classes else h, "fqcn": h,
             "would_have_added": count}
            for h, count in sorted(blocked.items(), key=lambda kv: -kv[1])],
        "hub_threshold": graph["hub_threshold"],
        "total_classes": graph["total_classes"],
        "budget": {
            "tests_to_verify": len(verify),
            "est_seconds": len(verify) * SECONDS_PER_TEST,
            "seconds_per_test": SECONDS_PER_TEST,
            "over_limit": len(verify) > MAX_TESTS,
            "max_tests": MAX_TESTS,
        },
    }


def describe(result: dict) -> str:
    """The blast radius as markdown, for 02-scope.md and the PR body."""
    tiers, budget = result["tiers"], result["budget"]
    lines = [f"**Selection:** {result['selection'] or 'nothing matched'}",
             f"**Scanned:** {result['total_classes']} classes "
             f"(hub threshold {result['hub_threshold']} referencing files)", ""]
    for tier, title in (("named", "Named"), ("shared_surface", "Shared surface"),
                        ("distant", "Distant (excluded)")):
        rows = tiers[tier]
        lines.append(f"### {title} — {len(rows)}")
        if not rows:
            lines.append("_none_")
        for row in rows[:25]:
            lines.append(f"- `{row['test']}` — {row.get('reason', '')}")
        if len(rows) > 25:
            lines.append(f"- …and {len(rows) - 25} more")
        lines.append("")
    if result["hubs_suppressed"]:
        lines.append("### Hubs suppressed")
        for hub in result["hubs_suppressed"][:10]:
            lines.append(f"- `{hub['type']}` — would have pulled in "
                         f"{hub['would_have_added']} referencing file(s)")
        lines.append("")
    mins = round(budget["est_seconds"] / 60)
    lines.append(f"**Budget:** {budget['tests_to_verify']} test(s) to verify "
                 f"≈ {mins} min at {budget['seconds_per_test']}s each"
                 + ("  ⚠️ over ADAPT_BLAST_MAX_TESTS — escalate"
                    if budget["over_limit"] else ""))
    return "\n".join(lines)
