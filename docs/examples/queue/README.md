# Queue input examples

Three worked examples per agent, each a different *type* of input, all against the
`saucedemo` module — so every file name starts with `saucedemo_`. They live
here, not in `agents/<agent>/queue/`, because that directory is the agent's
**live inbox**: the server writes into it, and `run.sh` moves each item into
`queue/processed/` once the run completes. It is git-ignored for that reason — a
file committed there would show up as a deletion the first time anyone ran the
agent.

| Agent | Queue input | Written by | Examples vary by |
|---|---|---|---|
| `test-authoring-agent` | `<module>.txt` — plain-English test steps | a human | `Type:` — web / api / both |
| `test-adaptation-agent` | `<module>.txt` — a change note | a human, or `shared/adaptation_handoff.py` | an enhancement or a change kind — and whether the agent may apply it |
| `test-healing-agent` | `<build_tag>.json` — a failure handoff | `test-triaging-agent` | diagnosis category |
| `test-triaging-agent` | *none* — it has no queue | — | — |

**The server seeds these automatically.** On its first boot in a checkout,
`qa_agents_server` copies each agent's examples into its queue root, and each
signed-in user gets their own copy the first time they open that queue — see
[`seed_examples.py`](../../../qa_agents_server/seed_examples.py). It never
overwrites a queued file, never re-creates one already in `processed/`, and skips
a queue once seeded, so anything you delete stays deleted. `QA_SEED_EXAMPLES=false`
disables it; deleting a queue directory re-arms it for that queue. The `cp` commands
below are for putting an example back by hand.

`test-triaging-agent` takes a CI build tag instead (`make run
AGENT=test-triaging-agent BUILD_TAG=ProdSanity-541`), or scouts the results
database for unanalysed builds when given none. There is nothing to hand-write,
so it has no examples here.

---

## test-authoring-agent

The `Type:` line is the axis: it decides whether the generated test drives an
API, a browser, or both in one interleaved flow.

| Example | `Type:` | Shows |
|---|---|---|
| [`saucedemo_web_product.txt`](test-authoring-agent/saucedemo_web_product.txt) | `web` | Browser-only flow — login, open a product, assert its details and the cart badge |
| [`saucedemo_api_todos.txt`](test-authoring-agent/saucedemo_api_todos.txt) | `api` | Full CRUD — POST, GET, PUT, PATCH and DELETE, each with its curl, plus a list check |
| [`saucedemo_both_checkout.txt`](test-authoring-agent/saucedemo_both_checkout.txt) | `both` | Interleaved — create a shopper via the API, check out with its values in the UI, delete it via the API |

All three target the existing `saucedemo` module, whose API half is
JSONPlaceholder (the same base URL `SauceDemoHelper` uses), so the agent extends
the SauceDemo tests rather than creating a new module.

The API examples state their auth and headers up front and give a curl for every
call; step 02 runs a given curl exactly as written. JSONPlaceholder needs no auth
and fakes every write — a POST returns an id that cannot be read back — so
follow-up calls use an existing record.

```bash
cp docs/examples/queue/test-authoring-agent/saucedemo_api_todos.txt \
   agents/test-authoring-agent/queue/saucedemo.txt

make run AGENT=test-authoring-agent MODULE=saucedemo
```

Two things worth knowing. `Module:` is the only header that names the module —
it decides the Java package and the module directory, and it is matched
literally (`Feature:` is *not* an alias and will be ignored). And the declared
`Type:` is a hint, not the source of truth: step 01 resolves the real test type
from what the steps actually do and logs the discrepancy if they disagree.

## test-adaptation-agent

A change note does not have to describe a breakage. It can just as well extend an
existing test with more steps and checks — the `coverage_added` kind, which the
agent applies: new assertions are fine, and what the guards refuse is removing,
weakening or changing an existing one. For a real
product change, the *kind* decides how much authority the agent has. Most kinds
it can apply; `outcome_changed` and `content_changed` it may only propose, since
those mean the specification moved rather than the test breaking.

| Example | Kind | Shows |
|---|---|---|
| [`saucedemo_cart_details.txt`](test-adaptation-agent/saucedemo_cart_details.txt) | `coverage_added` | No product change — adds quantity, price and checkout checks to `verifyProductAppearsInCart` |
| [`saucedemo_api_contract.txt`](test-adaptation-agent/saucedemo_api_contract.txt) | `api_contract` | `getPostById`, `createPost` and `updatePost` read renamed fields, and writes need a new `X-Request-Id` header |
| [`saucedemo_saved_for_later.txt`](test-adaptation-agent/saucedemo_saved_for_later.txt) | `outcome_changed` | Step 5 of `simulateWebLifecycle` now proves the wrong thing — escalate-only, the agent proposes and writes nothing |

```bash
cp docs/examples/queue/test-adaptation-agent/saucedemo_cart_details.txt \
   agents/test-adaptation-agent/queue/saucedemo.txt

make run AGENT=test-adaptation-agent MODULE=saucedemo

# propose without writing, for any change note
ADAPTATION_APPLY=false make run AGENT=test-adaptation-agent MODULE=saucedemo
```

Each note names, in `Affects:`, the existing `SauceDemoWebTest` / `SauceDemoApiTest`
methods it touches. The web notes carry no `URL:` — web exploration starts from
the module's own entry point (`saucedemo.url`). The API note keeps `API URL:`,
because step 03 probes no endpoint without it.

`saucedemo_cart_details.txt` matches the live site — every element it names exists
today. The other two are staged: SauceDemo and JSONPlaceholder do not actually
behave that way, so step 03's live exploration will observe today's behaviour, not
the one the note describes.

## test-healing-agent

Handoffs are written by `test-triaging-agent`, not by hand — `write_handoff` in
[`actions/05_ship.py`](../../../agents/test-triaging-agent/actions/05_ship.py)
queues only failures classified `AUTOMATION_ISSUE` at `HIGH` confidence whose
category the healing agent can act on. All three examples pass that gate. A
handoff's file name is its `build_tag`, which is how `BUILD_TAG=` finds it.

| Example | Category | Shows |
|---|---|---|
| [`saucedemo_sanity_541.json`](test-healing-agent/saucedemo_sanity_541.json) | `ELEMENT_NOT_FOUND` | Two tests broken by one selector — a single shared `cause_group_key`, so one fix closes both. The first failure has captured artefacts, the second has none |
| [`saucedemo_nightly_208.json`](test-healing-agent/saucedemo_nightly_208.json) | `AMBIGUOUS_LOCATOR` | A selector that now matches two elements. The remediation is to narrow it, not to replace it — a match count above zero is not proof the selector is fine |
| [`saucedemo_rc_92.json`](test-healing-agent/saucedemo_rc_92.json) | `LOCATOR_STALE` | Three failures across two page objects — two independent cause groups, fixed separately. One test is listed in `flaky_tests`, which is how the agent tells an intermittent test from a genuine break |

```bash
HANDOFF_FILE=docs/examples/queue/test-healing-agent/saucedemo_sanity_541.json \
  make run AGENT=test-healing-agent

# or, via the queue:
cp docs/examples/queue/test-healing-agent/saucedemo_sanity_541.json \
   agents/test-healing-agent/queue/
make run AGENT=test-healing-agent BUILD_TAG=saucedemo_sanity_541
```

Two caveats when replaying these. The artefact paths (`dom_snapshot`,
`trace_path`, `screenshot`) point into a triaging audit directory that will not
exist on your machine — the healing agent tolerates that, but diagnoses without
DOM evidence and so reaches a weaker verdict than it would on a real handoff.
And while every test they name is a real `SauceDemoWebTest` method, the breakages
are staged — against the live site those selectors still resolve, so step 00 will
not reproduce the failure.

To heal a single real test instead, skip the queue entirely — standalone mode
reproduces the failure and builds its own handoff:

```bash
make run AGENT=test-healing-agent \
  TEST_NAME=automation.saucedemo.SauceDemoWebTest#verifyProductAppearsInCart
```
