"""Execution-based verification.

A candidate is a hypothesis until it has been executed. This is the step the
published relocalization work skips entirely — Similo, VON Similo and Healenium
all stop at "highest-scoring similar element" and hand it straight back to the
test.

Honesty about strength: without the test's own next assertion we can only prove
the action was *possible*, not that it was *correct*. That distinction is
reported, not hidden, so a WEAK verification never masquerades as proof.
"""
from __future__ import annotations
from dataclasses import dataclass

STRONG, WEAK, FAILED = "STRONG", "WEAK", "FAILED"

FILLABLE = {"input", "textarea"}
NON_TEXT_INPUT = {"checkbox", "radio", "submit", "button", "image", "reset", "file"}


@dataclass
class VerifyResult:
    ok: bool
    strength: str
    reason: str
    step: str = ""

    def __str__(self) -> str:
        return f"{self.strength}: {self.reason}"


def affordance_ok(el: dict, action: str) -> tuple[bool, str]:
    """Is this action even legal for this element?

    Catches the embarrassing class of wrong-heal where a high-scoring <div>
    wins a fill() step because it happened to sit where the input used to.
    """
    tag, typ = el["tag"], (el.get("type") or "").lower()
    if action == "fill":
        if tag not in FILLABLE and not el["attrs"].get("contenteditable"):
            return False, f"cannot fill a <{tag}>"
        if tag == "input" and typ in NON_TEXT_INPUT:
            return False, f"cannot fill an input[type={typ}]"
    elif action == "select":
        if tag != "select":
            return False, f"cannot select_option on a <{tag}>"
    elif action == "click":
        # Deliberately permissive, unlike fill and select above.
        #
        # Interaction handlers routinely live on a wrapper: an <img> pencil icon
        # inside a <span class="cursor-pointer"> is a real and extremely common
        # pattern, and refusing it rejects correct heals. Clicking is also legal
        # on anything — Playwright dispatches a real mouse event at the position
        # — so element type says little here.
        #
        # The checks that matter for a click come after this one: the action is
        # actually performed, and the post-condition decides whether it did
        # anything. Type, by contrast, is the ONLY thing that can catch a fill on
        # a <div> before it happens, which is why that one stays strict.
        return True, ""
    return True, ""


def perform(ctx, selector: str, action: str, timeout: int = 3000) -> None:
    loc = ctx.locator(selector)
    if action == "click":
        loc.click(timeout=timeout)
    elif action == "fill":
        loc.fill("healcheck", timeout=timeout)
    elif action == "select":
        # Pick an option other than the current one, or nothing observable happens.
        n_opts = loc.evaluate("el => (el.options && el.options.length) || 0")
        loc.select_option(index=1 if n_opts > 1 else 0, timeout=timeout)
    elif action in ("hover",):
        loc.hover(timeout=timeout)
    else:
        loc.wait_for(state="visible", timeout=timeout)


def error_banner(ctx) -> str | None:
    try:
        return ctx.evaluate("""() => {
            const n = document.querySelector('[role="alert"], .error-message, .alert-danger');
            return n && n.offsetParent !== null ? (n.textContent || '').trim().slice(0,120) : null;
        }""")
    except Exception:
        return None


def post_from_spec(spec: dict | None):
    """Build a post-condition callable from a declarative {js, equals|contains}.

    In the real framework this is the test's own next assertion. Here it is
    declared per locator so the ladder is genuinely exercised rather than always
    degrading to WEAK.
    """
    if not spec:
        return None

    def check(ctx):
        got = ctx.evaluate(spec["js"])
        if "equals" in spec:
            return got == spec["equals"], f"expected {spec['equals']!r}, got {got!r}"
        if "contains" in spec:
            return (got or "").find(spec["contains"]) >= 0, \
                   f"expected to contain {spec['contains']!r}, got {got!r}"
        return got is not None, f"got {got!r}"

    return check


def verify(ctx, selector: str, action: str, el: dict, post=None) -> VerifyResult:
    """Run the full six-step check on one candidate."""
    # 1. resolves to exactly one element
    try:
        n = ctx.locator(selector).count()
    except Exception as e:
        return VerifyResult(False, FAILED, f"selector rejected: {type(e).__name__}", "resolve")
    if n != 1:
        return VerifyResult(False, FAILED, f"resolves to {n} elements, need exactly 1", "resolve")

    # 2. actionable
    loc = ctx.locator(selector)
    try:
        if not loc.is_visible():
            return VerifyResult(False, FAILED, "element is not visible", "actionable")
        if not loc.is_enabled():
            return VerifyResult(False, FAILED, "element is disabled", "actionable")
    except Exception as e:
        return VerifyResult(False, FAILED, f"actionability check threw {type(e).__name__}", "actionable")

    # 3. affordance — the action must make sense for this element
    ok, why = affordance_ok(el, action)
    if not ok:
        return VerifyResult(False, FAILED, why, "affordance")

    # 4. perform the original action
    before_url = ctx.url if hasattr(ctx, "url") else None
    try:
        perform(ctx, selector, action)
    except Exception as e:
        return VerifyResult(False, FAILED,
                            f"{action} failed: {type(e).__name__}: {str(e).splitlines()[0][:90]}",
                            "action")

    # 5. nothing blew up
    banner = error_banner(ctx)
    if banner:
        return VerifyResult(False, FAILED, f"error appeared after action: {banner!r}", "postcondition")

    # 6. post-condition. A caller-supplied check (the test's next assertion) is
    #    real proof; without one we can only report that the action went through.
    if post is not None:
        try:
            good, detail = post(ctx)
        except Exception as e:
            return VerifyResult(False, FAILED, f"post-condition threw {type(e).__name__}", "postcondition")
        if not good:
            return VerifyResult(False, FAILED, f"post-condition failed: {detail}", "postcondition")
        return VerifyResult(True, STRONG, f"action succeeded and post-condition held ({detail})")

    changed = (ctx.url != before_url) if before_url is not None else False
    return VerifyResult(True, WEAK,
                        f"{action} completed cleanly"
                        + (" and navigation occurred" if changed else
                           "; no assertion available to confirm intent"))
