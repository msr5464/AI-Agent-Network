# Queue input examples

Three worked examples per agent, each a different *type* of input. They live
here, not in `agents/<agent>/queue/`, because that directory is the agent's
**live inbox**: the server writes into it, and `run.sh` moves each item into
`queue/processed/` once the run completes. It is git-ignored for that reason — a
file committed there would show up as a deletion the first time anyone ran the
agent.

| Agent | Queue input | Written by | Examples vary by |
|---|---|---|---|
| `test-authoring-agent` | `<module>.txt` — plain-English test steps | a human | `Type:` — web / api / both |
| `test-adaptation-agent` | `<module>.txt` — a change note | a human, or `shared/adaptation_handoff.py` | change kind — and whether the agent may apply it |
| `test-healing-agent` | `<build_tag>.json` — a failure handoff | `test-triaging-agent` | diagnosis category |
| `test-triaging-agent` | *none* — it has no queue | — | — |

**The server seeds these automatically.** On its first boot in a checkout,
`qa_agents_server` copies each agent's examples into its queue so the UI has
something to show — see
[`seed_examples.py`](../../../qa_agents_server/seed_examples.py). It never
overwrites a queued file, never re-creates one already in `processed/`, and skips
an agent once seeded, so anything you delete stays deleted. `QA_SEED_EXAMPLES=false`
disables it; clearing an agent's queue directory re-arms it. The `cp` commands
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
| [`web_github_repo.txt`](test-authoring-agent/web_github_repo.txt) | `web` | Browser-only flow — login, navigate, assert an element is visible |
| [`api_github_users.txt`](test-authoring-agent/api_github_users.txt) | `api` | Request-only flow — status codes, field assertions, list contents |
| [`both_github_profile.txt`](test-authoring-agent/both_github_profile.txt) | `both` | Interleaved — capture values from the API, then assert the UI displays them |

```bash
cp docs/examples/queue/test-authoring-agent/api_github_users.txt \
   agents/test-authoring-agent/queue/github.txt

make run AGENT=test-authoring-agent MODULE=github
```

Two things worth knowing. `Module:` is the only header that names the module —
it decides the Java package and the module directory, and it is matched
literally (`Feature:` is *not* an alias and will be ignored). And the declared
`Type:` is a hint, not the source of truth: step 01 resolves the real test type
from what the steps actually do and logs the discrepancy if they disagree.

## test-adaptation-agent

The axis here is the *kind* of change, because that decides how much authority
the agent has. Most kinds it can apply; `outcome_changed` and `content_changed`
it may only propose, since those mean the specification moved rather than the
test breaking.

| Example | Kind | Shows |
|---|---|---|
| [`web_field_added.txt`](test-adaptation-agent/web_field_added.txt) | `field_added` | A new UI control the page object has no locator for — the agent adds one |
| [`api_contract_changed.txt`](test-adaptation-agent/api_contract_changed.txt) | `api_contract` | Renamed and relocated response fields, plus a newly required header |
| [`escalate_outcome_changed.txt`](test-adaptation-agent/escalate_outcome_changed.txt) | `outcome_changed` | The expected result itself changed — escalate-only, the agent proposes and writes nothing |

```bash
cp docs/examples/queue/test-adaptation-agent/web_field_added.txt \
   agents/test-adaptation-agent/queue/saucedemo.txt

make run AGENT=test-adaptation-agent MODULE=saucedemo

# propose without writing, for any change note
ADAPT_APPLY=false make run AGENT=test-adaptation-agent MODULE=saucedemo
```

`Affects:` is optional but worth giving. Without it the blast radius is derived
from `Module:` alone, which is a weaker claim — and step 01 reports it as such.

## test-healing-agent

Handoffs are written by `test-triaging-agent`, not by hand — `write_handoff` in
[`actions/05_ship.py`](../../../agents/test-triaging-agent/actions/05_ship.py)
queues only failures classified `AUTOMATION_ISSUE` at `HIGH` confidence whose
category the healing agent can act on. All three examples pass that gate.

| Example | Category | Shows |
|---|---|---|
| [`ProdSanity-541.json`](test-healing-agent/ProdSanity-541.json) | `ELEMENT_NOT_FOUND` | Two tests broken by one selector — a single shared `cause_group_key`, so one fix closes both. The first failure has captured artefacts, the second has none |
| [`NightlyRegression-208.json`](test-healing-agent/NightlyRegression-208.json) | `AMBIGUOUS_LOCATOR` | A selector that now matches three elements. The remediation is to narrow it, not to replace it — a match count above zero is not proof the selector is fine |
| [`ReleaseCandidate-92.json`](test-healing-agent/ReleaseCandidate-92.json) | `LOCATOR_STALE` | Three failures across two page objects — two independent cause groups, fixed separately. One test is listed in `flaky_tests`, which is how the agent tells an intermittent test from a genuine break |

```bash
HANDOFF_FILE=docs/examples/queue/test-healing-agent/ProdSanity-541.json \
  make run AGENT=test-healing-agent

# or, via the queue:
cp docs/examples/queue/test-healing-agent/ProdSanity-541.json \
   agents/test-healing-agent/queue/
make run AGENT=test-healing-agent BUILD_TAG=ProdSanity-541
```

Two caveats when replaying these. The artefact paths (`dom_snapshot`,
`trace_path`, `screenshot`) point into a triaging audit directory that will not
exist on your machine — the healing agent tolerates that, but diagnoses without
DOM evidence and so reaches a weaker verdict than it would on a real handoff.
And the tests they name must exist in your automation repo for step 00 to
reproduce anything.

To heal a single real test instead, skip the queue entirely — standalone mode
reproduces the failure and builds its own handoff:

```bash
make run AGENT=test-healing-agent \
  TEST_NAME=automation.saucedemo.SauceDemoWebTest#sortProductsByPriceLowToHigh
```
