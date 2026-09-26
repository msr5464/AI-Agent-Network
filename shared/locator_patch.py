"""Applying a located locator to the page object, and proving it first.

Finding a working locator is the easy half. This is the half that decides whether
a human can trust the change: the edit is located by field identity rather than by
searching for a selector string, it goes through the same guards every other
autofix does, and nothing is written until the locator has been proved more than
once against a live page.

The prototype wrote the file and reverted on failure. Ordering it the other way —
prove, then write — means a failed heal leaves no trace at all.
"""
from __future__ import annotations
import datetime as dt
import json
import pathlib
import re
from typing import Optional

from shared import edit_guards, locator_capture, locator_verify, page_identity


# ----------------------------------------------------------------- the edit

def declaration_edit(source: str, field: str, new_expression: str) -> tuple[Optional[dict], str]:
    """A search/replace edit that rewrites one page-object field's locator.

    Located by field name, never by searching for the selector string: two page
    objects can legitimately declare the same selector, and the one we located is
    the only one we may touch.

    Handles both shapes this repo uses — a `Locator` field with an initialiser,
    and an accessor method that returns one. Returns (edit, error).
    """
    if not source or not field:
        return None, "no source or field name"

    pattern = re.compile(
        r"(?P<lead>\b" + re.escape(field) + r"\b\s*"
        r"(?:\(\s*\)\s*\{\s*return\s+|=\s*))"
        r"(?P<expr>.+?)(?=\s*;)", re.S)
    match = pattern.search(source)
    if not match:
        return None, f"no declaration of {field} found in the page object"

    old_expression = match.group("expr").strip()
    if old_expression == new_expression:
        return None, "located the same locator that is already there"

    # The field name is carried into old_string so the match is unique even when
    # the selector itself appears elsewhere. apply_edits refuses ambiguity rather
    # than guessing, which is the behaviour we want.
    return {"old_string": match.group("lead") + old_expression,
            "new_string": match.group("lead") + new_expression}, ""


def apply_to_source(source: str, field: str, new_expression: str) -> tuple[Optional[str], str]:
    """Rewrite one field, through the same guards as any other autofix.

    `no_selector_broadening` matters here specifically: the emit ladder drops
    scope when it cannot find anything better, and a broader selector is how a
    wrong-page failure gets papered over into a pass.
    """
    edit, error = declaration_edit(source, field, new_expression)
    if edit is None:
        return None, error
    updated, error = edit_guards.apply_edits(source, [edit])
    if updated is None:
        return None, error
    ok, reason = edit_guards.no_selector_broadening(source, updated)
    if not ok:
        return None, reason
    return updated, ""


# -------------------------------------------------------------- proving it

def confirm(browser, url: str, selector: str, action: str, element: dict, post,
            runs: int = 2, storage_state=None, replay=None) -> tuple[bool, str]:
    """R6 — does it hold up more than once?

    One green run proves the locator resolves. Repeating it in fresh contexts is
    what separates a fix from a coincidence.
    """
    for attempt in range(runs):
        context = browser.new_context(storage_state=storage_state,
                                      viewport=locator_capture.VIEWPORT)
        try:
            page = context.new_page()
            (replay or (lambda p: p.goto(url)))(page)
            locator_capture.settle(page)
            result = locator_verify.verify(page, selector, action, element, post=post)
            if not result.ok:
                return False, f"confirmation run {attempt + 1}/{runs} failed: {result.reason}"
        finally:
            context.close()
    return True, f"held across {runs} confirmation runs"


def collisions(page, source_by_page_object: dict, healed_field: str,
               new_selector: str) -> list[str]:
    """Does the new locator now point at an element another locator already owns?

    Distinct from shared.blast_radius, which answers a code-graph question —
    which *tests* reach this page object. This is the runtime question: two
    locators resolving to the same element means two tests exercise one control
    while both still pass, which no rerun will reveal.
    """
    from shared import locator_capture

    problems: list[str] = []
    snapshot = locator_capture.snapshot(page)
    count, healed = locator_capture.find_by_locator(page, new_selector, snap=snapshot)
    if count != 1 or healed is None:
        return [f"healed locator resolves to {count} elements on a clean load"]

    for page_object, source in (source_by_page_object or {}).items():
        for declared in page_identity.extract_locators(source):
            name = declared.get("name") or ""
            if not name or name == healed_field:
                continue
            try:
                other_count, other = locator_capture.find_by_locator(
                    page, declared["raw"], snap=snapshot)
            except Exception:
                continue
            if other_count == 1 and other is not None and other["index"] == healed["index"]:
                problems.append(
                    f"collides with {page_object}#{name}: both now resolve to "
                    f"<{other['tag']}> {(other.get('accessible_name') or '')[:30]!r}")
    return problems


# ------------------------------------------------------- baseline maintenance

def update_baseline(path: pathlib.Path, field: str, new_locator: str,
                    fingerprint: dict, score: float,
                    source_expression: str = "") -> None:
    """Re-record the healed locator against the element it now matches.

    Skipping this is how a self-healing system decays: every later comparison
    would be made against the pre-drift element, so accuracy drops run over run.
    The history is also what lets a locator that has healed three times ask for a
    stable test id instead of a fourth heal.
    """
    if not path.exists():
        return
    record = json.loads(path.read_text())
    record.setdefault("healHistory", {}).setdefault(field, []).append({
        "healedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "to": source_expression or new_locator,
        "score": round(score, 3),
    })
    if fingerprint:
        record.setdefault("fingerprints", {})[field] = {
            k: v for k, v in fingerprint.items() if k != "_gt"}
    path.write_text(json.dumps(record, indent=2))


def pr_section(result, confirm_note: str, collision_notes: list[str],
               old_locator: str, reached_by: Optional[list] = None) -> str:
    """What a reviewer needs in order to disagree with the change.

    Includes the rejected candidates: a heal presented without its alternatives
    asks for trust instead of review.
    """
    emitted = result.emitted or {}
    lines = [
        f"### Healed `{result.locator_id}`", "",
        "| | |", "|---|---|",
        f"| Was | `{old_locator}` |",
        f"| Now | `{emitted.get('java') or emitted.get('sel')}` |",
        f"| Strategy | {emitted.get('strategy')} |",
        f"| Score | {result.score:.3f} (margin {result.margin:+.3f} over runner-up) |",
        f"| Found by | {result.tier} |",
        f"| Verification | {result.verification} — {confirm_note} |",
        "",
    ]
    rows = sorted(result.breakdown_rows or [], key=lambda r: -r["contrib"])[:6]
    if rows:
        lines += ["<details><summary>Score breakdown</summary>", "",
                  "| property | weight | match | contribution |", "|---|---|---|---|"]
        lines += [f"| {r['prop']} | {r['weight']} | {r['op']} | {r['contrib']} |" for r in rows]
        lines += ["", "</details>", ""]
    if result.top_rejected:
        lines += ["**Rejected candidates**", ""]
        lines += [f"- `<{r['tag']}>` {str(r['name'])[:40]!r} — score {r['score']}, {r['tier']}"
                  for r in result.top_rejected]
        lines.append("")
    if emitted.get("fragile"):
        lines += [f"> **Fragile:** {emitted['fragile']}", ""]
    if reached_by:
        lines += [f"**Reached by** {len(reached_by)} test(s): "
                  + ", ".join(str(t) for t in reached_by[:5])
                  + (" …" if len(reached_by) > 5 else ""), ""]
    lines.append("**Collisions:** " + ("none with other page-object locators"
                                       if not collision_notes
                                       else "\n".join(f"- ⚠️ {c}" for c in collision_notes)))
    return "\n".join(lines)
