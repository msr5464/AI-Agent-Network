"""What already exists, compact enough to put in front of the model.

Codegen has no tools: it sees the files it is handed and nothing else. Handed a
module's files cut at a few thousand characters, it never saw the operations
past the cut and wrote them again. Never handed the framework's shared code, it
re-implemented waits and conversions the base classes already had. Reuse cannot
be asked of a model that cannot see what there is to reuse — so this module
turns source into an index: one line per public member, grouped by class.

It also answers the two questions a reviewer asks of a change to existing code:
which existing methods did this run change, and did it write a method that
already exists under another name.

Java-shaped by necessity (the authoring agent's codegen is Java-coupled already),
built on `code_analyzer.split_class_members` like every other reader of members.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from shared.code_analyzer import _strip_annotations, split_class_members, without_comments

_TYPE_HEAD = re.compile(r"\b(class|interface|enum|record)\s+(\w+)")
_MODIFIERS = {"public", "protected", "private", "final", "synchronized", "abstract",
              "native", "default", "transient", "volatile", "strictfp"}
_NOT_CALLS = {"if", "for", "while", "switch", "catch", "return", "new", "throw", "super",
              "this", "synchronized"}
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')

# Below this many calls two bodies are too small to call one a copy of the other.
MIN_DUPLICATE_CALLS = 3
DUPLICATE_RATIO = 0.9


def _head(member_text: str) -> str:
    """A member's declaration: comments and annotations gone, body cut, one line."""
    text = _strip_annotations(without_comments(member_text or ""))
    return " ".join(text.split("{", 1)[0].replace(";", " ").split())


def _body(member_text: str) -> str:
    text = without_comments(member_text or "")
    start = text.find("{")
    end = text.rfind("}")
    return text[start + 1:end] if 0 <= start < end else ""


def _modifiers(head: str) -> set:
    return {word for word in head.split() if word in _MODIFIERS or word == "static"}


def _signature(head: str) -> str:
    """The head without access/final modifiers: `static boolean waitForUrl(Config config, String url)`."""
    return " ".join(word for word in head.split() if word not in _MODIFIERS)


def _types(source: str, prefix: str = "") -> List[Dict]:
    """Every type in `source`, nested ones included, as {name, kind, members}."""
    text = without_comments(source or "")
    match = _TYPE_HEAD.search(text)
    if not match:
        return []
    kind, name = match.group(1), match.group(2)
    qualified = f"{prefix}{name}"
    found = [{"name": qualified, "kind": kind, "source": text, "members": []}]
    for member in split_class_members(text):
        head = _head(member["text"])
        nested = _TYPE_HEAD.search(head)
        if nested and "(" not in head.split(nested.group(0), 1)[0]:
            found.extend(_types(member["text"], prefix=f"{qualified}."))
            continue
        kind, name = member["kind"], member["name"]
        # `Locator pay = page.locator("#pay");` ends in a call, and the splitter
        # names it a method `locator`. An `=` before the first `(` makes it a field.
        before_call = head.split("(", 1)[0]
        if kind in ("method", "method_decl") and "=" in before_call:
            assigned = re.search(r"(\w+)\s*=", before_call)
            kind, name = "field", (assigned.group(1) if assigned else name)
        found[0]["members"].append({**member, "kind": kind, "name": name, "head": head})
    return found


def _is_public(member: Dict, owner_kind: str) -> bool:
    mods = _modifiers(member["head"])
    if "private" in mods or "protected" in mods:
        return False
    return "public" in mods or owner_kind == "interface"


def public_signatures(source: str) -> Dict[str, List[str]]:
    """{type name: [public constructor and method signatures]}, nested types as `Outer.Inner`."""
    out: Dict[str, List[str]] = {}
    for owner in _types(source):
        lines = [_signature(m["head"]) for m in owner["members"]
                 if m["kind"] in ("method", "method_decl", "constructor")
                 and _is_public(m, owner["kind"])]
        out[owner["name"]] = lines
    return out


def _enum_body(source: str) -> str:
    """The constants section of the first enum declared in `source`."""
    text = without_comments(source or "")
    match = re.search(r"\benum\s+\w+[^{]*\{", text)
    if not match:
        return ""
    depth, out = 0, []
    for ch in text[match.end():]:
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            if depth == 0:
                break
            depth -= 1
        elif ch == ";" and depth == 0:
            break
        out.append(ch)
    return "".join(out)


def _enum_constants_in(body: str) -> List[str]:
    names, depth, current = [], 0, []
    for ch in _STRING.sub('""', body) + ",":
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        if ch == "," and depth == 0:
            token = re.match(r"\s*([A-Za-z_]\w*)", "".join(current))
            if token:
                names.append(token.group(1))
            current = []
            continue
        current.append(ch)
    return names


def enum_constants(source: str) -> Dict[str, List[str]]:
    """{enum name: [constants]} for every enum in `source`, nested ones as `Outer.Inner`."""
    return {owner["name"]: _enum_constants_in(_enum_body(owner["source"]))
            for owner in _types(source) if owner["kind"] == "enum"}


def _field_names(owner: Dict) -> List[str]:
    return [m["name"] for m in owner["members"]
            if m["kind"] == "field" and m["name"] and "static" not in _modifiers(m["head"])]


def _public_fields(owner: Dict) -> List[str]:
    """`CartPage cartPage` for each public instance field: the pages a Helper holds."""
    out = []
    for m in owner["members"]:
        if (m["kind"] != "field" or not m["name"] or not _is_public(m, owner["kind"])
                or "static" in _modifiers(m["head"])):
            continue
        declared = _signature(m["head"].split("=", 1)[0]).split()
        out.append(" ".join(declared[-2:]) if len(declared) >= 2 else m["name"])
    return out


def _describe_file(path: Path, rel: str) -> List[str]:
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    lines: List[str] = []
    signatures = public_signatures(source)
    enums = enum_constants(source)
    for owner in _types(source):
        name = owner["name"]
        if owner["kind"] == "enum":
            lines.append(f"  enum {name}: {', '.join(enums.get(name) or []) or '(no constants)'}")
            continue
        header = f"  {owner['kind']} {name}"
        if "." not in name:
            header += f"  ({rel})"
        lines.append(header)
        # Fields matter where they are the interface: a page's locators, a data
        # class's values, and a nested value class whose getters Lombok writes.
        if "/web/" in rel.replace("\\", "/") or name.endswith("Data") or "." in name:
            fields = _field_names(owner)
            if fields:
                lines.append(f"    fields: {', '.join(fields)}")
        # A Helper's public fields hold the pages its tests continue from.
        elif name.endswith("Helper"):
            fields = _public_fields(owner)
            if fields:
                lines.append(f"    page fields: {', '.join(fields)}")
        for signature in signatures.get(name) or []:
            lines.append(f"    {signature}")
    return lines


def _java_files(root: Path, paths: Iterable[str]) -> List[Path]:
    files: List[Path] = []
    for rel in paths or []:
        target = (root / rel) if not Path(rel).is_absolute() else Path(rel)
        if target.is_file() and target.suffix == ".java":
            files.append(target)
        elif target.is_dir():
            files.extend(sorted(target.rglob("*.java")))
    return files


def describe(root, paths: Iterable[str]) -> str:
    """The index of every Java file under `paths` (repo-relative), or "" if none."""
    root = Path(root)
    out: List[str] = []
    for path in _java_files(root, paths):
        try:
            rel = str(path.relative_to(root))
        except ValueError:
            rel = str(path)
        out.extend(_describe_file(path, rel))
    return "\n".join(out)


def describe_module(root, module_dir: str) -> str:
    """The existing module's index: its Helper, enums, pages, data and API constants."""
    return describe(root, [module_dir])


def describe_shared(root, paths: Iterable[str]) -> str:
    """The framework's shared code, one line per public member."""
    return describe(root, paths)


def _method_key(head: str, name: str) -> str:
    """`name(Type1,Type2)` — what tells overloads apart, parameter names dropped."""
    params = head.split("(", 1)[1].rsplit(")", 1)[0] if "(" in head else ""
    types, depth, current = [], 0, []
    for ch in params + ",":
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            words = [w for w in "".join(current).split() if w != "final"]
            if words:
                types.append("".join(words[:-1]) or words[0])
            current = []
            continue
        current.append(ch)
    return f"{name}({','.join(types)})"


def _methods(source: str) -> Dict[str, Dict]:
    """{`Type.name(ParamTypes)`: {head, body}} for every method and constructor."""
    out = {}
    for owner in _types(source):
        for member in owner["members"]:
            if member["kind"] not in ("method", "method_decl", "constructor"):
                continue
            key = f"{owner['name']}.{_method_key(member['head'], member['name'])}"
            out[key] = {"head": member["head"], "body": " ".join(_body(member["text"]).split()),
                        "public": _is_public(member, owner["kind"]),
                        "constructor": member["kind"] == "constructor"}
    return out


def method_keys(source: str) -> List[str]:
    """Every method and constructor of `source`, as `Type.name(ParamTypes)`."""
    return sorted(_methods(source))


def changed_methods(before: str, after: str) -> Dict[str, List[str]]:
    """Methods of one file that this edit changed, added or removed.

    Keyed by `Type.name(ParamTypes)`, so an overload added beside an existing
    method is "added", and the existing one is only "changed" if its own body or
    declaration differs.
    """
    old, new = _methods(before), _methods(after)
    return {
        "changed": sorted(k for k in old.keys() & new.keys()
                          if old[k]["body"] != new[k]["body"] or old[k]["head"] != new[k]["head"]),
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
    }


def changed_fields(before: str, after: str) -> List[str]:
    """Fields of one file whose declaration or initializer this edit changed or removed.

    A field is not a method a caller can be traced to: a locator declared as a
    field and changed reaches every test that touches its class, so a caller of
    this treats a changed field as a change to the whole class. Added fields are
    not listed — nothing existing reads them.
    """
    def fields(source: str) -> Dict[str, str]:
        return {f"{owner['name']}.{m['name']}": " ".join(without_comments(m["text"]).split())
                for owner in _types(source) for m in owner["members"]
                if m["kind"] == "field" and m.get("name")}
    old, new = fields(before), fields(after)
    return sorted(k for k in old if old[k] != new.get(k))


def lost_api(before: str, after: str) -> List[str]:
    """Public signatures and enum constants `before` had and `after` lost.

    A caller written against any of these no longer compiles, or no longer
    means what it did — which is never a small change to an existing method.
    """
    old, new = _methods(before), _methods(after)
    lost = [old[k]["head"] for k in sorted(old.keys() - new.keys()) if old[k]["public"]]
    old_enums, new_enums = enum_constants(before), enum_constants(after)
    for enum, constants in old_enums.items():
        gone = [c for c in constants if c not in (new_enums.get(enum) or [])]
        lost.extend(f"{enum}.{c}" for c in gone)
    return lost


_CALL_TOKEN = re.compile(r"(\bnew\s+)?(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)\s*\("
                         r"\s*([A-Za-z_]\w*(?=\s*[,)]))?")


def _call_sequence(body: str) -> List[str]:
    """What a body does, as a sequence of calls with literals masked.

    A call keeps its receiver (`cart.confirm`) and, when bare, its first argument
    if that is a plain name (`click(payButton)`): two page methods clicking
    different locators, or two selections through different enums, have the same
    call names and are still not copies of each other. Literal values are what a
    copy changes, so those are the only thing masked.
    """
    masked = _STRING.sub('""', body or "")
    tokens = []
    for new, receiver, name, first_arg in _CALL_TOKEN.findall(masked):
        if new:
            tokens.append(f"new {name}")
        elif receiver:
            tokens.append(f"{receiver}.{name}")
        elif name not in _NOT_CALLS:
            tokens.append(f"{name}({first_arg})" if first_arg else name)
    return tokens


def near_duplicates(new_sources: Dict[str, str], existing_sources: Dict[str, str],
                    only: Optional[Dict[str, Iterable[str]]] = None) -> List[Dict]:
    """New methods whose call sequence repeats an existing (or another new) method's.

    Literals are masked, so a copy that differs only in a hard-coded value — the
    exact thing the reuse ladder forbids — still matches. `only` limits which
    methods count as new, per path (the run's added members); without it every
    method of `new_sources` does.
    """
    candidates: Dict[str, List[str]] = {}
    for path, source in (new_sources or {}).items():
        methods = _methods(source)
        wanted = set(only.get(path) or ()) if only is not None else set(methods)
        # Constructors all share one shape — set up fields, check the page loaded —
        # so they are never anyone's copy.
        for key in wanted:
            if key in methods and not methods[key]["constructor"]:
                candidates[f"{path}::{key}"] = _call_sequence(methods[key]["body"])
    pool: Dict[str, List[str]] = dict(candidates)
    for path, source in (existing_sources or {}).items():
        for key, method in _methods(source).items():
            if not method["constructor"]:
                pool.setdefault(f"{path}::{key}", _call_sequence(method["body"]))

    pairs, seen = [], set()
    for new_id, calls in candidates.items():
        if len(calls) < MIN_DUPLICATE_CALLS:
            continue
        for other_id, other in pool.items():
            if other_id == new_id or len(other) < MIN_DUPLICATE_CALLS:
                continue
            pair = tuple(sorted((new_id, other_id)))
            if pair in seen:
                continue
            ratio = difflib.SequenceMatcher(None, calls, other).ratio()
            if ratio >= DUPLICATE_RATIO:
                seen.add(pair)
                pairs.append((pair, ratio))
    return _group_pairs(pairs)


def _group_pairs(pairs: List[tuple]) -> List[Dict]:
    """Pairs of alike methods as groups: `{"methods": [...], "ratio": lowest}`.

    Reported a pair at a time, one method written into four classes read as six
    separate findings.
    """
    parent: Dict[str, str] = {}

    def root(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), _ratio in pairs:
        parent[root(a)] = root(b)
    groups: Dict[str, Dict] = {}
    for (a, _b), ratio in pairs:
        group = groups.setdefault(root(a), {"ids": set(), "ratio": 1.0})
        group["ids"] |= {a, _b}
        group["ratio"] = min(group["ratio"], ratio)
    return [{"methods": [i.split("::", 1)[1] for i in sorted(g["ids"])],
             "ratio": round(g["ratio"], 2)} for g in groups.values()]


def describe_duplicates(group: Dict) -> str:
    """One line for a group of alike methods."""
    methods = group.get("methods") or []
    alike = "identical" if group.get("ratio") == 1.0 else f"similarity {group.get('ratio')}"
    names = {m.split(".", 1)[-1] for m in methods}
    if len(names) == 1:
        owners = ", ".join(m.split(".", 1)[0] for m in methods)
        return f"`{names.pop()}` is written {len(methods)} times ({alike}): {owners}"
    return f"{', '.join(f'`{m}`' for m in methods)} repeat one another ({alike})"


_NEW_TYPE = re.compile(r"\bnew\s+([A-Z]\w*)\s*\(")
# Whatever the framework names it: navigate(), navigateTo(), goto().
_NAVIGATION = re.compile(r"\b(?:navigate\w*|goto)\s*\(")


def rebuilt_pages(source: str, page_classes: Iterable[str],
                  only: Optional[Iterable[str]] = None) -> List[Dict]:
    """Helper methods that construct a page mid-flow instead of continuing the chain.

    Page objects chain: an action that leaves a page returns the next one, and the
    Helper keeps each page on its field. The one page a Helper constructs is the
    first, right after navigating. A `new SomePage(...)` with no navigation before
    it in the same method rebuilds a page the previous step already returned.

    Returns [{"method": "Type.name(Params)", "page": "SomePage"}], limited to the
    methods in `only` when it is given (the ones this run wrote).
    """
    pages = set(page_classes or ())
    wanted = set(only) if only is not None else None
    found = []
    for key, method in _methods(source).items():
        if wanted is not None and key not in wanted:
            continue
        body = _STRING.sub('""', method["body"])
        for match in _NEW_TYPE.finditer(body):
            if match.group(1) in pages and not _NAVIGATION.search(body[:match.start()]):
                found.append({"method": key, "page": match.group(1)})
    return found


def copied_methods(sources: Dict[str, str],
                   only: Optional[Dict[str, Iterable[str]]] = None) -> List[List[str]]:
    """Methods written out in full in more than one file, body for body.

    Each group is `["path::Type.name(Params)", ...]`. Narrower than near_duplicates,
    which also matches a copy that changed a literal: only an exact copy can be
    reduced to one without deciding which variant is right. `only` limits which
    methods count, per path, as near_duplicates does.
    """
    by_body: Dict[str, List[str]] = {}
    for path, source in (sources or {}).items():
        methods = _methods(source)
        wanted = set(only.get(path) or ()) if only is not None else set(methods)
        for key in sorted(wanted & set(methods)):
            method = methods[key]
            if not method["constructor"] and len(_call_sequence(method["body"])) >= MIN_DUPLICATE_CALLS:
                by_body.setdefault(method["body"], []).append(f"{path}::{key}")
    return [ids for ids in by_body.values() if len({i.split("::", 1)[0] for i in ids}) > 1]


def known_members(root, paths: Iterable[str]) -> Dict[str, set]:
    """{type name: its method, constructor and field names}, under every name it goes by.

    What a reuse claim can point at. A nested type is listed as `Outer.Inner` and
    as `Inner`, since a plan may name it either way.
    """
    out: Dict[str, set] = {}
    for path in _java_files(Path(root), paths):
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for owner in _types(source):
            names = {m["name"] for m in owner["members"] if m.get("name")}
            for alias in {owner["name"], owner["name"].rsplit(".", 1)[-1]}:
                out.setdefault(alias, set()).update(names)
    return out


_MEMBER_REFERENCE = re.compile(r"((?:[A-Z]\w*\.)*[A-Z]\w*)\.(\w+)\s*\(")


def parse_member_reference(text: str) -> Optional[tuple]:
    """`Type.method(...)` named in free text → (type, method), or None."""
    match = _MEMBER_REFERENCE.search(text or "")
    return (match.group(1), match.group(2)) if match else None


def is_known(known: Dict[str, set], owner: str, member: str) -> bool:
    """Whether `owner.member` exists — or is a getter Lombok writes for one of its fields."""
    names = known.get(owner) or known.get(owner.rsplit(".", 1)[-1]) or set()
    if member in names:
        return True
    for prefix in ("get", "is"):
        if member.startswith(prefix) and len(member) > len(prefix):
            field = member[len(prefix)].lower() + member[len(prefix) + 1:]
            if field in names:
                return True
    return False
