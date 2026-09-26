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
from shared.logstep_narration import log_steps

# Anything that asserts. Deliberately broad: a project-specific wrapper that
# nobody told us about still matters, and a false positive here only costs a
# fingerprint that never changes.
ASSERT_CALL = re.compile(
    r"\b(AssertHelper\.\w+|assertPageLoaded|assert[A-Z]\w*|verify[A-Z]\w*"
    r"|compare[A-Z]\w*|shouldBe[A-Z]\w*)\s*\(")

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


def _normalise_args(text: str) -> Tuple[str, str]:
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

    Returned as (skeleton, expected) so conserved() can tell "the same call with a
    different expected value" apart from "a different call".
    """
    literals = _STRING.findall(text)
    expected = literals[:-1] if len(literals) > 1 else (
        [] if len(literals) == 1 else literals)
    skeleton = _STRING.sub("@", text)
    skeleton = _IDENTIFIER.sub("_", skeleton)
    skeleton = re.sub(r"\s+", "", skeleton)
    return skeleton, "|".join(expected)


def _canonical(literals: List[str]) -> List[str]:
    """Expected values with whitespace and case ignored — formatting, not meaning.

    `"$175.00"` and `"$ 175.00"` are one expectation rendered two ways; a fix that
    turns one into the other has not changed what the test proves.
    """
    return [re.sub(r"\s+", "", s).lower() for s in literals]


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
    """Every class in the repo keyed by simple name and by full name.

    The simple name is what a call site gives us: `loginPage.clickLogin()` names
    a field whose declared type is a simple name. When two classes share one
    (GitHub's LoginPage and SauceDemo's), `_lookup` picks by the mentioning
    file's imports and package, as Java does.
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
        candidates = graph["by_simple"].get(entry["simple"], [fqcn])
        out[fqcn] = out[entry["simple"]] = {
            "fqcn": fqcn, "path": entry["path"], "content": content,
            "package": entry.get("package", ""), "imports": entry.get("imports") or [],
            "members": {m["name"]: m for m in members if m.get("name")},
            "fields": fields,
            "extends": parent.group(1) if parent else None,
            # Shared plumbing, by the same module-locality rule the blast radius
            # uses. Walking into browser setup or the API base class adds no
            # business assertion and buries the real contract in library calls.
            "infrastructure": fqcn in graph["infrastructure"],
            # Under the simple name a second class with the same name replaces
            # the first, so a lookup by simple name must go through `_lookup`.
            "candidates": candidates,
            "ambiguous": len(candidates) > 1,
        }
    return out


def _lookup(name: Optional[str], context: Optional[Dict], index: Dict[str, Dict]) -> Optional[Dict]:
    """The class `name` means in `context`'s source, or None if it could be several.

    Imported first, then same package — the rule `blast_radius.index` uses.
    """
    entry = index.get(name) if name else None
    if not entry or not entry.get("ambiguous") or name == entry["fqcn"]:
        return entry
    if context is None:
        return None
    pool = entry["candidates"]
    for pick in ([c for c in pool if c in context.get("imports", ())],
                 [c for c in pool if c.rsplit(".", 1)[0] == context.get("package")]):
        if len(pick) == 1:
            return index.get(pick[0])
    return None


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
        # An ambiguous parent reads as unreadable: the same as a library base.
        parent = _lookup(parent_name, current, index)
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


AMBIGUOUS = "?ambiguous"


def _resolve_callee(receiver: Optional[str], method: str, klass: Dict,
                    index: Dict[str, Dict],
                    locals_: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The full name of the class a call lands in; None when it cannot be
    decided, AMBIGUOUS when the type's simple name is shared by two classes and
    neither an import nor the package says which."""
    if receiver is None or receiver in ("this", "super"):
        return klass["fqcn"] if method in klass["members"] else None

    def resolved(name: str, where: Dict) -> str:
        if name not in index:
            return name                  # a library type: walk() stops there
        entry = _lookup(name, where, index)
        return entry["fqcn"] if entry else AMBIGUOUS

    if locals_ and receiver in locals_:              # local variable or parameter
        return resolved(locals_[receiver], klass)
    # Fields, including inherited ones: a page object holds `page` on its base
    # class far more often than on itself.
    for ancestor in _ancestry(klass, index)[0]:
        if receiver in ancestor["fields"]:
            return resolved(ancestor["fields"][receiver], ancestor)
    if receiver in index:                            # static call on a type
        return resolved(receiver, klass)
    return None


def asserts_in(text: str, site: str) -> List[Dict]:
    """The assertions written in one member's text, in source order.

    Shared by the call-graph walk and by callers that compare edited files
    directly, so an assertion is described the same way by both. `raw` is what
    the fingerprint hashes; callers that do not hash can ignore it.
    """
    found = []
    for match in ASSERT_CALL.finditer(text):
        if _is_declaration(text, match.start()):
            continue
        callee = match.group(1)
        args = _call_args(text, match.end() - 1)
        skeleton, expected = _normalise_args(args)
        found.append({
            "callee": callee, "site": site,
            "cond_path": list(_cond_path(text, match.start())),
            "strength": _strength(callee.split(".")[-1]),
            "literals": _STRING.findall(args),
            "skeleton": skeleton,
            "raw": f"{callee.split('.')[-1]}|{skeleton}|{expected}",
        })
    return found


# `new CartPage(config)`, `new ArrayList<>()`. Qualified names (`new a.B(`) are
# deliberately not matched: the simple-name index cannot place them.
_NEW = re.compile(r"\bnew\s+([A-Z]\w*)\s*(?:<[^<>()]*>)?\s*\(")


def fingerprints(class_simple: str, method: str, index: Dict[str, Dict],
                 max_depth: int = MAX_DEPTH, follow_constructors: bool = False) -> Dict:
    """Every assertion reachable from one test method, with how it is guarded.

    Returns {"asserts": {fp: {...}}, "unresolved": [...], "log_steps": [...]}.

    `follow_constructors` also walks into `new X(...)`. A page object's
    constructor usually asserts that its page loaded, so without it, removing
    the only step that reaches a page drops that check without a trace. Off by
    default: the authoring agent compares against assertions it froze without
    it, and must keep comparing like with like.
    """
    result: Dict = {"asserts": {}, "unresolved": [], "log_steps": []}
    seen: Set[Tuple[str, str]] = set()
    occurrences: Dict[str, int] = {}

    def walk(key: str, member_name: str, depth: int, via: str = ""):
        # `key` is a full name, or at depth 0 whatever the caller passed; sites
        # keep the simple name so check ids do not depend on which.
        if depth > max_depth:
            return
        simple = key.rsplit(".", 1)[-1]
        klass = index.get(key) or (index.get(simple) if depth == 0 else None)
        visit = (klass["fqcn"] if klass else key, member_name)
        if visit in seen:
            return
        seen.add(visit)
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

        # Steps a test logs. CONVENTIONS.md requires these to state the action and
        # the expected outcome in plain English, which makes them the best available
        # source for a derived intent contract.
        result["log_steps"].extend(log_steps(text))

        # `site` names the class the walk came through; `defined_in` names the
        # class whose source holds the assertion, which is what a comparison of
        # edited files sees for an inherited method.
        defined_in = f"{klass['fqcn'].rsplit('.', 1)[-1]}#{member_name}"
        for info in asserts_in(text, f"{simple}#{member_name}"):
            raw = info.pop("raw")
            base = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
            # Two identical assertions — cart total and checkout total, both
            # "$ 183.99" — hashed to one key, so the second overwrote the first and
            # deleting either went unnoticed. The occurrence number keeps them
            # apart; the walk is in source order, so the numbering is stable.
            occurrences[base] = occurrences.get(base, 0) + 1
            info.update({"depth": depth, "defined_in": defined_in,
                         # Full name: two classes can share `defined_in`.
                         "owner": klass["fqcn"],
                         # The first call in the test that leads here, so a
                         # reader can tell which step a helper's check hangs off.
                         "via": via})
            result["asserts"][f"{base}_{occurrences[base]}"] = info

        for match in _CALL.finditer(text):
            receiver, name = match.group(1), match.group(2)
            if name in ("if", "for", "while", "switch", "catch", "return", "new"):
                continue
            if ASSERT_CALL.match(text[match.start():]):
                continue
            target = _resolve_callee(receiver, name, klass, index, locals_)
            if target == AMBIGUOUS:
                result["unresolved"].append(
                    f"{simple}#{member_name} -> {receiver}.{name}() (ambiguous class name)")
                continue
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
            walk(target, name, depth + 1,
                 via or f"{receiver + '.' if receiver else ''}{name}()")

        if not follow_constructors:
            return
        for match in _NEW.finditer(text):
            target = match.group(1)
            if target not in index:
                continue                     # a library type: nothing of ours runs
            entry = _lookup(target, klass, index)
            if entry is None:
                # Two classes share the name and neither an import nor the
                # package says which. Walking one could measure the wrong
                # page's checks, so say so.
                result["unresolved"].append(
                    f"{simple}#{member_name} -> new {target}() (ambiguous class name)")
                continue
            ctor = entry["members"].get(target)
            if not ctor or ctor.get("kind") != "constructor":
                continue                     # no written constructor to run
            walk(entry["fqcn"], target, depth + 1, via or f"new {target}()")

    walk(class_simple, method, 0)
    result["unresolved"] = sorted(set(result["unresolved"]))
    return result


def conserved(before: Dict, after: Dict) -> Dict:
    """Compare two fingerprint sets. Returns a verdict with named reasons.

    Neither argument is modified: `before` is usually a frozen contract that the
    caller reuses across change items and fix attempts.
    """
    lost, weakened, conditionalised, moved, changed = [], [], [], [], []
    after_asserts = after["asserts"]
    # Insertion-ordered, so every fallback below pairs in source order and the
    # verdict cannot depend on the hash seed.
    unmatched = dict.fromkeys(after_asserts)
    pairs: Dict[str, str] = {}

    def claim(fp: str, afp: Optional[str]) -> bool:
        if afp is None:
            return False
        pairs[fp] = afp
        del unmatched[afp]
        return True

    # 1. Same fingerprint. A contract frozen before occurrence suffixes existed
    # stores the bare hash, which is exactly the base of `<hash>_1` now.
    for fp, info in before["asserts"].items():
        legacy = f"{fp}_1" if "skeleton" not in info else None
        claim(fp, fp if fp in unmatched else (legacy if legacy in unmatched else None))

    # 2. Same call, same place, different expected value. Allowed only when the
    # values match with whitespace and case ignored: "$175.00" -> "$ 175.00" is the
    # page's formatting, "$175.00" -> "$ 0.00" changes what the test proves. The
    # last literal is the failure message and may change freely (see
    # _normalise_args). Price, shipping and total are usually one call shape, so
    # pairing prefers an equal value first and only then falls back to source
    # order — taking any candidate paired price with total and reported a correct
    # reformat as a changed amount.
    for same_value in (True, False):
        for fp, info in before["asserts"].items():
            if fp in pairs or "skeleton" not in info:
                continue
            expected = _canonical(info["literals"][:-1])
            afp = next((a for a in unmatched
                        if after_asserts[a]["callee"] == info["callee"]
                        and after_asserts[a]["site"] == info["site"]
                        and after_asserts[a].get("skeleton") == info["skeleton"]
                        and (not same_value
                             or _canonical(after_asserts[a]["literals"][:-1]) == expected)),
                       None)
            if claim(fp, afp) and _canonical(after_asserts[afp]["literals"][:-1]) != expected:
                changed.append(f"{info['callee']} at {info['site']}: "
                               f"{', '.join(info['literals'][:-1])} -> "
                               f"{', '.join(after_asserts[afp]['literals'][:-1])}")

    for fp, info in before["asserts"].items():
        now = after_asserts.get(pairs.get(fp, ""))
        if now is None:
            continue
        if len(now["cond_path"]) > len(info["cond_path"]):
            conditionalised.append(
                f"{info['callee']} at {info['site']} is now guarded by "
                f"{'/'.join(now['cond_path'])} — it runs only when it would pass")
        elif now["site"] != info["site"]:
            moved.append(f"{info['callee']}: {info['site']} -> {now['site']}")

    # 3. Still unpaired. A same-family replacement lower on the ladder, at the same
    # place, is a weakening; anything else is a loss.
    for fp, info in before["asserts"].items():
        if fp in pairs:
            continue
        family, rung = info["strength"]
        replacement = next((a for a in unmatched
                            if family >= 0
                            and after_asserts[a]["strength"][0] == family
                            and after_asserts[a]["strength"][1] > rung
                            and after_asserts[a]["site"] == info["site"]), None)
        if claim(fp, replacement):
            weakened.append(
                f"{info['callee']} at {info['site']} replaced by "
                f"{after_asserts[replacement]['callee']} — same check, weaker guarantee")
        else:
            lost.append(f"{info['callee']} at {info['site']}"
                        + (f" ({', '.join(info['literals'][:2])})" if info["literals"] else ""))

    holes_before = set(before.get("unresolved") or [])
    holes_after = set(after.get("unresolved") or [])
    new_holes = sorted(holes_after - holes_before)

    ok = not (lost or weakened or conditionalised or changed)
    reasons = []
    if lost:
        reasons.append("assertion(s) removed: " + "; ".join(lost[:4]))
    if weakened:
        reasons.append("assertion(s) weakened: " + "; ".join(weakened[:4]))
    if conditionalised:
        reasons.append("assertion(s) made conditional: " + "; ".join(conditionalised[:4]))
    if changed:
        reasons.append("expected value(s) changed: " + "; ".join(changed[:4]))

    return {
        "ok": ok,
        "verdict": "CONFIRMED" if (not ok or not new_holes) else "PLAUSIBLE",
        "reason": " | ".join(reasons),
        "lost": lost, "weakened": weakened, "changed": changed,
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


# ── Per-item comparison (the adaptation agent) ────────────────────────────────
#
# `conserved` answers "was anything lost?" against a snapshot, pairing by
# fingerprint and then by source order. That is enough to refuse, but not to
# *name* what changed: two checks with the same call shape and expected value
# (both `getPageTitle(), "Products"`) differ only in their message, so removing
# the first reads as losing the second. An agent that may change checks once it
# declares them needs the right one named, so `delta` pairs as multisets,
# message first. `conserved` is left exactly as it is for the other agents.

_VALUE_TOKEN = re.compile(r"@|(?<![\w.])(0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?)[lLfFdD]?(?![\w.])")
_NUMBER_TEXT = re.compile(r"(-?)(0[xX][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?)[lLfFdD]?")


def _number(token: str, negative: bool) -> str:
    raw = token.replace("_", "")
    try:
        if raw[:2].lower() == "0x":
            value = int(raw, 16)
        elif "." in raw:
            value = float(raw)
            value = int(value) if value.is_integer() else value
        else:
            value = int(raw)
    except ValueError:
        return token
    return f"-{value}" if negative else str(value)


def canonical_value(value) -> str:
    """One expected value however it was written: quotes, case, spacing and
    number spelling (`1_000`, `10L`, `0x1F`, `1.50`) do not change it."""
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    number = _NUMBER_TEXT.fullmatch(text)
    if number:
        return _number(number.group(2), bool(number.group(1)))
    return re.sub(r"\s+", "", text).lower()


def check_parts(info: Dict) -> Dict:
    """An assertion split into what `delta` compares.

    `shape` is the call with every expected value taken out; `values` are those
    values in source order, canonical; `top` says which of them are a whole
    argument rather than something nested in a call (`get("title")`, `get(0)`,
    `> 0`) — only a whole argument is something a page can be seen to show. The
    last string literal is the failure message, as `_normalise_args` has it.
    """
    literals = list(info.get("literals") or [])
    message_index = len(literals) - 1
    skeleton = info.get("skeleton") or ""

    args, depth, current = [], 0, ""
    for ch in skeleton:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(current)
            current = ""
        else:
            current += ch
    args.append(current)

    shape, values, display, top = [], [], [], []
    string_no = 0
    for arg in args:
        out, last = "", 0
        for match in _VALUE_TOKEN.finditer(arg):
            start = match.start()
            if match.group(0) == "@":
                index, string_no = string_no, string_no + 1
                out += arg[last:match.end()]
                last = match.end()
                if index == message_index or index >= len(literals):
                    continue
                values.append(canonical_value(literals[index]))
                display.append(literals[index])
                top.append(arg == "@")
                continue
            # A leading minus is part of the number; `a-1` is subtraction.
            negative = (start > 0 and arg[start - 1] == "-"
                        and (start == 1 or arg[start - 2] in "(=<>!?:&|"))
            begin = start - 1 if negative else start
            out += arg[last:begin] + "#"
            last = match.end()
            raw = arg[begin:match.end()]
            values.append(canonical_value(raw))
            display.append(raw)
            top.append(arg == raw)
        shape.append(out + arg[last:])
    return {"shape": ",".join(shape), "values": values, "display": display,
            "top": top,
            "message": literals[message_index].strip('"') if literals else ""}


def merge(per_test: Dict[str, Dict]) -> Dict:
    """Several tests' fingerprints as one list: one entry per assertion in code.

    A helper's check reached by three tests is one check, not three; two
    identical checks in one method are still two. Ids are built from content,
    so a check keeps its id across measurements for as long as it is unchanged.
    """
    merged: Dict[tuple, Dict] = {}
    unresolved: Set[str] = set()
    for test, fps in per_test.items():
        unresolved |= set(fps.get("unresolved") or [])
        counts: Dict[tuple, int] = {}
        for info in (fps.get("asserts") or {}).values():
            parts = check_parts(info)
            site = info.get("defined_in") or info.get("site", "")
            cond = len(info.get("cond_path") or [])
            owner = info.get("owner", "")
            key = (site, info["callee"], parts["shape"], tuple(parts["values"]),
                   parts["message"], cond, owner)
            counts[key] = counts.get(key, 0) + 1
            slot = key + (counts[key],)
            if slot not in merged:
                merged[slot] = {**parts, "site": site, "callee": info["callee"],
                                "cond": cond, "owner": owner,
                                "strength": tuple(info.get("strength") or (-1, -1)),
                                "via": info.get("via", ""), "tests": []}
            merged[slot]["tests"].append(test)

    checks = list(merged.values())
    bases: Dict[str, int] = {}
    for check in checks:
        raw = "|".join([check["site"], check["callee"], check["shape"],
                        "\x1f".join(check["values"]), check["message"]])
        base = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:7]
        bases[base] = bases.get(base, 0) + 1
        check["id"] = f"c{base}" + (f"_{bases[base]}" if bases[base] > 1 else "")
    return {"checks": checks, "unresolved": sorted(unresolved)}


def delta(before: List[Dict], after: List[Dict]) -> Dict:
    """What an edit did to a list of checks (entries from `merge`).

    Pairing order: unchanged, then reworded (same check, new message), then
    moved (same check elsewhere), then changed (same call and place, new values
    — same message first, then source order), then weakened (lower on a ladder
    at the same place). Whatever is left was removed or added. Any pair whose
    guard depth grew is also reported as conditional: it now runs only when it
    would pass.
    """
    left, right = list(before), list(after)
    out: Dict[str, list] = {k: [] for k in ("reworded", "moved", "changed", "weakened",
                                            "conditional", "removed", "added")}

    def key(check):
        return check["callee"], check["shape"], tuple(check["values"])

    def where(check):
        # Two classes can share a simple name, and so a site string.
        return check["site"], check.get("owner", "")

    def take(same, label):
        for b in list(left):
            a = next((c for c in right if same(b, c)), None)
            if a is None:
                continue
            left.remove(b)
            right.remove(a)
            if a["cond"] > b["cond"]:
                out["conditional"].append((b, a))
            if label:
                out[label].append((b, a))

    take(lambda b, a: key(b) == key(a) and where(b) == where(a)
         and b["message"] == a["message"], None)
    take(lambda b, a: key(b) == key(a) and where(b) == where(a), "reworded")
    take(lambda b, a: key(b) == key(a) and b["message"] == a["message"], "moved")
    take(lambda b, a: key(b) == key(a), "moved")
    take(lambda b, a: (b["callee"], b["shape"], where(b), b["message"])
         == (a["callee"], a["shape"], where(a), a["message"]), "changed")
    take(lambda b, a: (b["callee"], b["shape"], where(b))
         == (a["callee"], a["shape"], where(a)), "changed")

    for b in list(left):
        family, rung = b["strength"]
        if family < 0:
            continue
        a = next((c for c in right if where(c) == where(b)
                  and c["strength"][0] == family and c["strength"][1] > rung), None)
        if a is not None:
            left.remove(b)
            right.remove(a)
            out["weakened"].append((b, a))

    out["removed"], out["added"] = left, right
    return out
