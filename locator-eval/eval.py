"""Corpus runner.

Reports precision, not just recall. A healer that fixes 99% of cases and
wrong-heals 2% is worse than one that fixes 85% and never wrong-heals: the
first kind converts loud failures into silent ones, which is the entire risk
this system has to earn its way past. So `wrong heals` is the gating number and
it is printed first.
"""
from __future__ import annotations
import argparse, json, pathlib, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import yaml
from playwright.sync_api import sync_playwright

from shared import browser_mode
from shared import locator_candidates as cand_mod
from shared import locator_capture as capture
from shared import locator_decide as decide_mod
from shared import locator_resolve as heal_mod
from shared import locator_score as score_mod

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE.parent / "config" / "locator.yaml"
FIX = HERE / "fixtures"
BASE = HERE / "baselines"

OK, BAD, MEH = "\033[32m", "\033[31m", "\033[33m"
DIM, RST = "\033[2m", "\033[0m"


def uri(rel: str) -> str:
    return (FIX / rel).resolve().as_uri()


def classify_outcome(case: dict, res) -> tuple[str, str]:
    """-> (bucket, note). Buckets: correct_heal, wrong_heal, missed_heal, correct_refusal."""
    if case["expect"] == "HEAL":
        if res.verdict != heal_mod.HEALED:
            return "missed_heal", res.reason
        if res.picked_gt != case["expect_gt"]:
            return "wrong_heal", f"picked {res.picked_gt!r}, wanted {case['expect_gt']!r}"
        return "correct_heal", res.emitted["strategy"]
    if res.verdict == heal_mod.HEALED:
        return "wrong_heal", f"healed onto {res.picked_gt!r} — should have refused"
    tag = "" if res.classification == case["expect_reason"] else \
          f" (via {res.classification}, expected {case['expect_reason']})"
    return "correct_refusal", res.classification + tag


def run(args) -> int:
    global FIX
    FIX = pathlib.Path(getattr(args, "corpus", FIX)).resolve()
    cfg = yaml.safe_load(CONFIG.read_text())
    man = json.loads((FIX / "manifest.json").read_text())
    cases = [c for c in man["cases"] if not args.case or c["name"] in args.case]
    if args.negatives_only:
        cases = [c for c in cases if c["expect"] == "NO_HEAL"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=browser_mode.headless())
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        n = heal_mod.record_baselines(page, uri(man["baseline"]), man["locators"], BASE, cfg=cfg)
        print(f"{DIM}baselines recorded from {man['baseline']}: {n}/{len(man['locators'])}{RST}\n")

        buckets: dict[str, list] = {}
        rows, total_ms = [], 0
        for case in cases:
            baseline = heal_mod.load_baseline(BASE, "fixture", case["target"])
            # Each fixture case simulates an independent suite run, so the
            # per-run caps get a fresh session rather than tripping on case 6.
            session = heal_mod.HealSession()
            res = heal_mod.heal(page, baseline, cfg, uri(case["file"]),
                                session=session, browser=browser)
            session.record(res)
            bucket, note = classify_outcome(case, res)
            buckets.setdefault(bucket, []).append(case["name"])
            total_ms += res.elapsed_ms
            rows.append((case, res, bucket, note))

            if args.explain:
                explain_case(page, baseline, cfg, case, res)

        browser.close()

    mark = {"correct_heal": (OK, "heal  "), "correct_refusal": (OK, "refuse"),
            "missed_heal": (MEH, "missed"), "wrong_heal": (BAD, "WRONG ")}
    print(f"{'':2}{'case':<20}{'target':<28}{'result':<8}{'score':<7}{'tier':<13}detail")
    print("-" * 118)
    for case, res, bucket, note in rows:
        col, label = mark[bucket]
        sc = f"{res.score:.2f}" if res.score else "  -  "
        tier = res.tier.split("_")[0] if res.tier else res.classification[:11]
        print(f"{col}{'':2}{case['name']:<20}{case['target']:<28}{label:<8}{RST}"
              f"{sc:<7}{tier:<13}{DIM}{note[:52]}{RST}")

    if args.locators:
        print(f"\n{'':2}{'case':<20}{'was':<46}{'becomes (Java Playwright)'}")
        print("-" * 118)
        for case, res, bucket, note in rows:
            if res.verdict != heal_mod.HEALED:
                continue
            b = heal_mod.load_baseline(BASE, "fixture", case["target"])
            java = res.emitted.get("java") or res.emitted["sel"]
            print(f"{'':2}{case['name']:<20}{DIM}{b['raw_locator'][:44]:<46}{RST}{java[:64]}")
        print()

    pos = [r for r in rows if r[0]["expect"] == "HEAL"]
    neg = [r for r in rows if r[0]["expect"] == "NO_HEAL"]
    ch = len(buckets.get("correct_heal", []))
    wh = len(buckets.get("wrong_heal", []))
    mh = len(buckets.get("missed_heal", []))
    cr = len(buckets.get("correct_refusal", []))

    print("\n" + "=" * 60)
    col = OK if wh == 0 else BAD
    print(f"{col}  wrong heals (GATING METRIC)  {wh} / {len(rows)}{RST}"
          + (f"   -> {', '.join(buckets['wrong_heal'])}" if wh else "   none"))
    print(f"  top-1 heal accuracy           {ch}/{len(pos)} positives"
          f"  ({ch / len(pos) * 100:.0f}%)" if pos else "")
    print(f"  correct refusals              {cr}/{len(neg)} negatives"
          f"  ({cr / len(neg) * 100:.0f}%)" if neg else "")
    strong = sum(1 for _, r, b, _ in rows if b == "correct_heal" and r.verification == "STRONG")
    weak = sum(1 for _, r, b, _ in rows if b == "correct_heal" and r.verification == "WEAK")
    print(f"  missed (safe failure)         {mh}")
    print(f"  verification strength         {strong} STRONG (post-condition held)"
          f", {weak} WEAK (action only)")
    print(f"  mean latency                  {total_ms / max(len(rows), 1):.0f} ms/heal")
    print("=" * 60)
    return 1 if wh else 0


def explain_case(page, baseline, cfg, case, res):
    """Per-property breakdown for the top candidates — a heal we cannot explain
    is a heal we cannot defend in a PR."""
    vol = score_mod.Volatility(cfg)
    page.goto(uri(case["file"]))
    snap = capture.snapshot(page)
    ranked = decide_mod.rank(cand_mod.gather(page, snap, baseline, cfg, vol), baseline, cfg, vol)
    print(f"\n{'=' * 78}\n{case['name']}  |  {case['target']}  |  {baseline['raw_locator']}")
    print(f"  classification: {res.classification}   verdict: {res.verdict} ({res.reason})")
    for i, c in enumerate(ranked[:4]):
        star = "->" if i == 0 else "  "
        print(f"{star} #{i} {c.score:.3f}  <{c.el['tag']}> {(c.el.get('accessible_name') or '')[:34]!r}"
              f"  gt={c.el.get('_gt')}  tier={c.best_tier}")
        if i < 2:
            print(score_mod.format_breakdown(c.breakdown, 8))
    for a in res.attempts:
        print(f"  {DIM}{a['loop']:<12} {a['detail'][:96]}{RST}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(FIX),
                    help="corpus directory (default: bundled fixtures)")
    ap.add_argument("--case", action="append", help="run only this case (repeatable)")
    ap.add_argument("--negatives-only", action="store_true")
    ap.add_argument("--explain", action="store_true", help="print score breakdowns")
    ap.add_argument("--locators", action="store_true",
                    help="print the old -> new locator each heal would write")
    sys.exit(run(ap.parse_args()))
