# test-authoring-agent — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Takes plain English test steps from a `.txt` file in the queue, generates complete
framework-compliant Java test code for the automation repository (`GITHUB_REPO_AUTOMATION`), validates
generated web flows by driving a real browser via Playwright MCP, runs the generated test via Maven,
fixes any failures iteratively, and raises a GitHub PR.

Runs independently. One session = one module input file = one PR (or Slack alert if tests fail).

---

## Architecture

```
run.sh (orchestrator)
  │
  ├─ 01_parse.py          [Python + Claude]   Plain text → structured generation plan
  ├─ 02_validate_api.py   [Python only]       Real HTTP calls: confirm auth + safe endpoints
  ├─ 02_validate_web.py   [Python + Claude]   Drive browser via Playwright MCP → selector map
  ├─ 03_generate.py       [Python + Claude]   Write Java files to the automation repo
  ├─ 04_run_and_fix.py    [Python + Claude]   Run mvn test → fix failures → retry loop
  └─ 05_ship.py           [Python only]       Git branch + commit + push + gh pr create
```

---

## Step Responsibilities

| Step | Owns | Does NOT do |
|------|------|-------------|
| **01 Parse** | Read plain text, call Claude, produce plan JSON | No file writes to the automation repo |
| **02 Validate API** | Real HTTP auth + a real call to every endpoint against `api_base_url` — the input's `curl` where it gave one (run without a shell), no LLM | Never call a path-param endpoint whose value is unknown and has no curl; never treat a body-less POST/PUT/DELETE status as the endpoint's real one |
| **02 Validate Web** | Drive the browser via Playwright MCP, collect confirmed selectors | No Java codegen |
| **03 Generate** | Write all Java files to the automation repo | No test running |
| **04 Run & Fix** | Run mvn test, call Claude to fix failures, retry | No git push |
| **05 Ship** | Branch + commit + push + PR creation | No AI calls |

### What "confirmed" means in step 02

Every locator in `02-validate-web.json` — both the `selectors` map and the
`interaction_hints` list, since step 03 generates from both — has been measured in
the live browser at **exactly one matching element**. Anything else is dropped
before the file is written:

| Case | Outcome |
|------|---------|
| `SELECTOR_FOUND` with `count=1` and `visible=1` | kept |
| `SELECTOR_FOUND` with `count != 1` | dropped — would be a runtime strict mode violation |
| `SELECTOR_FOUND` with `visible != 1` | dropped — a locator nobody can see makes a test fail for an invisible reason |
| `SELECTOR_FOUND` with no `count` at all | dropped — never measured, so not confirmed |
| `SELECTOR_FOUND` with no `visible` at all | kept, recorded as visibility-unmeasured (a pre-protocol cached run must not empty the map) |
| `INTERACTION_HINT` whose name has a confirmed selector | kept, with the hint's selector **replaced by the confirmed one** |
| `INTERACTION_HINT` with no confirmed selector and no `count: 1` | dropped |
| a plan locator with no `SELECTOR_FOUND`, whose element the browser recorded being clicked (counted 1/1, its text sharing the most words with the name, no tie) | **recovered** from that click (`recover_clicked_locators`). A run clicked a page's main button with a selector copied from its prompt's example and never reported it; step 03 guessed a `button` for what was a link, and the test failed on its first locator |
| `SELECTOR_FOUND` for a field with an `INPUT_USED`, measured by the helpers as taking no typing (`editable: false`, or a harvested `tag` other than input/textarea/select) | dropped, with the `INPUT_USED` and any `VALUE_CHECK` whose source is that input (`enforce_typed_fields`) |

Unique and visible says an element exists, not that it is the one a name means.
A run matched the plan's `amountField` to an earlier run's `amountText`, a cart's
read-only total cell, and reported `INPUT_USED: amountField|20,000` for a value it
had only read. Step 03 generated `fillText()` on a `<td>`.

The claim itself is checked too. `qa-helpers.js` reports every value typed into
any field as it happens, cross-origin iframes included, and `qa-page.js` writes
each one to the evidence file as a `typed` row. A password's value is never
recorded, only its selector. When a run recorded any typing, an `INPUT_USED` whose
value matches none of the recorded values is dropped. The field's selector stays,
because only the claim is wrong. A field that formats its input
(`4111111111111111` shown as `4111 1111 1111 1111`) still counts.

The hint rules exist because a hint records an element the model *interacted with*,
including ones an interaction then failed on — an observed run hinted a profile edit
icon as `img[alt='PencilSimple']`, found clicking it did nothing, and confirmed the
parent `span` instead, leaving a hint pointing at the element that does not work.

A run that confirms nothing is retried once; if it still confirms nothing, step 03
aborts rather than generating from guesses (override with `ALLOW_MISSING_SELECTORS`).

Every dropped selector is recorded in `rejected_selectors` with its reason, so
"why is there no locator for the toast?" has an answer in the audit trail rather
than in a console line that has scrolled away.

### What a step outcome means

A step has three outcomes, not two. The third exists because "I did the action but
could not observe what it claims" used to collapse into a pass — which is how a run
reported `STEP_PASSED: Verify a success confirmation toast appears` for a toast that
never rendered, reasoning from the save API returning 200.

| Marker | Meaning |
|--------|---------|
| `STEP_PASSED` | the step's claim was observed. For a claim about a UI element that means **seeing the element**; a network response is never proof one rendered |
| `STEP_UNVERIFIED` | the action completed, the claimed outcome was never observed |
| `STEP_FAILED` | the step could not be performed |

This is enforced, not requested: a verification step reported as passed with no
`SELECTOR_FOUND` for the element it claims to have seen is downgraded to unverified
in Python (`enforce_verification_evidence`). Step outcomes were the last self-report
in this step that nothing checked.

The other direction is enforced too: only a claim can be unverified. A
`STEP_UNVERIFIED` on an action is ignored (`drop_unverified_actions`). A run clicked
a payment-method tab and counted it after the click, when it was gone. It then wrote
`STEP_UNVERIFIED: Select Credit Card as the payment method (locator report)`. Step 03
kept that as a requested check, and step 04 stopped on the `defect` gate when the
tab's guessed locator failed. The PR reported a product defect that did not exist. `page.qa.step`'s `before` option counts the control a
step clicks before the click.

A downgraded step carries `check_provenance.UNMEASURED`, and steps 03 and 05 word it
differently from a check that was looked for and not found. The model may well have
seen it, so it is reported as "the locator is a guess" and never as "the product did
not do this". Both kinds still keep the assertion at full strength and still make the
verdict NEEDS-REVIEW.

### Why step 02 is fast now — preloaded helpers, Chromium, known selectors

The same 11-step checkout took 15 minutes to validate on a good night and timed out
at 30 on a slow one. The cost was never the flow. It came from four places:

| Cost | Where it went | Fix |
|------|---------------|-----|
| Browser | The MCP server launched the machine's branded Chrome, whose renderer sat at 100% CPU for ~80s after every page load on this site. A trivial evaluate took 2-10s, and a step took 15-55s. | `--browser chromium` (`PLAYWRIGHT_MCP_BROWSER`), which is also the engine the generated tests run on. Evaluates dropped to 2-15ms. |
| Model output | 48 of 59 tool calls were JavaScript the model typed from scratch: the same harvest, count, frame loop and recorder, over and over. That was a third of the 40k output tokens. | `shared/browser/`. `qa-helpers.js` (`window.__qa`, every frame) and `qa-page.js` (`page.qa`, loaded with `--init-page`) do the measuring. Each call is one line. |
| Round trips | About 2.4 calls per step, plus fixed sleeps. | `page.qa.step(action, opts)`: act, wait until every frame and the network have settled, then return the harvest (every element's best stable selector, already counted) or a batch `check`, all in one call. |
| Rediscovery | Every run started from nothing, and found some worse selectors the second time. | `known_selectors()` seeds the prompt with the newest confirmed map for the same host. Each selector is counted live again before it is reported. |

The quality bar is unchanged. Every selector is still measured live at count 1 and
visible 1 (the helpers do the same counting rule 2c always required), every iframe
hop is still proven unique, and brief screens are still recorded in the call that
shows them (`page.qa.record`). Driven by script with these helpers, the whole
checkout (buy, card, promo, 3-D Secure OTP, the result screen and the redirect)
takes about 35s of browser time.

The counts are also checked, not just requested. The helpers write what they
measure to `02-web-evidence.jsonl` as they measure it. `verify_with_evidence`
checks every `SELECTOR_FOUND` against that file before the map is written:

| Evidence for the selector | Outcome |
|---|---|
| a 1/1 reading | kept, counted as measured live |
| readings, none of them 1/1 | dropped into `rejected_selectors` with the measured count |
| none (typed outside the helpers) | kept on the model's numbers, as before |

The log line `Selectors measured live by the browser helpers: N of M kept` shows how
much still rests on the model's word: it adds how many were dropped and how many
rest on the marker's own count. The seeded `02-known-selectors.json` is
counted live on every page state too. The adaptation explorer uses the same
evidence (see its CLAUDE.md).

### Frames and brief screens

`browser_evaluate` reaches the top document only, and every tool call costs seconds.
Rule 2f (`CAPTURE_RULES` in `shared/mcp_config.py`, which the adaptation explorer also
gets) covers what that misses. It measures inside a cross-origin iframe through
`browser_run_code_unsafe` → `frame.evaluate`. It also catches a screen that closes by
itself: one call performs the action and records every new state of every frame for
20 seconds, measuring selectors while they are on screen. An embedded checkout run
needed both. The bank page's amount could not be counted, and the payment result
closed about 10s after the click, while the model's next look came 12s after it.

An element inside an iframe is reported as a **chain**:
`#checkout >> internal:control=enter-frame >> #amount`. This is the string Playwright
itself compiles `frameLocator(...)` into and prints in failure messages, so the same
text flows from `SELECTOR_FOUND` through codegen (the plugin renders it as
`page.frameLocator(...).locator(...)`), the step 04 guards, the failure snapshot and
the healing agent. `shared/frames.py` holds the format and the rule that picks each
iframe's selector: stable attributes only, the src path and never its host, and
no selector at all when the only choice would be positional. Every hop is checked
unique on its own, because a count through two matching iframes reads 1 while the
click fails. That rule is preloaded into every page the MCP browser opens
(`--init-script`, `window.__qaLink`) and the prompts only call it: asked to paste
it, the model retyped it without the title rule and reported a 3-D Secure page's
fields without their iframe.

A locator name the plan uses on more than one page (`amountDisplay` on a popup, a
bank page and a success screen) is asked for as `IssuingBankPage.amountDisplay`,
and a name reported twice keeps its first selector, never the last.

### Assertions vs mechanisms

The two halves of a test are treated very differently, following the rule
`shared/intent.py` already states — *the mechanism becomes mutable and the proof
does not*.

**A verification names the proof, and it is fixed.** What happens to one step 02
could not observe depends on who asked for it (`shared/check_provenance.py` decides,
by measuring the check's vocabulary against the author's own words — never by
trusting the model's claim about itself):

| Check | Outcome |
|-------|---------|
| the input asked for it | **kept at full strength.** The test fails on purpose, the PR says why, and the verdict is NEEDS-REVIEW. The product does not do what was asked — that is a finding. Step 04 is told these checks and stops with the `defect` gate when one is the failure, instead of pointing its locator at some other element |
| the pipeline invented it | **dropped entirely** — locator, accessor and assertion. A failing check nobody asked for is exactly what gets "fixed" by deleting it |

Dropping is the irreversible direction, so it needs the harder test: a check is only
dropped when *nothing* in it traces back to the input. A partly-traceable check is
kept and the test goes red, because a wrongly-kept check is visible and a wrongly-
dropped one is silent.

**An action names an outcome, and the mechanism is ours to find.** "Save the profile"
does not mean "there is a Save button" — Naukri's profile summary autosaves about a
second after the last keystroke. When an action's named control is not visible, step
02 discovers how the outcome actually happens (rule 2e) and reports
`MECHANISM_FOUND: <action>|<kind>|<trigger>|<settles when>`, which step 03 generates
from. An action step never becomes an unverified check.

### What step 04 may not do

A fix may change how the test reaches its result; it may not change the result it
proves. Every assertion reachable from the test method is fingerprinted **before the
first run** into `.assertions-frozen.json`, and each attempt is compared against that
frozen copy with `shared/assertion_graph.conserved()` — so attempt 3 cannot launder a
weakening introduced by attempt 2. An assertion removed, moved down a strength ladder
(`assertEquals` → `assertNotNull`), wrapped in a condition, or given a different
expected value rejects the **whole** fix and rolls every file back. `FORCE=true`
overrides it, matching test-healing-agent.

An expected value may differ only in whitespace or letter case: `"$175.00"` →
`"$ 175.00"` is the page's formatting, `"$175.00"` → `"$ 0.00"` is a new expectation.
Before this, any changed literal read as a *removed* assertion, so the correct
formatting fix was rejected along with the bug-hiding one. Identical assertions in one
method are fingerprinted separately (`<hash>_1`, `<hash>_2`), so deleting one of a
pair is caught too.

This exists because none of the six per-file guards could see it: deleting an
assertion is a one-line diff that loses no method, adds no `Thread.sleep`, and is
invisible to `no_selector_broadening`, which only inspects `page.locator(...)` calls.

A fix may not point a field at a different field either (`replacement_is_the_field`).
A fix re-pointed `amountField` at `tr:nth-child(2) input`, guessed from the name and
phone rows around it. That was the Email input, unique
and editable, so every other guard passed it. The test typed the amount into
Email, and the next attempt blamed keystroke events. When a fix changes the
locator of something the code types into (the plugin's `TYPING_CALLS`), the
element is looked up in the DOM saved at failure. It must be an input, textarea
or select, and its label, row or table column header must share a word with the
locator's name. The column header is not optional: the real amount input's row
named only the product, and "Amount" was its column's header. The guard only
judges a capture taken for this
file's own failing locator; a capture of any other page decides nothing.

### Values and check contracts — "matches" is decided by the page

English says "validate the name matches the one we filled". That names two values and
leaves the comparison open, and a page rarely renders a value as typed. A run failed
on `Expected: 'User_orrju' | Actual: 'User_orrju sample_last_name'`. Step 02 had typed
`Test User` and seen `Test User`. Step 03 generated a one-word name, and the demo
appended a default last name. The same test also expected an invented `Rp 490.909`, and
compared the bank page's `19000.00` with `Rp19.000` as strings. Step 02 had judged those
two equal as numbers, in prose, and nothing kept that judgment.

`shared/value_match.py` measures the relation between two texts in Python. The relations
are `equal`, `formatting` (whitespace/case), `numeric` (`Rp20.000` = `20,000`), `phone`
(`081…` = `+6281…`) and `words` (the expected text appears as whole words). Anything else
is `None`: a different value, which is a finding.

| Where | What happens |
|-------|--------------|
| **01 Parse** | Rule 4: every value the input gives for a step stays in that step of `web_steps_for_validation`, as written. Reworded to "fill the fields with dummy data", a run's address and card number never reached the browser. Rule 4c: a comparison with something earlier in the flow ("same as we passed earlier") keeps that back-reference. The plan names the earlier value and where it was typed or read. A value that was only shown gets a read-and-record step at that earlier point. That run had reworded one to "matches the expected purchase amount", and step 02 then compared the page's `Rp20.000` with itself. |
| **02 Validate Web** | The prompt's TEST DATA block lists every `Label: value` line of the raw test case (`shared/test_case.py`), so a value reaches the browser even when step 01 reworded it away. An `Email:` or `OTP:` goes there too unless the flow logs in; only then is it a CREDENTIAL. Rule 2g: `INPUT_USED: <field>\|<typed>` for every field filled, and `VALUE_CHECK: <step>\|<element>\|<shown>\|<input:field \| element:name \| literal>\|<other side>` for every comparison. Written to `02-validate-web.json` as `inputs_used` / `value_checks`, with the relation computed in Python. The values win in both directions. A comparison whose two sides match under no relation is downgraded to unverified. One the model reported unverified is promoted to passed when its two sides do match, the element's selector was confirmed, and the expected side traces to its source: the typed value, a measured element, or a literal in the step. A run had called `08123456789` against `+628123456789` a mismatch. A `literal` whose text is not in the test case was read off the page, and is dropped (`drop_untraced_sources`). "Record the amount shown" had come back as `literal\|20,000` and been generated as `assertEquals(amount, "20,000")`. |
| **03 Generate**, in the prompt | VALIDATED INPUTS: a typed value the test case itself states is marked GIVEN BY THE TEST CASE and becomes that field's default exactly as written. Every other field's test data keeps the typed value's shape (word count, character classes, prefix), randomised inside it. CHECK CONTRACTS: each check is asserted with exactly its measured relation, with the expected side taken from its source, never a new literal. A contract overrides the plan's `assertEquals` wording. A `numeric` contract compares both amounts as plain number text through the string equality assertion. When it said "the two parsed numbers", the model wrote `assertEquals(config, long, long, …)`, which has no overload, and the compile gate stopped the run. |
| **03 Generate**, after codegen | An expected value (an assertion's whole string argument, or a CSV cell under an `expected*` header) that appears neither in the test case nor in anything step 02 typed, read or reported is untraced. One repair pass under `VALUE_REPAIR_MAX_DIFF_LINES` runs, and it is kept only if it removes some. Whatever remains goes in `03-generate.json` → `untraced_expected_values`. |
| **04 Run & Fix** | A failure that is an `Expected/Actual` pair with a benign relation, pinned by its message to exactly one frozen assertion, is `VALUE_MISMATCH`. When its expected side is a string literal and the page only spaces or cases it differently, the fix is that literal, rewritten to exactly what the page shows (`literal_fix`). No sanction is given, so a comparator change is rejected: offered one, a fix wrapped both sides of a message check in `.replaceAll("\\s+", "").toLowerCase()`. Otherwise the prompt offers two fixes in order: (a) restore the data's validated shape, or (b) change **only** that assertion's comparator. `conserved(sanction=…)` accepts exactly (b): same place, same message, every compared expression and expected value still passed, no deeper condition. It is recorded as `relaxed_checks`. A frozen file without argument text (an older session) gets no sanction. |

The message is the pin because the fingerprint leaves it out. Name and phone checks with
one call shape share a fingerprint, so without it the name check's relaxation was paired
with the untouched phone check. A relaxed check still counts toward APPROVED. The
relation names what the product was measured to do, so it is not a weakening a human
must sign off.

### When step 04 stops retrying

`AUTHORING_FIX_RETRY_COUNT` is a **ceiling, not a target**. A budget bounds the worst
case; it cannot tell a real attempt from a repeat of one. So the loop also stops the
moment it can prove the next attempt would not differ, which is the whole reason the
budget could come down from 4 to 2.

Three proofs, all in `shared/fix_history.py`:

| Stop | Why another attempt cannot help |
|------|--------------------------------|
| the model returned `edits: []` | It reports it cannot fix this from the files it can see. That is an answer. Nothing on disk has changed since the failing run, so re-running maven reproduces a known result |
| the same guard rejected everything **twice running** | The first rejection earns a retry, because the model had not yet been told why. The second was made *with* that reason in the prompt |
| the proposed edits repeat an earlier attempt's | Matched on exact content hashes. Identical edits, identical result |

All three write the `stuck` gate, not `false` — the test genuinely ran and genuinely
failed, which is not an infra `skipped` where it never got a fair shot. The stored
`reason` is what the PR body and the Slack alert quote.

**Every attempt is recorded in `.fix-history.json`, appended and never overwritten**,
and rendered into the next prompt. This is what makes attempt N differ from attempt N-1:

- `04-run-and-fix.json` is overwritten each attempt, so on its own it gave attempt 3 no
  way to know what attempt 1 tried — and attempt 3 was free to re-propose it.
- Guard rejections used to reach disk and the ship verdict but never a prompt. The model
  was told "try something different" without being told what it had done wrong, and the
  run burned its budget re-triggering the same guard.

An attempt whose fixes were all rejected also **carries the previous attempt's failure
context forward** rather than writing a result without it. Dropping it blanked the next
prompt's `<structured_failure_report>`, made the `stuck` check unreachable, and — via
`run_started_at=0.0` — silently disabled `gather_runtime_evidence`'s freshness gate, so
the next attempt was shown a DOM captured in a different session.

---

## Data Flow

```
queue/<module>.txt  (plain English test steps; server runs use queue/<user-id>/<module>.txt)
    ↓
01-parse.json            (structured generation plan: classes, fields, methods)
    ↓
02-validate-api.json     (confirmed auth + endpoint shapes, or skipped if not an API test)
02-validate-web.json     (confirmed DOM selectors, or empty if not a web test)
    ↓
03-generate.json         (list of Java files written to the automation repo)
    ↓
04-run-and-fix.json      (test run results, applied fixes)
.fix-passed              (gate: true / false / skipped / stuck / defect)
    ↓
05-ship.json             (PR URL, Slack status)
.verdict                 (APPROVED / NEEDS-REVIEW)
    ↓
queue/processed/<module>.txt  (moved after completion)
```

---

## Input File Format

Plain text file at `queue/<module>.txt` (CLI runs; a run started from the server reads
`queue/<user-id>/<module>.txt`). Claude in step 01 is flexible about exact format.
The minimum required information:

```
Module: payments
Type: both          # api | web | both
URL: https://app.staging.example.com
API URL: https://api.staging.example.com

Steps:
1. Login as Admin user
2. Create a payment of 100 SGD to recipient ABC
3. Verify the payment ID is returned in the response
4. Fetch the payment by ID and verify the status is PENDING

Web Steps:
1. Login as Admin user and navigate to Payments page
2. Click New Payment button
3. Fill in recipient field with Test Recipient
4. Fill amount as 100 and select currency SGD
5. Click Submit
6. Verify success message appears
```

---

## Gate Values

**.fix-passed**
- `true`    — generated test ran and passed → proceed to ship
- `false`   — test failed after all fix attempts → ship with NEEDS-REVIEW verdict
- `skipped` — no test could be run (infra issue) → clean exit
- `stuck`   — the test ran and failed, and a further attempt provably could not differ (see "When step 04 stops retrying") → ship with NEEDS-REVIEW
- `defect`  — the test ran and failed exactly as the input's documented `Actual Result` says the product misbehaves today, or on a check step 02 never saw the product do; the loop stops instead of working around a real bug → ship with NEEDS-REVIEW. Never on a compile failure

**.verdict**
- `APPROVED`      — test passed, nothing the input asked for went unverified, no fix was rejected for weakening an assertion
- `NEEDS-REVIEW`  — test failing, OR a requested check could not be observed, OR a fix was rejected for weakening a test, OR no test ran at all

---

## Audit Trail

**Session folder:** `agents/test-authoring-agent/audit/$SESSION_ID/`

| File | Written by | Purpose |
|------|-----------|---------|
| `00-session-init.md` | run.sh | Session metadata, env snapshot |
| `01-parse.json` + `.md` | Parse | Generation plan |
| `02-validate-api.json` + `.md` | Validate API | Auth status, confirmed endpoint response shapes |
| `02-validate-web.json` + `.md` | Validate Web | Selector map, step results (passed/failed/**unverified**), `rejected_selectors`, `mechanisms`, `inputs_used`, `value_checks` |
| `claude-*.log` | Validate Web | Raw `claude -p` stream, for diagnosing empty runs |
| `02-known-selectors.json` | Validate Web | The confirmed selectors seeded from an earlier run on the same host |
| `02-web-evidence.jsonl` | Validate Web | What the browser helpers measured, one line per settled page state. Every `SELECTOR_FOUND` is checked against it |
| `03-system-prompt.txt`, `04-system-prompt.txt` | Generate, Run & Fix | The static half of each prompt — conventions, references, rules — sent once as `--system-prompt-file` instead of inside every batch or attempt |
| `03-generate.json` + `.md` | Generate | List of files written, `dropped_unverified_checks`, `kept_unverified_checks`, `unconfirmed_locators`, `untraced_expected_values` |
| `04-run-and-fix.json` + `.md` | Run & Fix | Test output, applied fixes, `value_mismatch` / `relaxed_checks` when a failure was triaged as one |
| `.assertions-frozen.json` | Run & Fix | What the generated test proved before any fix — the conservation baseline |
| `.fix-history.json` | Run & Fix | Every fix attempt, appended: diagnosis, edits proposed, guards that rejected them. Feeds the next prompt and the stop rule |
| `.fix-passed` | Run & Fix | Gate: true / false / skipped / stuck / defect |
| `05-ship.json` + `.md` | Ship | PR URL, Slack status |
| `.verdict` | Ship | APPROVED / NEEDS-REVIEW |

---

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAUDE_CLI_PATH` | Path to claude CLI binary | `claude` |
| `AUTHORING_MODEL` | Claude model for all AI steps. No default in code: run.sh stops without it | required |
| `AUTHORING_EFFORT` | Thinking effort for steps 01, 02 and 04 (`low`, `medium`, `high`, `xhigh`, `max`). Step 03 uses `GENERATE_EFFORT`, and step 02 uses `BROWSER_EFFORT` when it is set. Empty = the runner's `effortLevel` | set in `config/.env` |
| `BROWSER_EFFORT` | Thinking effort for step 02, shared with every agent's browser-driving calls. Step 02 runs ~40 turns and pays it on each. Empty = `AUTHORING_EFFORT` | set in `config/.env` |
| `WORKSPACE_DIR` | Parent directory containing the automation repo | required unless `FRAMEWORK_DIR` is set |
| `FRAMEWORK_DIR` | Absolute path to the checkout, overriding `WORKSPACE_DIR/GITHUB_REPO_AUTOMATION` | optional |
| `GITHUB_TOKEN` | GitHub auth token for PR creation | required |
| `GITHUB_ORG` | GitHub org/user owning the repo | required |
| `GITHUB_REPO_AUTOMATION` | Name of the automation repo (directory and GitHub repo) | required |
| `GITHUB_DEFAULT_BRANCH` | Base branch for PRs | `main` |
| `GITHUB_PR_REVIEWERS` | Comma-separated reviewer handles | optional |
| `AUTHORING_BRANCH_PREFIX` | Branch name prefix | `authoring` |
| `AUTHORING_FIX_RETRY_COUNT` | Max retry cycles for failing tests. A ceiling — the loop stops early once an attempt can bring nothing new | `2` |
| `AUTO_PUSH` | Set `false` to skip PR creation (dry-run) | `true` |
| `AUTHORING_ENVIRONMENT` | Maven `-Denvironment=` value | `staging` |
| `AUTHORING_COUNTRY` | Maven `-Dcountry=` value | `SG` |
| `MAVEN_TEST_TIMEOUT_S` | Timeout (s) for a single `mvn test` run in step 04 | `300` |
| `TEST_RESULTS_DIR_NAME` | Java framework's report/screenshot output dir name | `test-output` |
| `AUTHORING_BROWSER_TIMEOUT_MS` | Timeout (ms) for each individual browser action | `30000` |
| `VALIDATE_WEB_TIMEOUT_S` | Wall-clock budget (s) for the whole step-02 run | `1800` |
| `VALIDATE_WEB_RETRY_ATTEMPTS` | Extra full re-runs step 02 attempts on recoverable failures | `1` |
| `HEADLESS_BROWSER` | Set `false` to watch every browser this agent starts — step 02's validation and step 04's `mvn test` run | `true` |
| `PLAYWRIGHT_MCP_VERSION` | `@playwright/mcp` version the browser steps launch (pinned, not `latest`) | `0.0.79` |
| `PLAYWRIGHT_MCP_BROWSER` | Browser the MCP server launches for step 02. `chrome` restores the machine's branded Chrome | `chromium` |
| `VALIDATE_API_REQUEST_TIMEOUT_S` | Timeout (s) for each real HTTP call in Validate API | `15` |
| `VALIDATE_API_RETRY_ON_ERROR` | Set `false` to disable the one connection-error retry in Validate API | `true` |
| `ALLOW_MISSING_SELECTORS` | Let step 03 generate when step 02 confirmed nothing | `false` |
| `GENERATE_COMPILE_CHECK` | Set `false` to skip step 03's `mvn test-compile` gate (a non-Maven framework plugin) | `true` |
| `GENERATE_COMPILE_TIMEOUT_S` | Timeout (s) for that compile | `180` |
| `GENERATE_EFFORT` | Thinking effort for step 03 codegen calls (`low`, `medium`, `high`, `xhigh`, `max`). Pinned so the run does not inherit the effortLevel in the runner's `~/.claude/settings.json` | set in `config/.env` |
| `COMPILE_REPAIR_MAX_DIFF_LINES` | Diff budget for the compile repair pass | `60` |
| `VALUE_REPAIR_MAX_DIFF_LINES` | Diff budget for the untraced-expected-value repair pass | `60` |
| `FORCE` | Let a step 04 fix through even when it weakens an assertion. For a human who has read the diff — never for the loop | `false` |
| `SLACK_BOT_TOKEN` | Slack bot token | optional |
| `SLACK_NOTIFY_CHANNEL` | Slack channel for success notifications | optional |
| `SLACK_ALERT_CHANNEL` | Slack channel for failure alerts | optional |
| `SESSION_ID`, `AUDIT_DIR`, `INPUT_FILE`, `MODULE` | Set by run.sh — do not set manually | — |
| `START_FROM_STEP` | Resume an existing session from step 1-5 instead of a fresh run (see "Resuming a Session" below) | `1` |

---

## How to Run

```bash
# First-time setup: copy the example env file and fill in your values
cp agents/test-authoring-agent/.env.example agents/test-authoring-agent/.env
# Edit .env: set WORKSPACE_DIR, GITHUB_TOKEN, GITHUB_ORG at minimum

# Direct mode — process a specific module input file
./scripts/run-authoring-agent.sh payments

# Queue mode — picks the oldest .txt in the queue
./scripts/run-authoring-agent.sh

# Dry-run — generates, tests, but no PR pushed
AUTO_PUSH=false ./scripts/run-authoring-agent.sh payments

# View audit trail
make audit AGENT=test-authoring-agent
make audit AGENT=test-authoring-agent SESSION=20260330-143022-create-payments
```

### Resuming a Session

If a session got through some steps successfully but failed at (or you just want to
re-run) a later step, resume it in place instead of starting over from Parse. This reuses
the existing session's audit dir — steps before `START_FROM_STEP` are reused as-is, and
any stale output for `START_FROM_STEP` onward (including per-attempt fix files from a
prior failed try) is cleared before it re-runs.

```bash
# Re-run step 4 (Run & Fix) and 05 (Ship) for a session that failed there, reusing
# its 01-parse.json / 02-validate-api.json / 02-validate-web.json / 03-generate.json as-is.
START_FROM_STEP=4 SESSION_ID=20260330-143022-create-payments \
  ./scripts/run-authoring-agent.sh
```

A resume never takes a `TESTING_MODE` cache hit. Retrying a step means running it
again, and the cached artefact is the output of the run being retried — restoring
it made the retry a no-op that reported the step done in 0s and gated every step
after it on a result the retry existed to replace.

`MODULE` is recovered automatically from the session's own `00-session-init.md` if not
given — the original queue `.txt` file may already have moved to `processed/` by the run
being resumed, so it isn't required to still exist. Resuming fails fast with a clear error
if the step immediately before `START_FROM_STEP` never actually completed in that session
(e.g. `START_FROM_STEP=4` requires `03-generate.json` to exist).

The same capability is exposed to `qa_agents_server` as
`POST /agents/test-authoring-agent/sessions/<session_id>/retry` with body
`{"from_step": 4}` — the AI-Test-Studio authoring page's "Retry from step N" action calls it.

---

## Automation Repo Conventions

> **All framework conventions are defined in the automation repo's own `CLAUDE.md`**
> (the single source of truth — in Playwright-Automation-Framework it is titled
> "Jarvis — AI Agent Guide"). Steps 01, 03 and 04 read `$FRAMEWORK_DIR/CLAUDE.md`
> directly and inject it into their Claude prompts.
> Do NOT duplicate framework rules here — update that file instead.

The section below covers **agent-specific generation rules** that are not in the automation repo's `CLAUDE.md`.

### Package Structure (new module)
```
src/main/java/automation/modules/{feature}/
  {Feature}Data.java
  {Feature}Builder.java
  {Feature}Helper.java          extends ApiHelper
  api/{Feature}Api.java         enum implements ApiDetails
  web/{Page}Page.java           extends BasePage

src/test/java/automation/{feature}/
  {Feature}ApiTest.java         extends TestBase
  {Feature}WebTest.java         extends TestBase
```

All patterns (Data POJO, Builder, API Enum, Helper, Page Object, Test classes, DO/DON'T rules)
are defined in the automation repo's `CLAUDE.md` and injected into the Claude prompts at
runtime — that file is the authoritative reference.

---

### URLs Are Properties, Never Java Literals

A URL welded into a test, page object or helper pins the module to one environment —
the automation repo's `CLAUDE.md` has always said so ("Hardcoded URL in test/page → put in properties
file"), but until this guardrail nothing enforced it, and generated modules shipped with
`private static final String LOGIN_URL = "https://..."` and no matching property.

The rule is enforced at four points, all reading `shared/url_properties.py`:

| Where | What happens |
|-------|--------------|
| **03 Generate**, before codegen | `collect_urls()` harvests every URL from the plan (`web_base_url`, `api_base_url`, validation steps) and from `02-validate-web.json` — `urls_visited` first, then `steps_passed`. It names a key for each and writes them to `parameters/{environment}-{country}.properties`. The key table goes into the codegen prompt. |
| **03 Generate**, after codegen | A key the generated code reads that the properties file does not define is **recovered from `urls_visited` or the run aborts** — see "A URL property is not a warning" below. |
| **03 Generate**, after codegen | A literal URL the generated code *added* gets one targeted repair pass, guarded by `validate_fix`. One already in an existing file is left alone, as step 04's guard does. What survives is logged and recorded in `03-generate.json` → `hardcoded_urls`. |
| **04 Run & Fix** | `ensure_url_properties()` rewrites the keys before the first run (run.sh's forced base checkout, `shared.workspace prepare-base --checkout`, discards them). `no_hardcoded_url` is a fix guard: a fix that adds a literal URL is rejected before it reaches disk. |
| **05 Ship** | The URL keys are committed — added to HEAD's copy of the properties file, never the working copy, so the run's real credentials in that same file are not committed with them. |

### `urls_visited` — why the step text was not enough

`collect_urls()` used to read URLs only out of step 02's step *summaries*, which are prose
the model chooses to write. A run that navigated to `https://www.naukri.com/mnjuser/profile`
reported it as `STEP_PASSED: Navigate to the profile page` — no URL in the string — so no
key was minted, the generated test read `naukari.profile.url` from a file that never
defined it, and Playwright died on `url: expected string, got undefined`.

So step 02 now records the URL of every `browser_navigate` call the model made, straight
off the tool stream (`shared/claude.py` collects `navigated_urls`; `02-validate-web.json`
carries them as `urls_visited`). It is the only URL source that cannot be silent: a step
summary is what the model chose to say, this is the argument it actually passed.

### A URL property is not a warning

Reading a key nobody wrote is unrunnable code, and step 03 already knew it — it computed
the list, logged `WARNING`, and wrote the files anyway. Step 04 then spent a maven run, a
browser launch and a fix attempt rediscovering it. Now the same block:

1. recovers the key from `urls_visited` when a page step 02 opened supplies it — not a
   guess, an address the browser loaded;
2. `sys.exit(1)` on anything left, with `"error": "missing_url_properties"` in the audit.

A key that survives (1) means the browser never visited that page, so the test navigates
somewhere step 02 never validated — which is the one thing this pipeline does not do.
`BrowserHelper.navigateTo` also rejects a null URL by name now, so the same mistake made
by hand reads as a missing property rather than a Playwright protocol error.

---

## The compile gate — step 03 builds what it wrote

Every other guard in step 03 reads the generated code. None of them ran a compiler, so
`import automation.core.web.BasePage` — a package that has never existed — reached step 04
intact and cost the initial run, the no-change flakiness re-run, and one of only two fix
attempts. The framework's own CLAUDE.md has always made `mvn compile` step 1 of its
mandatory self-test; this is the agent finally doing it.

| Where | What happens |
|-------|--------------|
| **03 Generate**, after the write loop | `mvn -q test-compile` in the framework checkout. `test-compile`, not `compile`, so the generated *test* class is covered too. |
| on failure | `compile_errors()` parses javac's `[ERROR] path:[line,col] msg` lines. One targeted repair pass over only the generated files named, handed the real `automation.core` class list so an invented package has somewhere to land. Guarded by `validate_fix` under `COMPILE_REPAIR_MAX_DIFF_LINES`, accepted per file. |
| still failing | `sys.exit(1)` with `"error": "compile_failed"` and the javac errors in `03-generate.json`. |
| errors only in files this run did not write | abort too, saying so — the checkout does not build on its own, which is not something a repair pass can fix. |
| maven missing, timed out, or no `pom.xml` | skipped, not failed. That is infra, and failing the run on it blames the wrong thing. |

Safe to place in step 03 because there is no `git checkout -f` between steps 03 and 04 in
the isolated worktree — what step 03 compiles is what step 04 runs.

Key naming: the host alone is `{feature}.url` (matching the existing `saucedemo.url`), the
API base is `{feature}.api.url`, and anything with a path is named for its last meaningful
segment — `/nlogin/login` → `{feature}.login.url`. Id-like segments are skipped.

Credentials use the same properties file through `shared/credential_properties.py` but are
the opposite case: never committed. Both share `shared/properties_file.py` so the file
location and the "never overwrite a human's value" rule exist in one place.

---

## Locator baselines reach the PR

The framework writes an element fingerprint per page object on every successful page
load — `src/main/resources/baselines/NaukriLoginPage.json` — and that file is the only
record of what a locator matched while it worked. A generated page object shipped
without one leaves the next diagnosis of that page with nothing to compare against.

Until this was fixed the authoring PR never carried them, for a reason that is easy to
miss: 05_ship's branch creation is a `checkout -f -B`, so the untracked baseline the
green run had just written was wiped off disk *before* there was a branch to commit it
onto. So the ship step now reads the baselines **before** it touches the branch, and
commits them last, once the code and any fixes are in:

| Where | What happens |
|-------|--------------|
| **05 Ship**, before branching | `baseline.promoted()` reads every fingerprint the run left in `src/main/resources/baselines/` — never `pending/`, which holds records from a test that did not finish. |
| **05 Ship**, after the fix commits | `baseline.changed()` keeps only the ones whose substance differs from HEAD, and they land in their own commit, listed in the PR body. |

Comparison ignores `recordedAt`: the framework rewrites it on every load, so comparing
raw bytes would put an empty baseline diff in every PR. The timestamp itself has to stay
in the file — `baseline.load()`'s staleness guard uses it to reject a record written by
the failing run itself.

The same rule now holds in the other two agents that raise PRs — `01_fix.py` in the
healing agent (where the heal is exactly what makes the old fingerprint stale) and
`05_ship.py` in the adaptation agent — all through `shared/baseline.py`, which
`scripts/commit_baselines.py` also uses after a green CI suite.

---

## Step narration — one `logStep` per step

The run report prints one line per `logStep`. A test that opens with a single run-on
summary — *"Login to Naukri, toggle the trailing dot in Profile Summary, save the change,
and verify it persists after page reload"* — passes every check that existed before
(`logStep` present, in a test class, plain English) and still produces a one-line report
for a four-step scenario: when it fails, the report cannot say which step broke. The
derived intent contract is built from the same strings, so one sentence collapses four
checkable claims into one blob.

Presence was already checked (`logstep_present` in `shared/edit_guards.py`); granularity
is what this adds, in two places:

| Where | What happens |
|-------|--------------|
| **03 Generate**, in the prompt | Rule 7b: one `logStep` per plan step, immediately before the call(s) that carry it out; setup lines get none; a helper may encapsulate one step, never the whole scenario. Shown with a wrong/right pair. |
| **03 Generate**, after codegen | `_repair_step_narration()` audits each generated test class with `shared/logstep_narration.py` and runs one targeted repair pass over the ones that fall short, guarded by `validate_fix` and rejected unless it actually adds narration. What survives is recorded in `03-generate.json` → `under_narrated_tests`. |

The expectation is deliberately the *smaller* of two bounds: the plan's own step count for
that method (setup steps dropped), and the number of statements in the method that
actually drive or check the app. A method cannot narrate more groups than it has work to
narrate, so capping by the second is what keeps the guard from firing on correct code —
and it is why a test whose whole scenario hides behind one helper call is asked for two
steps rather than five. The repair is given the helper and page objects generated
alongside it as read-only context and may only call methods that already exist there.

---

## Key Rules for Existing Module Appending

When `existing_module=true` in the plan:
- Do NOT recreate `{Feature}Data.java`, `{Feature}Builder.java`, or `{Feature}Api.java` unless
  new fields/endpoints are needed
- DO add new methods to `{Feature}Helper.java` (API and web workflows)
- DO add new page objects if new pages are involved
- DO create a new test class file (e.g., `{Feature}NewScenarioTest.java`) rather than modifying
  an existing test file — this avoids merge conflicts and preserves existing tests
- Read the existing Helper/Data files before generating to avoid duplicating methods or fields
