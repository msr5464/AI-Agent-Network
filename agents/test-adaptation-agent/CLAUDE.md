# test-adaptation-agent — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Updates automation tests when the **product** changes — a step is inserted, a
wizard's pages merge, a `<select>` becomes a combobox, a route changes, a form
gains a required field. Driven by a plain-English change note a human writes, so
it can run **before** the tests go red.

That makes it a different job from `test-healing-agent`, not a bigger version of
one. Healing is reactive: a test failed, and the question is why. This is
proactive: somebody knows what changed, and the question is what the tests should
now do about it.

## Why this is not a setting on the healing agent

Healing holds exactly one repair hypothesis — the selector string is stale — and
that narrowness is enforced at six separate layers on purpose. Its own CLAUDE.md
says why: *the verification loop cannot catch a fix built on a wrong diagnosis,
because the easiest way to make a page assertion pass is to weaken it.*

So widening its diff budget does not buy the ability to repair a changed flow. It
buys the ability to delete an assertion and ship a permanently green broken test,
with a passing run as proof. Once an edit may add and remove steps, "the test
passes" stops being evidence — a test that asserts nothing passes fastest of all.

**What replaces it is the intent contract.** The mechanism is mutable: locators,
waits, step order, which pages get visited. The proof is not.

## The change note

`queue/<module>.txt`, same convention as the authoring agent:

```
Module: checkout
Type: web                      # api | web | both
URL: https://app.staging.example.com
Affects: automation.checkout.*

What changed:
1. After login a "Choose workspace" screen now appears before the dashboard.
2. The 3-step checkout wizard is now 2 steps.
3. "Place Order" was renamed to "Confirm & Pay".

Expected outcome unchanged: an order is placed and a confirmation number is shown.
```

Each numbered item is classified into a **kind**, and the kind decides how much
authority the repair gets. This is the whole payoff of being triggered by a note
rather than by a failure: telling "a step was inserted" apart from "the previous
step silently did nothing" is the hardest inference in the problem, and a human
writing it down has already answered it.

| kind | what changes | diff budget |
|---|---|---|
| `locator` | an **existing** selector string is replaced | 6 |
| `interaction` | the wrapper call *and* the selector (select → combobox) | 20 |
| `route` | a URL or route constant | 10 |
| `step_insert` | a new step in the flow | 40/file |
| `step_merge` | steps merged or removed | 40/file |
| `field_added` | something the page object does not model at all — a new required field, or a control with no locator yet | 60 across ≤3 files |
| `api_contract` | request/response shape, status, header | 30 |
| `test_data` | a fixture or default | 30 |
| `page_object_new` | a whole new page object | 300 (the one whole-file case) |
| `content_changed` | expected copy only | **escalate — proposed, never applied** |
| `outcome_changed` | what the feature *does* | **escalate — the spec moved, not the test** |

---

## Architecture

```
run.sh (orchestrator)
  │
  ├─ 01_parse_change.py   [Python + Claude]  change note → classified items
  ├─ 02_scope.py          [Python only]      blast radius + FROZEN intent contracts
  ├─ 03_explore_api.py    [Python only]      real GETs against the live API
  ├─ 03_explore_web.py    [Python + Claude]  drive a browser → ordered flow map
  ├─ 03_combine_explore.py[Python only]      one summary file for both halves
  ├─ 04_adapt.py          [Python + Claude]  edit → guards → compile → verify
  └─ 05_ship.py           [Python only]      branch, commit per item, PR, Slack
```

Step 03 is split by interface, mirroring authoring's `02_validate_api` +
`02_validate_web` sharing one numeric slot. The framework tests APIs too, and a
product change is just as often a renamed request field as a moved button.

**The combined `03-explore.json` is what the server polls**, not either half.
Keying a shared step on one half — as authoring does with `02-validate-web.json` —
means a run that only exercised the other half never completes the step.

---

## What makes this safe

### 1. Freeze the contract before editing

`02_scope.py` measures what every in-scope test proves — every assertion reachable
through the whole helper call graph, not just the ones visible in the test file —
and writes it into `02-scope.json`. Every later guard compares against **that
stored copy**.

Re-deriving the contract after the edit would describe the edited code, and a
conservation check against that approves whatever the edit just did. There is a
test for exactly this (`tests/unit/test_intent.py`).

### 2. Guess, then measure, then edit — never guess then edit

Step 02 nominates page objects by name similarity. That is cheap, usually right,
and still a guess: two modules can both own a `LoginPage`, and a page object stops
matching its own name long before it stops being the right file.

So once exploration has reported what is on each page, step 03 **measures** which
page object each observed page actually is — with
`page_identity.page_object_coverage`, the same rule the healing agent applies to a
failure DOM, so both agents answer "is this that page?" the same way. Step 04 then
orders its candidates by the measurement and tells the model which file goes with
which observed page.

It measures against every page object in scope, not only the ones the noun filter
nominated: narrowing the measurement to the guess would leave a page whose page
object the guess missed with nothing to match against, which is the failure the
step exists to remove.

The honest limit: an inventory is a bounded sample of a page, so a zero means
*not observed*, not *absent*. Every report carries `sampled: true`, and the
mapping ranks candidates rather than ruling one out.

### 3. Transcribe, do not invent

Every interaction an edit **adds** must correspond to a step in the flow map that
exploration actually observed, matched on the element's name. A flow-map step
whose selector could not be verified unique justifies nothing — `unverified` is not
`yes`.

Selector uniqueness is recounted **in Python** against the element inventory the
browser reported. The model's own count is kept as `claimed_by_model` and used for
nothing.

### 4. Read the entry path; do not derive it

The agent edits an **existing** test, and that test already states how it signs
itself in. Exploration used to ignore that and reconstruct login from repo-wide
convention — and the framework contradicts every convention it relied on:

| assumed | actual |
|---|---|
| the login page object is `LoginPage` | `LoginPage` twice, `NaukriLoginPage` once |
| the URL key is `{module}.url` | `saucedemo.url`, `naukari.url` — but `githubUrl` |
| a module logs in one way | `GitHubLoginTest` does both, in one file |
| a session exists if tests pass | `storeSession()` is keyed on the `ProjectName` enum (GitHub, SauceDemo, FullSuite) and has one caller |

So `02_scope.py` runs `shared/entry_path.extract()` over the test it is about to
verify and records one of three modes:

- **`stored_session`** — the test loads a storage state. Reuse *that file*. If it
  is missing, name the test that writes it; do not invent one.
- **`credential`** — the test reads named properties and calls a login method.
  Run *that method* with *those keys*, through `automation.core.SessionMinter`.
- **`none`** — the test never signs in. Explore unauthenticated rather than
  hard-stopping for a session the flow was never going to use.

`SessionMinter` receives property **keys** and resolves them inside the JVM, so
no credential crosses a command line — a password in `-Dexec.args` is a password
in `ps` and in every parent process's log.

Running the real login means the module's own navigation, post-submit steps and
page-object assertions all happen, so verification is the framework's own rather
than a heuristic. Two consequences worth knowing:

- **A login method that throws may still have authenticated.** Login helpers end
  by constructing a destination page object that asserts itself loaded, so a
  destination that changed fails the whole call. Refusing the session then is
  circular — that page is what the agent was going to look at. The state is kept,
  flagged `degraded`, and the post-login error is reported as evidence.
- **A bounce is not a session.** A rejected or raced login redirects back to the
  login form with cookies already set, which looks authenticated and is not.
  `mint_session._bounced()` compares the landed URL's path against the module's
  configured entry URL and refuses. Minting writes to a staging file and promotes
  it only on success, so a failed attempt can never delete a session that worked.

## Guards

Run over the combined diff of one change item, before anything compiles:

| guard | rejects |
|---|---|
| `validate_fix` | oversized diffs, emptied files, lost methods |
| `assertion_graph.conserved` | an assertion removed, weakened, or made conditional — **anywhere in the call graph** |
| `no_new_swallowing` | empty catch, `Thread.sleep`, `@Ignore`, `enabled=false`, `assumeTrue`, `SkipException` |
| `wrapper_compliance` | raw Selenium — `driver.findElement`, `.sendKeys()`, `new WebDriverWait` |
| `logstep_present` | an interaction added to a test class with no `logStep` |
| `steps_justified` | an interaction matching nothing exploration observed |
| `matches_negative` | an anchor that also matches the logged-out or error page |

The last one is the anti-tautology check: a selector that matches the logged-out
page is not proof of a successful login.

## The change-item transaction

Work is organised by change item, **not by file**. One item routinely spans a new
page object, a helper method and a call site; applying those one at a time leaves
the repo uncompilable between writes and makes rollback ambiguous.

```
snapshot every target → apply all edits → guards → compile → verify
                      → on ANY failure, restore EVERY file
```

**Compiling before running anything is not an optimisation.** `00_reproduce.py`
classifies `cannot find symbol` as `INFRA_BUILD`, which routes to *skip, don't
call the model* — right when the repo arrived broken, and completely wrong when
our own edit broke it. Compiling straight after the edit tells the two apart.

`run.sh`'s ERR trap restores the snapshots too: a crash must not leave the shared
automation checkout in a state that is neither the original nor a working change.

## The automation repo

Cloned if absent, exactly as the other two agents do — healing in Python,
authoring in its run.sh. All three now share one implementation,
`shared/workspace.py`, taking healing's version because `git clone` persists
whatever URL it was handed into `.git/config`: authoring's leaves the token there
for the life of the checkout, healing's strips it back out.

**Syncing is a fetch, never a reset.** authoring follows its clone with
`checkout -f` + `pull`, which is right for an agent that only adds new files and
wrong here: this agent refuses to start on a dirty tree precisely so nobody's
uncommitted work is swept into its commit, and a force-checkout would destroy
exactly what that gate protects. So it fetches, reports how far behind the
checkout is, and leaves HEAD alone.

## Escalate — never attempt

- the note says the expected **outcome** changed (the spec moved, not the test);
- `unexplained_failures` is non-empty — a failure no line of the note accounts
  for. A human asserted change A; that says nothing about an unrelated defect B.
  **This is the change-vs-bug gate**;
- `UNREACHABLE_STATE` covers the changed area;
- the test's declared entry path cannot produce a session — a stored session
  file that is missing, or a login that runs but lands back on the login form.
  When the login the test itself uses does not authenticate, that is a finding
  about the module, not a reason to sign in some other way;
- the flow's terminal action is destructive and `ADAPTATION_SANDBOX` is unset;
- an **existing** page object would need regenerating wholesale;
- assertion conservation would have to be violated to make the test green;
- the blast radius exceeds `ADAPTATION_BLAST_MAX_TESTS` — bigger than one agent run;
- a changed expected string: propose, never apply.

## Two ways in

**GUI** — `POST /agents/test-adaptation-agent/run {"module": "checkout"}`. The
change note is editable over `GET/POST /agents/test-adaptation-agent/queue`, the
same writable `.txt` queue the authoring agent uses.

**CLI**

```bash
make run AGENT=test-adaptation-agent MODULE=checkout
EXPLORE_ONLY=true make run AGENT=test-adaptation-agent MODULE=checkout
ADAPTATION_APPLY=false make run AGENT=test-adaptation-agent MODULE=checkout   # propose only
START_FROM_STEP=4 SESSION_ID=<sid> make run AGENT=test-adaptation-agent  # resume
```

Resume matters more here than anywhere else in this repo: exploration is the
expensive half, and a failed edit must never cost a second thirty-minute browser
run. `TESTING_MODE=true` caches steps 01–03 under `cache/<module>/`.

## Locator baselines are committed with the edits

The fingerprints under `src/main/resources/baselines/` describe what each locator matched
on the page as it was. Adaptation is the case where leaving them behind hurts most: the
product moved, the tests were adapted to follow it, and a PR without the refreshed
baselines leaves the repo describing the page as it used to be — so the next drift is
diagnosed against a record that is already wrong.

Ship now adds a final path-scoped commit for the baselines whose substance changed
(`shared/baseline.py`, `recordedAt` excluded from the comparison). Same rule as the item
commits above: only the paths that changed, never `git add -A`.


## Gate Values

**`.fix-passed`** — `true` (something applied and verified) / `false` (applied,
tests still failing) / `skipped` (nothing eligible, or propose-only).

**`.verdict`** — always `NEEDS-REVIEW`. Asserted in `05_ship.py`, not computed:
an agent that may change test *steps* always needs a human, and a branch could
drift.

**`.skip-reason`** — `infra` (leave the note queued) / `no-work` / `escalate` /
`unsafe` / `no-session` / `unreachable` / `explore-only`.

## Audit Trail

`agents/test-adaptation-agent/audit/$SESSION_ID/`

| File | Purpose |
|---|---|
| `00-session-init.md` | session metadata |
| `01-parse-change.json` + `.md` | classified change items |
| `02-scope.json` + `.md` | blast radius, cost estimate, **frozen intent contracts** |
| `03-explore-api.json`, `03-explore-web.json` | per-interface exploration |
| `03-explore.json` + `.md` | combined flow map — the file the server polls |
| `04-adapt.json` + `.md` | per-item diffs, guard results, conservation reports |
| `05-ship.json` + `.md` | PR URL, verdict, escalations |
| `.snapshots.json` | transient; the ERR trap's rollback source |

## Key Rules

1. **The mechanism is mutable; the proof is not.** Assertions survive, always.
2. **Freeze the contract before editing.** Never re-derive it afterwards.
3. **Transcribe, do not invent.** Every added step maps to an observed one.
4. **`storage_state`, never a password.** `shared/claude.py` writes the whole
   command line to its log; a credential in a prompt is a credential on disk.
   An expired session is a hard stop — exploring with one lands on a login page
   and "discovers" that the entire flow changed. The session itself comes from
   the entry path the test declares, never from a reconstructed login.
5. **Refusing is a correct outcome.** Escalation is the design working.
6. **Compile before verifying**, so our own broken edit is never misread as infra.
7. **All-or-nothing per change item**, including on crash.
8. **The PR is always NEEDS-REVIEW.**
