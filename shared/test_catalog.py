"""Catalogue the test classes and methods in an automation repo.

Feeds the Auto-Heal Tests picker so nobody has to type
`SauceDemoWebTest#loginAndVerifyProductsPage` from memory — a typo there costs a
full reproduce run before the agent can tell you the file does not exist.

Read straight from source rather than from TestNG: a static scan needs no build,
no running suite, and covers classes the suite file happens not to list (which
are still runnable, because the agent invokes `-Dtest=Class#method`).
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from shared.code_analyzer import (
    CodeAnalyzer,
    _iter_source_files,
    read_source,
    source_roots,
    split_class_members,
    without_comments,
)

# Group constants are compile-time finals (TestBase.java), so a source scan sees
# the identifier, not the value.
_GROUP_VALUES = {
    "GROUP_WEB": "webCases",
    "GROUP_API": "apiCases",
    "GROUP_REGRESSION": "regression",
    "GROUP_MOBILE": "mobileCases",
}
_WEB_MARKERS = {"webcases", "web"}
# The adaptation agent explores an API half as well as a web one, and picks which
# from the note's `Type:`. Exposing this alongside is_web lets the UI derive that
# header from the tests the user picked instead of asking, and keeps the marker
# vocabulary in one place rather than half here and half in the frontend.
_API_MARKERS = {"apicases", "api"}

# Only used to present a package segment the way the framework spells it.
_PROJECT_NAMES = ("GitHub", "SauceDemo", "FullSuite")

_TEST_ANNOTATION = re.compile(r'@Test\b')
_ARG_DESCRIPTION = re.compile(r'description\s*=\s*"((?:[^"\\]|\\.)*)"')
_ARG_GROUPS = re.compile(r'groups\s*=\s*\{([^}]*)\}')
_ARG_ENABLED = re.compile(r'enabled\s*=\s*(\w+)')
_ARG_DATAPROVIDER = re.compile(r'dataProvider\s*=\s*"([^"]*)"')

_cache: Dict[str, dict] = {}


def test_source_roots(repo_path: str) -> List[Path]:
    """Only the roots that hold tests.

    source_roots() also returns src/main/java, whose framework classes mention
    "@Test" in comments — enough to look like test classes if the whole tree is
    scanned.
    """
    roots = source_roots(repo_path)
    test_roots = [r for r in roots if "test" in r.parts]
    return test_roots or roots


def _annotation_args(member_text: str) -> str:
    """The raw argument text of the member's @Test annotation, or ''."""
    match = _TEST_ANNOTATION.search(member_text)
    if not match:
        return ""
    i = match.end()
    while i < len(member_text) and member_text[i] in " \t\n\r":
        i += 1
    if i >= len(member_text) or member_text[i] != "(":
        return ""          # bare @Test, no arguments
    depth, start = 0, i
    while i < len(member_text):
        if member_text[i] == "(":
            depth += 1
        elif member_text[i] == ")":
            depth -= 1
            if depth == 0:
                return member_text[start + 1:i]
        i += 1
    return ""


def _resolve_groups(raw: str) -> List[str]:
    groups = []
    for token in raw.split(","):
        token = token.strip().strip('"')
        if not token:
            continue
        groups.append(_GROUP_VALUES.get(token, token))
    return groups


def module_for_package(package: str) -> str:
    """The segment after `automation.`, spelled the way the framework spells it.

    `automation.saucedemo.SauceDemoWebTest` → `SauceDemo`. Classes sitting
    directly in the root package have no segment and group under `Other`.
    """
    parts = [p for p in (package or "").split(".") if p]
    if len(parts) < 2:
        return "Other"
    segment = parts[1]
    for canonical in _PROJECT_NAMES:
        if canonical.lower() == segment.lower():
            return canonical
    return segment


def test_methods_in(source: str) -> List[str]:
    """Names of the @Test methods declared in one Java/Kotlin source, in order.

    Comment-aware, unlike a bare regex: `// auto-wire retry on every @Test` above
    an ordinary helper must not make it look like a test method. Shared because
    both the authoring agent's generate step (which must report the method it
    actually produced) and its run step (which must not hand `mvn -Dtest` a name
    that does not exist) need exactly this answer — and disagreeing about it means
    surefire runs nothing and calls it BUILD SUCCESS.
    """
    names = []
    for member in split_class_members(source or ""):
        if member["kind"] not in ("method", "method_decl") or not member["name"]:
            continue
        if _TEST_ANNOTATION.search(without_comments(member["text"])):
            names.append(member["name"])
    return names


def _class_entry(path: Path, repo: Path) -> Optional[dict]:
    content = read_source(path)
    if not content:
        return None
    if not _TEST_ANNOTATION.search(without_comments(content)):
        return None

    analyzer = CodeAnalyzer()
    package = analyzer._extract_package(content) or ""
    class_name = analyzer._extract_class_name(content) or path.stem

    methods = []
    for member in split_class_members(content):
        if member["kind"] not in ("method", "method_decl") or not member["name"]:
            continue
        declaration = without_comments(member["text"])
        if not _TEST_ANNOTATION.search(declaration):
            continue

        args = _annotation_args(declaration)
        groups = _resolve_groups(_ARG_GROUPS.search(args).group(1)) if _ARG_GROUPS.search(args) else []
        enabled_match = _ARG_ENABLED.search(args)
        description = _ARG_DESCRIPTION.search(args)
        provider = _ARG_DATAPROVIDER.search(args)

        methods.append({
            "name": member["name"],
            "description": (description.group(1) if description else "").strip(),
            "groups": groups,
            "enabled": not (enabled_match and enabled_match.group(1) == "false"),
            # No groups at all means a plain unit test, not a browser test —
            # absent is not unknown here.
            "is_web": any(g.lower() in _WEB_MARKERS for g in groups),
            "is_api": any(g.lower() in _API_MARKERS for g in groups),
            "data_provider": provider.group(1) if provider else "",
        })

    if not methods:
        return None

    methods.sort(key=lambda m: m["name"])
    return {
        "module": module_for_package(package),
        "package": package,
        "name": class_name,
        "qualified_name": f"{package}.{class_name}" if package else class_name,
        "path": str(path.relative_to(repo)),
        "methods": methods,
        "web_count": sum(1 for m in methods if m["is_web"]),
        "api_count": sum(1 for m in methods if m["is_api"]),
    }


def _newest_mtime(roots: List[Path]) -> float:
    newest = 0.0
    for root in roots:
        for path in _iter_source_files(root):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
    return newest


def source_stamp(repo_path: str) -> float:
    """The newest mtime across the repo's test sources.

    Public so callers outside this module can key their own caches on the same
    signal `list_tests` uses, instead of re-walking the tree or inventing a
    second, differently-wrong notion of "has anything changed".
    """
    return _newest_mtime(test_source_roots(repo_path))


def list_tests(repo_path: str, use_cache: bool = True) -> dict:
    """Every test class in the repo, with its methods and their @Test metadata.

    Cached against the newest source mtime, so reopening the picker does not
    re-walk the tree, but an edited test shows up without a restart.
    """
    repo = Path(repo_path)
    if not repo.is_dir():
        raise FileNotFoundError(f"automation repo not found: {repo_path}")

    roots = test_source_roots(str(repo))
    stamp = _newest_mtime(roots)
    cached = _cache.get(str(repo))
    if use_cache and cached and cached["stamp"] == stamp:
        return cached["payload"]

    classes = []
    seen = set()
    for root in roots:
        for path in _iter_source_files(root):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            entry = _class_entry(path, repo)
            if entry:
                classes.append(entry)

    classes.sort(key=lambda c: (c["module"].lower(), c["name"].lower()))
    payload = {
        "workspace": str(repo),
        "classes": classes,
        "modules": sorted({c["module"] for c in classes}, key=str.lower),
        "total_classes": len(classes),
        "total_tests": sum(len(c["methods"]) for c in classes),
    }
    _cache[str(repo)] = {"stamp": stamp, "payload": payload}
    return payload
