"""Candidate generation — the T0..T5 ladder.

Every scorable element on the page is a candidate (that is T4, the guaranteed
fallback). The cheaper tiers do not produce a *separate* candidate list; they
annotate elements they can reach by a direct, clean query. That annotation is
worth a lot: a tier-0/1 hit is a unique match on a strong identity signal, which
lets the decision layer skip the ambiguity margin, and it hands `emit` a
ready-made selector instead of one reverse-engineered from a fingerprint.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

from shared import locator_capture as capture
from shared.locator_score import Volatility, jaro_winkler

# Tier ordering, strongest evidence first.
TIERS = ["T0_literal", "T1_identity", "T2_semantic", "T3_anchored", "T4_scan", "T5_visual"]


@dataclass
class Candidate:
    index: int
    el: dict
    tiers: set[str] = field(default_factory=set)
    via: list[str] = field(default_factory=list)
    score: float = 0.0
    breakdown: list[dict] = field(default_factory=list)

    @property
    def best_tier(self) -> str:
        for t in TIERS:
            if t in self.tiers:
                return t
        return "T4_scan"


# ------------------------------------------------------------ T0: literal repair

def _split_scope(sel: str) -> list[str]:
    """Split a selector on descendant whitespace only.

    Naive whitespace splitting tears quoted text apart: `:text-is("Save changes")`
    becomes a fragment ending in `changes")`. Track quote and bracket depth.
    """
    parts, buf, depth, quote = [], [], 0, ""
    for ch in sel:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch; buf.append(ch); continue
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth = max(0, depth - 1)
        if ch.isspace() and depth == 0:
            if buf:
                parts.append("".join(buf)); buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def literal_mutations(raw: str, base_el: dict, vol: Volatility) -> list[tuple[str, str]]:
    """Mechanical repairs of the broken selector itself.

    Cheapest possible fix and it needs no baseline: if the only thing that
    changed is a hash suffix or an inserted wrapper div, we never have to score
    anything.
    """
    out: list[tuple[str, str]] = []

    seen: set[str] = {raw}

    def push(label: str, sel: str):
        sel = (sel or "").strip()
        if sel and sel not in seen:
            seen.add(sel)
            out.append((label, sel))

    # id selector -> prefix/substring match (survives generated suffixes)
    for m in re.finditer(r"#([A-Za-z_][\w-]*)", raw):
        ident = m.group(1)
        push("id-prefix", raw.replace(m.group(0), f'[id^="{ident}"]'))
        push("id-substring", raw.replace(m.group(0), f'[id*="{ident}"]'))

    # drop the tag qualifier: button#x -> #x  (component-library tag swaps)
    push("drop-tag-qualifier", re.sub(r"(^|\s|>)([a-z]+)(?=[#.\[])", r"\1", raw).strip())

    # positional predicates are the first thing to rot
    push("drop-nth", re.sub(r":nth-(child|of-type)\([^)]*\)", "", raw))

    # child combinator -> descendant (survives inserted wrapper divs)
    if ">" in raw:
        push("relax-child-combinator", re.sub(r"\s*>\s*", " ", raw))

    # exact text -> substring, case-insensitive (survives re-casing/re-wording)
    for m in re.finditer(r':text-is\((["\'])(.*?)\1\)', raw):
        push("relax-exact-text", raw.replace(m.group(0), f':has-text("{m.group(2)}")'))
    for m in re.finditer(r':has-text\((["\'])(.*?)\1\)', raw):
        first = m.group(2).split()[0] if m.group(2).split() else ""
        if first and first != m.group(2):
            push("shorten-text", raw.replace(m.group(0), f':has-text("{first}")'))

    # attribute value -> substring; and the same value under a sibling testid name
    for m in re.finditer(r'\[([\w-]+)([~^*$|]?=)(["\'])(.*?)\3\]', raw):
        attr, _, _, val = m.groups()
        push("attr-substring", raw.replace(m.group(0), f'[{attr}*="{val}"]'))
        for alt in ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy"):
            if attr.startswith("data-") and alt != attr:
                push(f"attr-rename:{alt}", raw.replace(m.group(0), f'[{alt}="{val}"]'))

    # progressively drop leading scope segments (element moved out of a container)
    parts = _split_scope(raw)
    for i in range(1, len(parts)):
        tail = " ".join(parts[i:]).lstrip("> ")
        if tail:
            push(f"drop-scope-{i}", tail)

    # class selectors: keep only the classes that are not machine-generated
    classes = re.findall(r"\.([\w-]+)", raw)
    stable = vol.stable_classes(classes)
    if classes and stable != classes and stable:
        push("drop-volatile-classes", "".join(f".{c}" for c in stable))

    return out


# ------------------------------------------------------- T1/T2/T3/T5 queries

def identity_queries(base_el: dict) -> list[tuple[str, str]]:
    """Strong identity attributes recorded on the green run."""
    q: list[tuple[str, str]] = []
    if base_el.get("testid"):
        for a in ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy"):
            q.append(("testid", f'[{a}="{base_el["testid"]}"]'))
    if base_el.get("id"):
        q.append(("id", f'[id="{base_el["id"]}"]'))
    if base_el.get("name"):
        q.append(("name", f'[name="{base_el["name"]}"]'))
    if base_el.get("aria_label"):
        q.append(("aria-label", f'[aria-label="{base_el["aria_label"]}"]'))
    if base_el.get("placeholder"):
        q.append(("placeholder", f'[placeholder="{base_el["placeholder"]}"]'))
    if base_el.get("href"):
        q.append(("href", f'[href="{base_el["href"]}"]'))
    return q


def anchored_queries(base_el: dict, snap: dict) -> list[tuple[str, str]]:
    """Search inside the nearest ancestor that still exists.

    Walks outward from the element: the closer the surviving anchor, the more
    the structure below it still means what it used to.
    """
    out = []
    present_ids = {e["id"] for e in snap["elements"] if e.get("id")}
    present_testids = {e["testid"] for e in snap["elements"] if e.get("testid")}
    tag = base_el["tag"]
    for anc in base_el.get("ancestor_chain", []):
        anchor = None
        if anc.get("testid") and anc["testid"] in present_testids:
            anchor = f'[data-testid="{anc["testid"]}"]'
        elif anc.get("id") and anc["id"] in present_ids:
            anchor = f'#{anc["id"]}'
        if not anchor:
            continue
        out.append(("anchored-tag", f"{anchor} {tag}"))
        if base_el.get("role"):
            out.append(("anchored-role", f'{anchor} [role="{base_el["role"]}"], {anchor} {tag}'))
        break                                   # nearest surviving anchor only
    return out


def _resolve_unique(ctx, snap: dict, selector: str) -> dict | None:
    try:
        n, fp = capture.find_by_locator(ctx, selector, snap=snap)
    except Exception:
        return None
    return fp if n == 1 else None


def surviving_anchor(baseline: dict, snap: dict) -> dict | None:
    """Nearest ancestor from the green run that still exists on this page."""
    ids = {e["id"] for e in snap["elements"] if e.get("id")}
    testids = {e["testid"] for e in snap["elements"] if e.get("testid")}
    for anc in baseline["element"].get("ancestor_chain", []):
        if anc.get("testid") and anc["testid"] in testids:
            return {"testid": anc["testid"], "label": f'[data-testid={anc["testid"]}]'}
        if anc.get("id") and anc["id"] in ids:
            return {"id": anc["id"], "label": f'#{anc["id"]}'}
    return None


def within(el: dict, anchor: dict) -> bool:
    for a in el.get("ancestor_chain") or []:
        if anchor.get("id") and a.get("id") == anchor["id"]:
            return True
        if anchor.get("testid") and a.get("testid") == anchor["testid"]:
            return True
    return False


def gather(ctx, snap: dict, baseline: dict, cfg: dict, vol: Volatility) -> list[Candidate]:
    """Build the annotated candidate set for one search scope."""
    base_el = baseline["element"]
    raw = baseline["raw_locator"]

    cands: dict[int, Candidate] = {
        e["index"]: Candidate(index=e["index"], el=e)
        for e in capture.scorable(snap["elements"])
    }

    def mark(fp: dict | None, tier: str, via: str):
        if fp is None:
            return
        c = cands.get(fp["index"])
        if c is None:                             # matched a non-scorable node
            c = cands[fp["index"]] = Candidate(index=fp["index"], el=fp)
        c.tiers.add(tier)
        c.via.append(f"{tier}:{via}")

    # Recorded fallbacks from the last successful run: the richer prior we saved
    # precisely so the next drift does not start from a single string.
    for sel in baseline.get("fallbacks") or []:
        mark(_resolve_unique(ctx, snap, sel), "T1_identity", f"recorded-fallback {sel}")

    # T0 — repair the literal
    for label, sel in literal_mutations(raw, base_el, vol):
        mark(_resolve_unique(ctx, snap, sel), "T0_literal", sel)

    # T1 — identity attributes from the baseline
    for label, sel in identity_queries(base_el):
        mark(_resolve_unique(ctx, snap, sel), "T1_identity", sel)

    # T2 — role + accessible name. The tier that survives full restructures.
    role, acc = base_el.get("role"), base_el.get("accessible_name")
    if role and acc:
        try:
            loc = ctx.get_by_role(role, name=acc, exact=True)
            if loc.count() == 1:
                _, fp = capture.find_by_locator(
                    ctx, f'internal:role={role}[name="{acc}"s]', snap=snap)
                if fp is None:
                    idx = ctx.evaluate(capture.script(), loc.first.element_handle())
                    fp = snap["elements"][idx] if 0 <= idx < len(snap["elements"]) else None
                mark(fp, "T2_semantic", f'role={role} name="{acc}"')
        except Exception:
            pass
        # fuzzy accessible-name match within the same role
        floor = cfg["thresholds"]["role_fuzzy"]
        same_role = [c for c in cands.values() if c.el.get("role") == role
                     and c.el.get("accessible_name")]
        ranked = sorted(same_role,
                        key=lambda c: -jaro_winkler(acc, c.el["accessible_name"]))
        for c in ranked[:3]:
            if jaro_winkler(acc, c.el["accessible_name"]) >= floor:
                c.tiers.add("T2_semantic")
                c.via.append(f'T2_semantic:fuzzy-name~{c.el["accessible_name"]!r}')

    # T3 — anchored structural
    for label, sel in anchored_queries(base_el, snap):
        mark(_resolve_unique(ctx, snap, sel), "T3_anchored", sel)

    # T5 — visual fallback: nearest compatible element to where it used to be.
    # Position is a *feature*, never a grouping strategy — VON-style visual
    # merging measured worse than plain Similo on the larger benchmark.
    bb = base_el["bbox_norm"]
    compatible = [c for c in cands.values()
                  if c.el["is_interactive"] == base_el["is_interactive"]]
    by_distance = sorted(compatible, key=lambda c: (
        (c.el["bbox_norm"]["x"] - bb["x"]) ** 2 + (c.el["bbox_norm"]["y"] - bb["y"]) ** 2))
    for c in by_distance[:3]:
        c.tiers.add("T5_visual")
        c.via.append("T5_visual:nearest-bbox")

    return list(cands.values())
