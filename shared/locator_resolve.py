"""Locator resolution: R0..R6 with circuit breakers.

Given a locator that stopped matching, work out which element it meant, prove the
answer by performing the step, and hand back a locator that can be written into
the page object. Diagnosis (is this even a locator problem?) is answered by
locator_classify; this module owns the search and the proof.

Each retry loop has its own trigger, budget and exit condition. They are not
"try the same thing N times" — R0 asks "was it just slow?", R1 asks "are we in
the wrong state?", R2 "can the selector repair itself?", R3 "does the candidate
actually work?", R4 "am I looking in the wrong place?", R5 "is this a semantic
change I need help with?", R6 "does it hold up more than once?".
"""
from __future__ import annotations
import datetime as dt
import json, pathlib, time
from dataclasses import dataclass, field

from shared import browser_mode
from shared import locator_capture as capture
from shared import locator_candidates as candidates
from shared import locator_classify as classify
from shared import locator_decide as decide_mod
from shared import locator_emit as emit_mod
from shared import locator_patch, locator_verify as verify_mod
from shared.locator_score import Volatility

HEALED, NO_HEAL = "HEALED", "NO_HEAL"


class HealSession:
    """See `check` and `record`."""
    """Suite-wide state across heals: enforces the per-run cap, one attempt per
    locator, and backoff once the application itself starts failing."""

    def __init__(self):
        self.heals = 0
        self.attempted: set[str] = set()
        self.app_errors = 0

    def check(self, locator_id: str, cfg: dict) -> tuple[str, str] | None:
        if locator_id in self.attempted:
            return ("ALREADY_ATTEMPTED",
                    "already attempted this locator in this run — one shot per run")
        cap = cfg["budgets"]["max_heals_per_run"]
        if self.heals >= cap:
            return ("RUN_CAP_REACHED",
                    f"{self.heals} locators already healed this run (cap {cap}) — "
                    f"this looks like a deploy failure or a redesign, not drift")
        if self.app_errors >= cfg["budgets"]["app_error_backoff"]:
            return ("APP_UNSTABLE",
                    f"{self.app_errors} app-level failures this run — backing off")
        return None

    def record(self, res: "HealResult") -> None:
        self.attempted.add(getattr(res, "locator_id", "") or "")
        if res.verdict == HEALED:
            self.heals += 1
        if res.classification in ("APP_BUG", "WRONG_STATE"):
            self.app_errors += 1


@dataclass
class HealResult:
    verdict: str
    reason: str
    locator_id: str = ""
    classification: str = ""
    picked_gt: str | None = None          # ground truth of the chosen element (eval only)
    picked_el: dict = field(default_factory=dict)
    emitted: dict | None = None
    score: float = 0.0
    margin: float = 0.0
    tier: str = ""
    verification: str = ""
    breakdown_rows: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    top_rejected: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0

    def log(self, loop: str, detail: str):
        self.attempts.append({"loop": loop, "detail": detail})


# ------------------------------------------------------------------ baselines

def record_baselines(page, url: str, locators: dict, out_dir: pathlib.Path,
                     app: str = "fixture", cfg: dict | None = None) -> int:
    """Phase A. Runs on green: fingerprint every locator that still resolves."""
    page.goto(url)
    snap = capture.snapshot(page)
    commit = capture.app_commit()
    shots = bool((cfg or {}).get("capture", {}).get("screenshots", False))
    vol = Volatility(cfg) if cfg else None
    written = 0
    for lid, spec in locators.items():
        n, fp = capture.find_by_locator(page, spec["raw"], snap=snap)
        if n != 1 or fp is None:
            print(f"  skip {lid}: {n} matches")
            continue
        record = {
            "locator_id": lid, "raw_locator": spec["raw"], "action": spec.get("action", "click"),
            # How the test uses this locator. Interaction locators may be healed;
            # assertion locators may not.
            "usage": spec.get("usage", "interaction"),
            "post": spec.get("post"),
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "url": snap["url"], "url_pattern": snap["url"].rsplit("/", 1)[-1],
            "app_commit": commit, "match_count": n,
            "frame_path": capture.resolve_frame(page, spec["raw"])[1],
            "screenshot_crop": capture.element_screenshot(page, spec["raw"]) if shots else None,
            "fallbacks": ([a["sel"] for a in emit_mod.alternates(
                page, fp, vol, snap, skip=spec["raw"])] if vol else []),
            "element": {k: v for k, v in fp.items() if k != "_gt"},
            "context": {"landmarks": snap["landmarks"], "viewport": snap["viewport"],
                        "digest": capture.page_digest(snap["landmarks"])},
            "history": [],
        }
        capture.write_baseline(out_dir / app / f"{lid.replace('#', '__')}.json", record)
        written += 1
    return written


def load_baseline(base_dir: pathlib.Path, app: str, locator_id: str) -> dict:
    return json.loads((base_dir / app / f"{locator_id.replace('#', '__')}.json").read_text())


def baseline_for(page_object: str, field: str, raw_locator: str, record: dict,
                 action: str = "click", usage: str = "interaction",
                 post: dict | None = None) -> dict | None:
    """Engine-shaped baseline from the page-object record Baseline.java writes.

    The Java side stores one file per page object, keyed by field; the engine
    works per locator. This is the seam between the two, and the only place that
    knows both shapes.
    """
    fingerprint = (record.get("fingerprints") or {}).get(field)
    if not fingerprint:
        return None
    history = (record.get("healHistory") or {}).get(field) or []
    return {
        "locator_id": f"{page_object}#{field}",
        "raw_locator": raw_locator,
        "action": action,
        "usage": usage,
        "post": post,
        "element": fingerprint,
        "context": {
            "landmarks": record.get("landmarks") or [],
            "url_shape": record.get("url_shape", ""),
        },
        "fallbacks": (record.get("fallbacks") or {}).get(field) or [],
        # Normalised to what the circuit breaker reads.
        "history": [{"healed_at": h.get("healedAt", ""), "from": h.get("from", ""),
                     "to": h.get("to", ""), "score": h.get("score", 0)}
                    for h in history],
    }


# --------------------------------------------------------------- retry loops

def r0_resolve(ctx, raw: str, snap: dict, res: HealResult) -> tuple[int, dict | None, dict]:
    """R0 — was it just slow? Progressive waits before we conclude 'gone'."""
    for i, wait in enumerate(("none", "load", "networkidle")):
        n, fp = capture.find_by_locator(ctx, raw, snap=snap)
        if n > 0:
            if i:
                res.log("R0_timing", f"resolved after waiting for {wait}")
            return n, fp, snap
        if wait == "none":
            continue
        try:
            ctx.wait_for_load_state(wait, timeout=2000)
        except Exception:
            pass
        snap = capture.snapshot(ctx)
    res.log("R0_timing", "still unresolved after load + networkidle waits")
    return 0, None, snap


def r1_state(ctx, raw: str, snap: dict, res: HealResult) -> tuple[int, dict | None, dict]:
    """R1 — are we blocked rather than broken? Clear overlays, reveal the element."""
    actions = [
        ("dismiss-overlay", """() => {
            const sel = '[role=dialog] button[aria-label*=close i], .cookie-banner button,'
                      + ' button[aria-label*=dismiss i], .modal-close';
            const b = document.querySelector(sel); if (b) { b.click(); return true; } return false;
        }"""),
        ("scroll-bottom", "() => { window.scrollTo(0, document.body.scrollHeight); return true; }"),
        ("expand-collapsed", """() => {
            let hit = false;
            document.querySelectorAll('[aria-expanded=false]').forEach(e => { e.click(); hit = true; });
            return hit;
        }"""),
    ]
    for name, js in actions:
        try:
            if not ctx.evaluate(js):
                continue
        except Exception:
            continue
        snap = capture.snapshot(ctx)
        n, fp = capture.find_by_locator(ctx, raw, snap=snap)
        if n > 0:
            res.log("R1_state", f"{name} revealed the element — state issue, not locator drift")
            return n, fp, snap
        res.log("R1_state", f"{name} applied, still unresolved")
    return 0, None, snap


def scopes(page, baseline: dict, snap: dict):
    """R4 — widen the search one ring at a time.

    Narrowest first: structure that survived from the green run is real evidence,
    and searching inside it avoids distant look-alikes. Each ring that fails
    hands over to a wider one.
    """
    anchor = candidates.surviving_anchor(baseline, snap)
    if anchor:
        yield f"container:{anchor['label']}", page, anchor
    yield "main", page, None
    for i, f in enumerate(page.frames[1:], 1):
        yield f"frame[{i}]:{(f.name or f.url)[:36]}", f, None
    try:
        for i, other in enumerate(page.context.pages):
            if other is not page:
                yield f"popup[{i}]:{other.url[:36]}", other, None
    except Exception:
        pass


# ---------------------------------------------------------------- LLM escalation

LLM_DATA_MARKER = "----- DATA -----"


def claude_picker(model: str = "sonnet", cwd: str = ".", log_dir=None,
                  timeout: int = 120):
    """An R5 picker backed by this repo's existing claude call path.

    Returned as a callable rather than wired in directly for two reasons: tests
    stub it, and the deterministic path must never depend on a model being
    reachable. R5 only runs when scoring is ambiguous or empty, so on a healthy
    suite this is never called at all.
    """
    from shared import claude

    def pick(prompt: str) -> dict | None:
        raw = claude.call_claude(prompt, model=model, cwd=cwd, timeout=timeout,
                                 log_dir=log_dir)
        if not raw:
            return None
        # The reply is asked for as bare JSON but may arrive wrapped in prose.
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            answer = json.loads(raw[start:end + 1])
        except ValueError:
            return None
        return answer if isinstance(answer, dict) else None

    return pick


def build_llm_prompt(baseline: dict, ranked: list, k: int, snap: dict | None = None) -> str:
    """R5 prompt. Deliberately provider-neutral — this repo already routes LLM
    calls through LLM_PROVIDER (ollama/openai/google), so the adapter is wired
    at integration time rather than hardcoded here."""
    def brief(el: dict) -> dict:
        return {k2: el.get(k2) for k2 in
                ("tag", "role", "accessible_name", "text", "id", "name", "testid",
                 "aria_label", "class_list", "neighbor_texts", "bbox_norm")}
    payload = {
        "intent": {"locator_id": baseline["locator_id"], "action": baseline.get("action"),
                   "broken_locator": baseline["raw_locator"]},
        "target_was": brief(baseline["element"]),
        "candidates": [dict(widget_id=i, score=round(c.score, 3), **brief(c.el))
                       for i, c in enumerate(ranked[:k])],
    }
    if snap:
        payload["page_landmarks"] = snap["landmarks"]
    return (
        "A UI test's locator stopped matching. Below is the element it used to match "
        "(target_was) and the ranked candidates now on the page.\n"
        "Pick the ONE candidate that is the same element, serving the same purpose.\n"
        "If none of them is that element -- for example the feature was removed, or the "
        "element in that position now does something different -- answer -1. "
        "Answering -1 is the correct answer more often than it feels; a wrong pick "
        "silently disables the test.\n"
        'Reply with JSON only: {"widget_id": <int>, "why": "<one sentence>"}\n'
        + LLM_DATA_MARKER + "\n" + json.dumps(payload, indent=1))


# --------------------------------------------------------------------- heal

def verify_in_fresh_context(browser, url: str, selector: str, action: str, el: dict,
                            post, storage_state=None, replay=None):
    """Verify the emitted selector in a clean context.

    The working page has had failed candidates clicked into it; a heal proved on
    that page is proved against polluted state. `storage_state` carries the
    logged-in session so the replay can skip straight to the failing step.
    """
    ctx = browser.new_context(storage_state=storage_state,
                              viewport=capture.VIEWPORT)
    try:
        fresh = ctx.new_page()
        (replay or (lambda p: p.goto(url)))(fresh)
        # The same wait the main path takes. Without it the candidate is checked
        # against a page that has not rendered yet, and a locator that is right
        # comes back as "resolves to 0 elements".
        capture.settle(fresh)
        return verify_mod.verify(fresh, selector, action, el, post=post)
    finally:
        ctx.close()


def heal(page, baseline: dict, cfg: dict, url: str, *, post=None, llm=None,
         explain: bool = False, session: "HealSession | None" = None,
         browser=None, storage_state=None, replay=None,
         assertion_fields: set | None = None,
         page_comparison: dict | None = None) -> HealResult:
    t0 = time.time()
    vol = Volatility(cfg)
    raw, action = baseline["raw_locator"], baseline.get("action", "click")
    res = HealResult(NO_HEAL, "not attempted", locator_id=baseline["locator_id"])
    if post is None:
        post = verify_mod.post_from_spec(baseline.get("post"))

    # Heal how a test FINDS an element; never what it VERIFIES. A healed
    # assertion locator turns a caught regression into a green build, which is
    # the exact failure this whole system exists to avoid.
    #
    # `assertion_fields` is read out of the real source by locator_assertions;
    # the `usage` flag is the fixture-level equivalent. Either is enough.
    field = baseline["locator_id"].split("#")[-1]
    verified_by = (baseline.get("usage") == "assertion"
                   or bool(assertion_fields and field in assertion_fields))
    if verified_by and not cfg["classify"].get("heal_assertions", False):
        res.classification = "ASSERTION_LOCATOR"
        res.reason = ("locator is read by an assertion — reported for review, "
                      "never auto-healed")
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res

    # Suite-level circuit breaker: twenty broken locators is a deploy failure or
    # a redesign, not twenty independent heals.
    if session is not None:
        blocked = session.check(baseline["locator_id"], cfg)
        if blocked:
            res.classification, res.reason = blocked
            res.elapsed_ms = int((time.time() - t0) * 1000)
            return res

    # Circuit breaker: a locator that keeps breaking needs a testid, not a 4th heal.
    hist = baseline.get("history", [])
    window = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg["budgets"]["heal_history_window_days"])
    recent = [h for h in hist if dt.datetime.fromisoformat(h["healed_at"]) > window]
    if len(recent) >= cfg["budgets"]["heal_history_max"]:
        res.reason = (f"circuit breaker: healed {len(recent)}x in "
                      f"{cfg['budgets']['heal_history_window_days']}d — needs a stable test id")
        res.classification = "UNSTABLE_LOCATOR"
        return res

    # Replay to the failing step. A bare goto() only works for apps with no
    # session; anything behind a login needs the caller's replay, and using
    # goto() there silently lands us on the sign-in page -- which the classifier
    # then correctly, and uselessly, reports as WRONG_STATE.
    http_status: int | None = None

    def _note_status(r):
        nonlocal http_status
        if r.request.is_navigation_request() and r.request.frame == page.main_frame:
            http_status = r.status

    page.on("response", _note_status)
    try:
        (replay or (lambda p: p.goto(url)))(page)
    finally:
        page.remove_listener("response", _note_status)

    capture.settle(page)
    snap = capture.snapshot(page)

    # R0 / R1 — rule out timing and state before concluding the locator is wrong.
    n, matched, snap = r0_resolve(page, raw, snap, res)
    if n == 0:
        n, matched, snap = r1_state(page, raw, snap, res)

    # Phase B — the gate.
    verdict = classify.classify(snap, baseline, n, matched, cfg, vol, http_status,
                                page_comparison=page_comparison)
    # Absolute similarity cannot catch a rebind between near-identical siblings:
    # a T-shirt button scores high against a backpack baseline because they ARE
    # nearly identical. The relative question is the sharp one -- is some OTHER
    # element on this page a materially better match than the one we resolved?
    if (verdict.kind == "NOT_LOCATOR" and matched is not None and n == 1
            and matched["is_visible"] and matched["is_enabled"]):
        ranked_now = decide_mod.rank(
            candidates.gather(page, snap, baseline, cfg, vol), baseline, cfg, vol)
        cur = next((c for c in ranked_now if c.index == matched["index"]), None)
        best = ranked_now[0] if ranked_now else None
        if (cur and best and best.index != cur.index
                and best.score - cur.score >= cfg["classify"]["misbound_margin"]):
            verdict = classify.Verdict(
                "MISBOUND",
                f"locator resolves to {(cur.el.get('accessible_name') or cur.el.get('text',''))[:32]!r} "
                f"(score {cur.score:.2f}) but {(best.el.get('accessible_name') or best.el.get('text',''))[:32]!r} "
                f"(score {best.score:.2f}) matches the recorded element better")

    res.classification = verdict.kind
    # A locator that still resolves is not failing. Rebinding it changes what a
    # passing test asserts, with no failure to justify the change -- so MISBOUND
    # is reported for a human by default rather than healed behind their back.
    if verdict.kind == "MISBOUND" and not cfg["classify"].get("heal_misbound", False):
        res.reason = verdict.reason + " — reported, not auto-rebound"
        res.log("classify", str(verdict))
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res
    if not verdict.healable:
        res.reason = verdict.reason
        res.log("classify", str(verdict))
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res
    res.log("classify", str(verdict))

    budget_s = cfg["budgets"]["heal_seconds"]
    best_decision = None

    for scope_name, ctx, anchor in scopes(page, baseline, snap):          # R4
        if time.time() - t0 > budget_s:
            res.reason = "wall-clock budget exhausted"
            break
        scope_snap = snap if scope_name == "main" else capture.snapshot(ctx)
        cands = candidates.gather(ctx, scope_snap, baseline, cfg, vol)   # R2 lives inside (T0)
        if anchor is not None:
            cands = [c for c in cands if candidates.within(c.el, anchor)]
            if not cands:
                continue
        ranked = decide_mod.rank(cands, baseline, cfg, vol)
        d = decide_mod.decide(ranked, cfg)
        best_decision = best_decision or d
        res.log("R4_scope", f"{scope_name}: {len(ranked)} candidates, {d.outcome} ({d.reason})")

        # Only genuine near-ties are alternates. If the decision named a clear
        # winner, failing to synthesise a locator for it is a failure of `emit`
        # -- not evidence for the runner-up. Falling through to a lower-scoring
        # DIFFERENT element there is how a healer silently rebinds a test.
        pool = []
        if d.proceed:
            k = cfg["budgets"]["candidates_to_verify"]
            pool = [c for c in ranked[:k]
                    if d.top.score - c.score < cfg["thresholds"]["margin"]]

        # R5 — escalate ambiguity/low confidence to the model, then verify its pick.
        if not pool and llm is not None:
            for attempt in range(cfg["budgets"]["llm_attempts"]):
                k = cfg["budgets"]["llm_topk" if attempt == 0 else "llm_topk_wide"]
                pick = llm(build_llm_prompt(baseline, ranked, k,
                                            snap if attempt else None))
                res.log("R5_llm", f"attempt {attempt + 1} (top-{k}) -> {pick}")
                if pick is None or pick.get("widget_id", -1) < 0:
                    break
                wid = pick["widget_id"]
                if 0 <= wid < len(ranked):
                    pool = [ranked[wid]]
                    break

        # R3 — try each candidate by actually executing the step.
        for cand in pool:
            if time.time() - t0 > budget_s:
                break
            emitted = emit_mod.emit(ctx, cand.el, vol, scope_snap)
            if emitted is None:
                res.log("R3_verify",
                        f"no stable unique locator could be synthesised for #{cand.index} "
                        f"({cand.el['tag']} {(cand.el.get('accessible_name') or '')[:30]!r})")
                res.classification = "NO_STABLE_LOCATOR"
                continue
            if browser is not None and scope_name.startswith(("main", "container")):
                vr = verify_in_fresh_context(browser, url, emitted["sel"], action,
                                             cand.el, post, storage_state, replay)
            else:
                vr = verify_mod.verify(ctx, emitted["sel"], action, cand.el, post=post)
            res.log("R3_verify", f"{emitted['strategy']} {emitted['sel']} -> {vr}")
            if vr.ok:
                res.verdict = HEALED
                res.reason = d.reason
                res.picked_gt = cand.el.get("_gt")
                res.picked_el = cand.el
                res.emitted = emitted
                res.score, res.margin, res.tier = cand.score, d.margin, cand.best_tier
                res.breakdown_rows = cand.breakdown
                res.verification = vr.strength
                res.top_rejected = [
                    {"tag": r.el["tag"], "name": r.el.get("accessible_name"),
                     "score": round(r.score, 3), "tier": r.best_tier}
                    for r in d.runners]
                res.elapsed_ms = int((time.time() - t0) * 1000)
                return res
            # A failed attempt may have mutated page state; restore it and
            # re-fingerprint so the next candidate's index stays valid.
            (replay or (lambda p: p.goto(url)))(page)
            snap = capture.snapshot(page)
            if scope_name == "main":
                scope_snap = snap

    if res.classification == "NO_STABLE_LOCATOR" and res.reason == "not attempted":
        res.reason = ("found the element but could not express it as a stable unique "
                      "locator — it needs a test id")
    elif best_decision is not None and res.reason == "not attempted":
        res.reason = best_decision.reason
        res.classification = ("LOW_CONFIDENCE"
                              if best_decision.outcome == decide_mod.ESCALATE
                              else res.classification)
    elif res.reason == "not attempted":
        res.reason = "no candidate survived verification"
        res.classification = res.classification or "LOW_CONFIDENCE"
    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


def _reached_by(locator_id: str, sources: dict | None) -> list:
    """Which tests execute the page object we are about to edit.

    A reviewer's first question about a locator change is "what else uses this",
    and shared.blast_radius already answers it from the call graph. Advisory: the
    walk is not worth failing a proven heal over.
    """
    page_object = (locator_id or "").split("#")[0]
    if not page_object or not sources:
        return []
    try:
        from shared import blast_radius, repo_config  # noqa: F401
        import os
        repo = os.environ.get("WORKSPACE_DIR", "")
        name = os.environ.get("GITHUB_REPO_AUTOMATION", "")
        if not repo or not name:
            return []
        found = blast_radius.resolve(str(pathlib.Path(repo) / name),
                                     affects=[page_object])
        return (found or {}).get("tests", [])[:20]
    except Exception:                              # noqa: BLE001 - advisory only
        return []


# ----------------------------------------------------- prove, then write

def resolve_and_apply(browser, page, baseline: dict, cfg: dict, url: str, *,
                      source: str, field: str, sources_by_page_object: dict | None = None,
                      session: "HealSession | None" = None, replay=None,
                      storage_state=None, assertion_fields: set | None = None) -> dict:
    """Locate, prove, then produce the patched source.

    Nothing is written here. Every step that could fail — confirmation, collision
    checks, the edit guards — runs before the caller has a file to write, so a
    heal that cannot be proved leaves no trace rather than needing a revert.
    """
    result = heal(page, baseline, cfg, url, session=session, browser=browser,
                  replay=replay, storage_state=storage_state,
                  assertion_fields=assertion_fields)
    out = {"result": result, "updated_source": None, "confirm": "",
           "collisions": [], "pr_section": None, "error": ""}
    if result.verdict != HEALED:
        return out

    emitted = result.emitted
    new_expression = emitted.get("java") or f'page.locator("{emitted["sel"]}")'

    # R6 — prove it more than once, in fresh contexts, before touching anything.
    ok, note = locator_patch.confirm(
        browser, url, emitted["sel"], baseline.get("action", "click"),
        result.picked_el, verify_mod.post_from_spec(baseline.get("post")),
        runs=cfg["budgets"]["confirm_runs"], storage_state=storage_state, replay=replay)
    out["confirm"] = note
    if not ok:
        result.verdict, result.reason = NO_HEAL, f"failed confirmation: {note}"
        result.classification = "UNCONFIRMED"
        return out

    out["collisions"] = locator_patch.collisions(
        page, sources_by_page_object or {}, field, emitted["sel"])
    if out["collisions"]:
        result.verdict = NO_HEAL
        result.reason = "; ".join(out["collisions"])
        result.classification = "COLLISION"
        return out

    updated, error = locator_patch.apply_to_source(source, field, new_expression)
    if updated is None:
        result.verdict, result.reason = NO_HEAL, f"could not patch: {error}"
        result.classification = "NOT_PATCHABLE"
        out["error"] = error
        return out

    out["updated_source"] = updated
    out["pr_section"] = locator_patch.pr_section(
        result, out["confirm"], out["collisions"], baseline["raw_locator"],
        reached_by=_reached_by(baseline["locator_id"], sources_by_page_object))
    return out


# --------------------------------------------------------------------- CLI

def _main() -> int:
    import argparse
    import yaml
    from playwright.sync_api import sync_playwright

    here = pathlib.Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Heal one locator against a live page.")
    ap.add_argument("--locator-id", required=True, help="e.g. LoginPage#usernameField")
    ap.add_argument("--url", required=True)
    ap.add_argument("--baseline", default=str(here / "baselines"))
    ap.add_argument("--app", default="fixture")
    ap.add_argument("--pageobjects", default=str(here / "fixtures" / "pageobjects"))
    ap.add_argument("--config", default=str(here.parent / "config" / "locator.yaml"))
    ap.add_argument("--explain", action="store_true", help="ranked candidates + breakdown")
    ap.add_argument("--no-apply", action="store_true",
                    help="dry run: show the patch without writing it")
    a = ap.parse_args()

    cfg = yaml.safe_load(pathlib.Path(a.config).read_text())
    base_dir = pathlib.Path(a.baseline)
    baseline = load_baseline(base_dir, a.app, a.locator_id)
    all_b = {f.stem.replace("__", "#"): json.loads(f.read_text())
             for f in (base_dir / a.app).glob("*.json")}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=browser_mode.headless())
        page = browser.new_page(viewport=capture.VIEWPORT)
        po_path = pathlib.Path(a.pageobjects) / f"{a.locator_id.split('#')[0]}.java"
        source = po_path.read_text() if po_path.exists() else ""
        sources = {p.stem: p.read_text() for p in pathlib.Path(a.pageobjects).glob("*.java")}
        out = resolve_and_apply(browser, page, baseline, cfg, a.url, source=source,
                                field=a.locator_id.split("#")[-1],
                                sources_by_page_object=sources)
        res = out["result"]

        print(f"\n{res.locator_id}: {res.verdict}  [{res.classification}]")
        print(f"  {res.reason}")
        if res.emitted:
            print(f"  was:  {baseline['raw_locator']}")
            print(f"  now:  {res.emitted.get('java') or res.emitted['sel']}")
            print(f"  score {res.score:.3f}  margin {res.margin:+.3f}  "
                  f"tier {res.tier}  verification {res.verification}")
            if res.emitted.get("fragile"):
                print(f"  FRAGILE: {res.emitted['fragile']}")
        if a.explain:
            import score as score_mod
            print("\n  score breakdown:")
            print(score_mod.format_breakdown(res.breakdown_rows, 10))
            print("\n  retry log:")
            for at in res.attempts:
                print(f"    {at['loop']:<12} {at['detail'][:100]}")
        if out["updated_source"] and not a.no_apply:
            po_path.write_text(out["updated_source"])
            print(f"\n  patched {po_path}")
        elif out["updated_source"]:
            print("\n  (dry run — nothing written)")
        if out["pr_section"]:
            print("\n" + "-" * 70 + "\n" + out["pr_section"])
        browser.close()
    return 0 if res.verdict == HEALED else 1


if __name__ == "__main__":
    raise SystemExit(_main())
