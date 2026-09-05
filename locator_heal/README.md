# Locator self-healing — engine test bench

**The engine now lives in `shared/locator_*.py`.** This directory is its test
bench: the fixture corpus, the evaluation harness and the tests. Keeping them
here and importing from `shared/` is deliberate — a second copy of the engine
would drift, and the corpus is the only thing that says whether a change to the
scorer made it better or worse.

```bash
V=.venv/bin/python
$V locator_heal/fixtures/generate.py       # build the v2 corpus + java page objects
$V locator_heal/eval.py                    # full accuracy/precision table
$V locator_heal/eval.py --negatives-only   # must report 0 wrong heals
$V locator_heal/eval.py --locators         # old -> new locator per heal
$V locator_heal/eval.py --case tag_swapped --explain     # score breakdown
$V -m pytest locator_heal/test_*.py -q     # phase G + retry-loop tests
$V locator_heal/live_check.py              # golden case vs real saucedemo.com

# resolve one locator end to end (dry run, then for real)
$V -m shared.locator_resolve --locator-id LoginPage#loginButton \
   --url "file://$PWD/locator_heal/fixtures/v2/tag_swapped.html" \
   --baseline locator_heal/baselines --pageobjects locator_heal/fixtures/pageobjects \
   --no-apply --explain
```

## Current results

**Synthetic corpus — 25 cases**

| | |
|---|---|
| **Wrong heals (gating metric)** | **0 / 25** |
| Top-1 accuracy, positives | 17 / 17 (100%) |
| Correct refusals, negatives | 8 / 8 (100%) |
| Verification strength | 17 STRONG (post-condition held), 0 WEAK |
| Mean latency | ~600 ms/heal end to end |
| Deterministic path alone | ~56 ms (26 gather + 30 score/rank) |
| LLM calls | 0 |

7 cases are a **holdout set** written after the weights and thresholds were
frozen and never tuned against. All landed correctly with no further tuning.

**Live golden case — saucedemo.com, real markup, DOM mutated in-browser**

| | |
|---|---|
| Healed to the correct element | **6 / 6** |
| Page sizes | 20 and 112 scorable elements |
| Latency | ~1.0–1.4 s/heal (incl. login replay + 2 confirmation runs) |

Ground truth is stamped on the elements *before* mutation and `capture.py` strips
`data-gt*`, so the healer cannot see the answer.

**Tests** — 10 passing, covering the paths the corpus cannot reach: minimal
patching, byte-identical revert, blast-radius collisions, the heal-history
circuit breaker, R5 with a stub model (including the model refusing), R0 flake
detection, and the per-run caps.

## Pipeline

```
shared/locator_capture.py   one page.evaluate() -> fingerprint every element
                            reads THE capture script from the framework's
                            src/main/resources/locator-capture.js — that repo owns
                            it, because it ships in the jar and runs during tests
shared/locator_classify.py  the gate: APP_BUG / WRONG_STATE / NOT_LOCATOR / AMBIGUOUS /
                            FEATURE_REMOVED / MISBOUND / ASSERTION_LOCATOR /
                            LOCATOR_DRIFT  <- only the last one heals
shared/locator_candidates.py T0 literal repair -> T1 identity -> T2 role+name ->
                            T3 anchored -> T4 full scan -> T5 visual
shared/locator_score.py     Similo/Similo++ weighted similarity, pure Python, no deps
shared/locator_decide.py    accept threshold + ambiguity margin + tier priors
verify.py     resolve -> actionable -> affordance -> execute -> post-condition
shared/locator_emit.py      getByTestId > getByRole > label/placeholder > text >
                            scoped-by-text > CSS > Robula+ XPath; identity-checked
shared/locator_verify.py    resolve -> actionable -> affordance -> execute ->
                            post-condition, in a fresh context from storage_state
shared/locator_patch.py     declaration edit (via edit_guards), R6 confirmation,
                            locator collisions, baseline update, PR section
shared/locator_resolve.py   orchestrator: R0..R6 + circuit breakers + CLI
config/locator.yaml         weights, thresholds, budgets, volatile patterns
```

## What it reuses rather than reinvents

| Concern | Uses |
|---|---|
| Applying the edit | `edit_guards.apply_edits` — refuses ambiguous matches rather than guessing |
| Refusing a looser selector | `edit_guards.no_selector_broadening` |
| Finding declarations | `page_identity.extract_locators` |
| "Are we on the right page?" | `baseline.is_different_page` |
| Baseline records + staleness | `shared/baseline.py` (`_is_older` rejects one written by the failing run) |
| R5 model call | `shared/claude.py`, injected — never required |

`shared/blast_radius.py` answers a different question (which *tests* reach a page
object). `locator_patch.collisions` answers the runtime one: does the new locator
now point at an element another locator already owns — which no rerun reveals,
because both tests still pass.

## Coverage against the plan

| Plan item | State |
|---|---|
| A baseline fingerprint | done, incl. `frame_path`, `app_commit`, optional screenshot crop, fallback chain |
| B classification gate | done: APP_BUG / WRONG_STATE / NOT_LOCATOR / AMBIGUOUS / FEATURE_REMOVED / MISBOUND / ASSERTION_LOCATOR / LOCATOR_DRIFT |
| C candidate ladder | T0-T5 done; T6/R5 wired and stub-tested |
| D scoring + 3-number rule | done |
| E live verification | done: fresh context, `storage_state`, replay, post-conditions |
| F emit | done + scoped-by-text rung, a11y hint, fragility flags |
| G patch -> confirm -> blast radius -> PR | done; revert-on-red tested |
| R0-R6 | done |
| Circuit breakers | done: wall clock, per-run cap, one-shot per locator, app-error backoff, heal-history |

## Design decisions that earned their place

Each of these was added because the corpus caught a real failure, not on theory.

**The ambiguity margin.** Published tools (Healenium's `score-cap`) accept
"closest wins above a threshold". We additionally require top-1 to beat top-2 by
0.10. This is what refuses `decoy_added` and `element_duplicated` instead of
flipping a coin between identical candidates.

**Rect-gap neighbours, not centre distance.** A full-width `<label>` directly
above an input has a huge centre-to-centre distance — the first version ranked a
product card 200px away as "closer" than the field's own label. Edge-to-edge gap
plus explicit `label[for]` / `aria-labelledby` / section-heading relations fixed
it, and turned `neighbor_texts` into the property that separates three identical
Add-to-cart buttons.

**Role-aware tag comparison.** `<button>` → `<a role="button">` is routine
component-library churn. Scoring tag inequality as a flat 0 sank a candidate that
matched on name, id, role, text and neighbours. When the computed role holds, a
changed tag is presentation, not identity.

**Absent ≠ different.** If the baseline had a `data-testid` and the candidate has
*none*, the app dropped an attribute — that is weaker evidence than the candidate
carrying a *different* testid. Absent properties cost a fraction of their weight
instead of all of it.

**`emit` must confirm identity, not just uniqueness.** `count()==1` alone let a
selector matching exactly one *sibling* pass. On three identical product buttons
this silently emitted a locator for the wrong one. Every rung now verifies the
single match is the node we chose.

**R3 does not fall through to a different element.** When the decision names a
clear winner and `emit` cannot express it, that is a failure of `emit` — not
evidence for the runner-up. The earlier version rebound the backpack test onto
the bike light for exactly this reason. Alternates are now only genuine near-ties.

**Anchor text ranked by stability, not proximity.** The nearest text to a product
button is its price. The first version emitted
`.inventory_item:has-text("$29.99") button`, trading one brittleness for another.
Now it prefers `"Sauce Labs Backpack"`, and flags any emitted locator that still
embeds self-changing text (counts, prices, dates).

**MISBOUND is reported, never auto-fixed.** A reordered list rebinds a positional
locator with no error at all — the test keeps passing while exercising the wrong
element. Detected by asking whether some *other* element matches the baseline
better than the resolved one. But rebinding a locator that still resolves changes
what a passing test asserts with no failure to justify it, so it goes to a human
(`heal_misbound: false`).

## Honest limitations

- **Weights are hand-set.** The paper optimised them with a genetic algorithm.
  Ours should be tuned against real heal history, not intuition — and we do not
  have that history yet.
- **No real model has been through R5.** The wiring is proved with a stub. The
  deterministic path clears all 25 corpus cases and all 6 live ones, so R5 has
  never been *needed* — which is the shape the research predicts, but it does
  mean no real prompt/response pair has been exercised. Wire it to this repo's
  existing `LLM_PROVIDER` routing rather than hardcoding a vendor.
- **"Rerun the test suite" is still simulated.** Phase G proves the patch by
  re-verifying the locator in fresh contexts (R6) and checking neighbours for
  collisions. It does not run `mvn test` — that needs the real suite and belongs
  to the integration pass.
- **Largest page measured is 112 elements.** Real enterprise pages are 10-50x
  that. Scoring is O(n) and the deterministic path is ~56 ms at 37 elements, so
  it should hold, but it is unmeasured at scale.
- **Wayback snapshot validation not done.** The papers' method needs manually
  labelled ground truth across archived versions; without labels the numbers
  would be circular, since the healer uses the same signals a naive auto-labeller
  would. The saucedemo study substitutes for it: real markup, real a11y tree,
  ground truth stamped before mutation and invisible to the scorer.
- **Frames and popups are coded, not exercised.** R4 widens through child frames
  and popups, and shadow DOM is pierced and unit-checked, but no fixture drives
  the frame/popup paths end to end.
