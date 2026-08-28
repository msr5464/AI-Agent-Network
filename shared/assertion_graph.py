"""What a test proves, and whether an edit quietly stopped it proving that.

A locator fix is small enough that a diff-size cap and a lost-method check bound
the damage. A flow edit is not: it may legitimately add steps, delete steps and
touch several files, so "the diff is big" stops being a signal. Something else has
to hold the line, and the only thing that can is the set of assertions the test
actually executes.

The subtlety is that the set is not visible in the test method. A test method
calls `helper.completeCheckout()`, and the assertions live two hops down inside
the helper. Comparing the test file's own diff therefore proves nothing: an edit
can delete an assertion from a helper and leave the test method untouched. So the
comparison has to run over the transitive call graph.

Three details make the comparison honest rather than merely strict:

  * **String literals stay in the fingerprint.** `assertEquals(cfg, "Total", total,
    "42")` weakened to `assertNotNull(cfg, "Total", total)` is only detectable
    because the expected value is part of what is compared.
  * **Conditionality is part of the fingerprint.** An assertion still present but
    now wrapped in `if (isDisplayed(...))` runs only when it would have passed.
    That is a deleted assertion wearing a disguise, and comparing call sites alone
    would wave it through.
  * **Unresolved receivers are reported, never dropped.** A call this module could
    not resolve is a hole in the guarantee. Silently ignoring it turns "no
    assertion was lost" into "no assertion I happened to look at was lost".
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from shared.code_analyzer import (read_source, split_class_members,
                                  without_comments)

# Anything that asserts. Deliberately broad: a project-specific wrapper that
# nobody told us about still matters, and a false positive here only costs a
# fingerprint that never changes.
ASSERT_CALL = re.compile(
    r"\b(AssertHelper\.\w+|assertPageLoaded|assert[A-Z]\w*|verify[A-Z]\w*"
    r"|compare[A-Z]\w*|shouldBe[A-Z]\w*)\s*\(")

# Steps a test logs. CONVENTIONS.md requires these to state the action and the
# expected outcome in plain English, which makes them the best available source
# for a derived intent contract.
# Both call shapes in the wild: `logStep(testConfig, "…")` as CONVENTIONS.md
# writes it, and `config.logStep("…")` as the Playwright framework actually does.
# Only matching the first found no steps at all in a real repo.
LOG_STEP = re.compile(
    r"\blogStep\s*\(\s*(?:\w+\s*,\s*)?(\"(?:\\.|[^\"\\])*\")")

# Strength order within a family, weakest last. An edit that moves an assertion
# down one of these ladders has weakened it even though a call still exists.
_LADDERS = (
    ("assertEquals", "assertPartialEquals", "assertContains", "assertTrue",
     "assertNotNull", "assertNotEmpty"),
    ("assertElementText", "assertPartialElementText", "assertElementIsDisplayed"),
    ("compareEquals", "compareContains", "compareTrue"),
)

# The nearest control keyword before an opening brace, with nothing but the
# condition in between. Anchored to the end so `if (a) { if (b) {` attributes
# each brace to its own keyword.
_NEAREST_KEYWORD = re.compile(r"\b(if|else|while|for|try|catch|switch)\b[^{;]*$")
_IDENTIFIER = re.compile(r"\b[a-z]\w*\b")
# `ProductsPage products = sauceDemo.doLogin(...)`. Page objects are handed back
# by helpers and held in locals far more often than they are fields, so a
# field-only resolver reports most of a real test as unresolvable.
_LOCAL_DECL = re.compile(r"\b([A-Z]\w*)(?:<[^>]*>)?\s+([a-z]\w*)\s*=")
# `addProductToCart(String productName)` declares productName just as firmly as
# an assignment does, but _LOCAL_DECL needs an `=` and so never saw a parameter.
# Every call on one — `config.logStep()`, `productName.toLowerCase()` — was then
# reported as a call we could not follow, which is how an unresolved list ends up
# full of the JDK. Anchored to a comma or the closing paren so it only matches in
# a parameter position.
_PARAM_DECL = re.compile(
    r"\b([A-Z]\w*)(?:<[^>]*>)?(?:\[\])?\s+([a-z]\w*)\s*(?=[,)])")
# `class ProductsPage extends BasePage` — needed to find inherited fields, and to
# know when the chain leaves the code we can see.
_EXTENDS = re.compile(r"\bclass\s+\w+(?:<[^>]*>)?\s+extends\s+([A-Z]\w*)")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_CALL = re.compile(r"\b(?:(\w+)\s*\.\s*)?(\w+)\s*\(")

MAX_DEPTH = 4


def _strength(callee: str) -> Tuple[int, int]:
    """(ladder index, rung) — higher rung means weaker. (-1, -1) if unranked."""
    for i, ladder in enumerate(_LADDERS):
        for j, rung in enumerate(ladder):
            if callee == rung or callee.endswith("." + rung):
                return i, j
    return -1, -1


def _normalise_args(text: str) -> str:
    """Argument text with identifiers collapsed and the expected value preserved.

    Renaming a local variable must not read as a changed assertion; changing the
    expected value must.

    The **last** string literal is dropped, because in this framework — and in
    TestNG generally — it is the human-readable failure message:
    `assertEquals(config, actual, "Products", "Products page title should be …")`.
    Including it meant that improving the wording of a message registered as a
    changed fingerprint, which the ladder check then reported as a *weakened
    assertion*. A guard that cries wolf over a copy edit is a guard people learn
    to override, which costs far more than it saves.
    """
    literals = _STRING.findall(text)
    expected = literals[:-1] if len(literals) > 1 else (
        [] if len(literals) == 1 else literals)
    skeleton = _STRING.sub("@", text)
    skeleton = _IDENTIFIER.sub("_", skeleton)
    skeleton = re.sub(r"\s+", "", skeleton)
    return skeleton + "|" + "|".join(expected)


def _is_declaration(text: str, start: int) -> bool:
    """Whether the name at `start` is a method being declared, not called.

    `public void verifyTotal() {` matches the same pattern as a call to it, so
    without this every project wrapper named verifyX is counted twice — once
    where it is used and once where it is defined — and the definition drags its
    whole body in as the argument text.
    """
    head = text[:start].rstrip()
    if head.endswith("."):
        return False                      # qualified call: confirm.verifyTotal()
    return bool(re.search(r"\b\w+\s*$", head))


def _blank_literals(text: str) -> str:
    """String literals blanked out, **keeping their length**.

    Length matters: the blanked copy is only used for scanning, and every offset
    found in it is used to slice the original. Collapsing `"Order total"` to `""`
    shifted every later index by eleven characters, which silently truncated the
    argument list and dropped the expected value from the fingerprint — the one
    thing that distinguishes a weakened assertion from an intact one.
    """
    return re.sub(r'"(?:\\.|[^"\\])*"',
                  lambda m: '"' + " " * (len(m.group(0)) - 2) + '"', text)


def _call_args(text: str, open_paren: int) -> str:
    """The argument text of the call whose `(` is at `open_paren`.

    Needs its own matcher: code_analyzer._match_brace counts `{}`, so handing it
    a parenthesis index silently returned whatever the enclosing block happened
    to span. That made an assertion's fingerprint depend on the braces *around*
    it, so wrapping one in `if (...)` changed its fingerprint and the guard
    reported a conditionalised assertion as a deleted one — right alarm, wrong
    reason, and it would have said the model removed a check it had only moved.
    """
    scan = _blank_literals(text)
    depth = 0
    for i in range(open_paren, len(scan)):
        if scan[i] == "(":
            depth += 1
        elif scan[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:i]
    return ""


def _cond_path(text: str, upto: int) -> Tuple[str, ...]:
    """Enclosing control-flow keywords for the call site at `upto`.

    An assertion that survives an edit but is now inside a new `if` runs only
    when it would have passed anyway. Comparing call sites alone waves that
    through, so the guard path is part of what gets compared.
    """
    scan = _blank_literals(text[:upto])
    stack: List[str] = []
    for i, ch in enumerate(scan):
        if ch == "{":
            match = _NEAREST_KEYWORD.search(scan[:i])
            stack.append(match.group(1) if match else "block")
        elif ch == "}" and stack:
            stack.pop()
    return tuple(k for k in stack if k != "block")


def member_index(repo_path: str) -> Dict[str, Dict]:
    """Every class in the repo keyed by simple name, with its members and fields.

    Keyed on the simple name because that is what a call site gives us:
    `loginPage.clickLogin()` names a field whose declared type is a simple name.
    """
    from shared.blast_radius import index as _index

    graph = _index(repo_path)
    out: Dict[str, Dict] = {}
    for fqcn, entry in graph["classes"].items():
        path = Path(repo_path) / entry["path"]
        content = read_source(path)
        if not content:
            continue
        members = split_class_members(content)
        fields: Dict[str, str] = {}
        for member in members:
            if member["kind"] != "field" or not member["name"]:
                continue
            # The declared type is the last capitalised token before the name.
            head = member["text"].split(member["name"])[0]
            types = re.findall(r"\b([A-Z]\w*)\b", head)
            if types:
                fields[member["name"]] = types[-1]
        parent = _EXTENDS.search(content)
        out[entry["simple"]] = {
            "fqcn": fqcn, "path": entry["path"], "content": content,
            "members": {m["name"]: m for m in members if m.get("name")},
            "fields": fields,
            "extends": parent.group(1) if parent else None,
            # Shared plumbing, by the same module-locality rule the blast radius
            # uses. Walking into browser setup or the API base class adds no
            # business assertion and buries the real contract in library calls.
            "infrastructure": fqcn in graph["infrastructure"],
        }
    return out


def _ancestry(klass: Dict, index: Dict[str, Dict]) -> Tuple[List[Dict], bool]:
    """The class and its visible superclasses, plus whether the chain runs out.

    "Runs out" means it extends something this repo does not define — a framework
    or library base. Fields declared up there are real but unreadable, so a call
    on one cannot be followed and equally cannot be a hole in *our* guarantee.
    """
    chain, seen, complete = [klass], set(), True
    current = klass
    while current.get("extends"):
        parent_name = current["extends"]
        if parent_name in seen:                      # defensive: cyclic extends
            break
        seen.add(parent_name)
        parent = index.get(parent_name)
        if parent is None:
            complete = False
            break
        chain.append(parent)
        current = parent
    return chain, complete


def _is_generated_accessor(name: str, chain: List[Dict]) -> bool:
    """`getBody` where a `body` field is declared — a generated getter."""
    for prefix in ("get", "is", "set"):
        if not name.startswith(prefix) or len(name) <= len(prefix):
            continue
        bare = name[len(prefix):]
        candidate = bare[0].lower() + bare[1:]
        if any(candidate in k["fields"] for k in chain):
            return True
    return False


def _resolve_callee(receiver: Optional[str], method: str, klass: Dict,
                    index: Dict[str, Dict],
                    locals_: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Which class a call lands in, or None when it cannot be decided."""
    if receiver is None or receiver in ("this", "super"):
        return klass["fqcn"].rsplit(".", 1)[-1] if method in klass["members"] else None
    if locals_ and receiver in locals_:              # local variable or parameter
        return locals_[receiver]
    # Fields, including inherited ones: a page object holds `page` on its base
    # class far more often than on itself.
    for ancestor in _ancestry(klass, index)[0]:
        if receiver in ancestor["fields"]:
            return ancestor["fields"][receiver]
    if receiver in index:                            # static call on a type
        return receiver
    return None


def fingerprints(class_simple: str, method: str, index: Dict[str, Dict],
                 max_depth: int = MAX_DEPTH) -> Dict:
    """Every assertion reachable from one test method, with how it is guarded.

    Returns {"asserts": {fp: {...}}, "unresolved": [...], "log_steps": [...]}.
    """
    result: Dict = {"asserts": {}, "unresolved": [], "log_steps": []}
    seen: Set[Tuple[str, str]] = set()

    def walk(simple: str, member_name: str, depth: int):
        if depth > max_depth or (simple, member_name) in seen:
            return
        seen.add((simple, member_name))
        klass = index.get(simple)
        if not klass:
            # At depth 0 this is the test we were asked about and not finding it
            # is a real answer. Deeper, it means the trail led into a type this
            # repo does not define — a framework or library class. There are no
            # assertions of ours in there to lose, so it is the same deliberate
            # boundary as `infrastructure` below, not a hole in the guarantee.
            if depth == 0:
                result["unresolved"].append(f"{simple}#{member_name} (class not indexed)")
            return
        if depth > 0 and klass.get("infrastructure"):
            # Not a hole: a deliberate boundary. What a test proves lives in the
            # module's own code and its page objects, not in the framework.
            return
        chain, chain_complete = _ancestry(klass, index)
        member = None
        for ancestor in chain:                       # a helper's `execute()` is
            member = ancestor["members"].get(member_name)  # usually on its base
            if member:
                klass = ancestor
                break
        if not member:
            # A getter over a declared field is generated (Lombok and friends),
            # and an accessor holds no assertions in any case. An incomplete
            # ancestry means the method may simply live in code we cannot read.
            if not (_is_generated_accessor(member_name, chain) or not chain_complete):
                result["unresolved"].append(f"{simple}#{member_name} (method not found)")
            return
        # Comments are not code. A `//`-ed out assertion was being fingerprinted
        # as a live one, so conservation would wave through an edit that disabled
        # a check by commenting it — the same disguise as wrapping it in an `if`,
        # which this module already refuses. Javadoc examples (`api.execute(...)`)
        # were likewise counted as calls and reported as unfollowable.
        text = without_comments(member["text"])
        # Two maps, deliberately. `locals_` is what we can follow; `foreign` is
        # what we know we cannot and do not need to — a `BrowserContext` from the
        # Playwright library is not a hole in our guarantee, it is simply not our
        # code, and lumping the two together buried a handful of genuine unknowns
        # under sixty lines of library plumbing.
        # Parameters first so an assignment later in the body can shadow one.
        # The list has to be taken with balanced parens keyed on the member name:
        # splitting on the first "{" lands inside `@Test(groups = {A, B})` and
        # truncates the signature before the parameters ever appear.
        signature = ""
        opener = re.search(r"\b" + re.escape(member_name) + r"\s*\(", text)
        if opener:
            signature = _call_args(text, opener.end() - 1)
        declared = dict((name, type_) for type_, name in _PARAM_DECL.findall(signature + ")"))
        declared.update((name, type_) for type_, name in _LOCAL_DECL.findall(text))
        locals_ = {n: t for n, t in declared.items() if t in index}
        foreign = {n for n, t in declared.items() if t not in index}
        # A base class this repo does not define — TestBase, BasePage — holds
        # fields we can see used but never declared. Calls on them are library
        # plumbing, not gaps in what the test proves.
        opaque_base = not _ancestry(klass, index)[1]

        for match in LOG_STEP.finditer(text):
            result["log_steps"].append(match.group(1).strip('"'))

        for match in ASSERT_CALL.finditer(text):
            if _is_declaration(text, match.start()):
                continue
            callee = match.group(1)
            args = _call_args(text, match.end() - 1)
            cond = _cond_path(text, match.start())
            raw = f"{callee.split('.')[-1]}|{_normalise_args(args)}"
            fp = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
            result["asserts"][fp] = {
                "callee": callee, "site": f"{simple}#{member_name}",
                "depth": depth, "cond_path": list(cond),
                "strength": _strength(callee.split(".")[-1]),
                "literals": _STRING.findall(args),
            }

        for match in _CALL.finditer(text):
            receiver, name = match.group(1), match.group(2)
            if name in ("if", "for", "while", "switch", "catch", "return", "new"):
                continue
            if ASSERT_CALL.match(text[match.start():]):
                continue
            target = _resolve_callee(receiver, name, klass, index, locals_)
            if target is None:
                # A capitalised receiver this repo does not define is a static
                # call into the JDK or a library — `Paths.get()`, `Duration.
                # ofSeconds()`. Those are not holes in the guarantee, they are
                # simply not our code, and recording them buried the handful of
                # genuine unknowns under eighty lines of noise.
                external = ((bool(receiver) and receiver[:1].isupper()
                             and receiver not in index)
                            or receiver in foreign
                            # An unknown lowercase receiver in a class whose base
                            # we cannot read is almost certainly an inherited
                            # framework field. Reporting it as a hole every time
                            # is what taught people to ignore this list.
                            or (opaque_base and bool(receiver)
                                and receiver[:1].islower()
                                and receiver not in klass["fields"]))
                if receiver and receiver not in ("this", "super") and not external:
                    result["unresolved"].append(
                        f"{simple}#{member_name} -> {receiver}.{name}()")
                continue
            walk(target, name, depth + 1)

    walk(class_simple, method, 0)
    result["unresolved"] = sorted(set(result["unresolved"]))
    return result


def conserved(before: Dict, after: Dict) -> Dict:
    """Compare two fingerprint sets. Returns a verdict with named reasons."""
    lost, weakened, conditionalised, moved = [], [], [], []

    for fp, info in before["asserts"].items():
        if fp in after["asserts"]:
            now = after["asserts"][fp]
            if len(now["cond_path"]) > len(info["cond_path"]):
                conditionalised.append(
                    f"{info['callee']} at {info['site']} is now guarded by "
                    f"{'/'.join(now['cond_path'])} — it runs only when it would pass")
            elif now["site"] != info["site"]:
                moved.append(f"{info['callee']}: {info['site']} -> {now['site']}")
            continue
        # Not present by fingerprint. A same-family replacement lower on the
        # ladder is a weakening; anything else is a loss.
        family, rung = info["strength"]
        replacement = None
        if family >= 0:
            for other in after["asserts"].values():
                fam2, rung2 = other["strength"]
                if fam2 == family and rung2 > rung:
                    replacement = other
                    break
        if replacement is not None:
            weakened.append(
                f"{info['callee']} at {info['site']} replaced by "
                f"{replacement['callee']} — same check, weaker guarantee")
        else:
            lost.append(f"{info['callee']} at {info['site']}"
                        + (f" ({', '.join(info['literals'][:2])})" if info["literals"] else ""))

    holes_before = set(before.get("unresolved") or [])
    holes_after = set(after.get("unresolved") or [])
    new_holes = sorted(holes_after - holes_before)

    ok = not (lost or weakened or conditionalised)
    reasons = []
    if lost:
        reasons.append("assertion(s) removed: " + "; ".join(lost[:4]))
    if weakened:
        reasons.append("assertion(s) weakened: " + "; ".join(weakened[:4]))
    if conditionalised:
        reasons.append("assertion(s) made conditional: " + "; ".join(conditionalised[:4]))

    return {
        "ok": ok,
        "verdict": "CONFIRMED" if (not ok or not new_holes) else "PLAUSIBLE",
        "reason": " | ".join(reasons),
        "lost": lost, "weakened": weakened,
        "conditionalised": conditionalised, "moved": moved,
        "new_unresolved": new_holes,
        "counted": len(before["asserts"]),
    }


def describe(report: Dict) -> str:
    if report["ok"]:
        line = f"assertion conservation OK ({report['counted']} assertion(s) preserved)"
        if report["moved"]:
            line += f"; moved: {', '.join(report['moved'][:3])}"
        if report["new_unresolved"]:
            line += (f"; PLAUSIBLE not CONFIRMED — {len(report['new_unresolved'])} "
                     f"call(s) could not be resolved: "
                     f"{', '.join(report['new_unresolved'][:3])}")
        return line
    return "assertion conservation FAILED — " + report["reason"]
