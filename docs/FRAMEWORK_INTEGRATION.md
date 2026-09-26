# Framework Integration Guide

QA Agent Network works against any automation framework through a small plugin
layer. This guide covers what to build to add one, and — more importantly — what
the **target repository** has to produce for the agents to be any good.

## First: which "framework" are we talking about?

The word does two jobs here, and conflating them is the mistake this
architecture was originally built around. Keep them apart:

| | The target repo's **convention** | The agents' **instrument** |
|---|---|---|
| What | The syntax its tests are written in, how they run, what artifacts they leave, what its errors mean | The browser the agents open themselves to read a DOM and check whether a candidate locator resolves |
| Pluggable? | **Yes. This is the entire job.** | **No — always Playwright.** |
| Lives in | `shared/frameworks/<name>_plugin.py` | `shared/mcp_config.py`, `shared/locator_verify.py`, `shared/locator_candidates.py` |

The instrument speaks CDP, and CDP is a browser-level protocol: a browser has no
idea which framework drove it there. So a Selenium repo is inspected with the
Playwright MCP server, and that is correct rather than a workaround. (Selenium 4
exposes the same DevTools port, which is what makes attaching possible.)

**You do not need to provide anything for the instrument.** There is no
`MCPProvider` to implement — an earlier version of this guide described one, and
both implementations of it returned the same Playwright server.

---

## Step 1: write the plugin

Create `shared/frameworks/<name>_plugin.py` implementing the four interfaces in
`shared/frameworks/base.py`, plus a `FrameworkPlugin` subclass that exposes them
as its `telemetry`, `runner`, `diagnostics` and `code` properties.
`shared/frameworks/selenium_plugin.py` is the worked example. Every
`@abc.abstractmethod` in `base.py` must be implemented; the lists below cover the
ones with non-obvious rules.

### `TelemetryParser`
Reconstructs what the test did before it failed. The actions that **succeeded**
matter as much as the one that did not: they establish how far the journey got.

- `discover(results_dir, method_name)` — find your framework's artifacts. Do not
  skip this. Callers used to glob for Playwright's `traces/*.zip` themselves,
  which meant a parser accepting anything else could never be handed a path it
  would take.
- `read_actions(path)` — parse one artifact into the **action schema** documented
  on `TelemetryParser.ACTION_KEYS`. Call `self.normalise(...)` on each record.
  Consumers index these keys directly, so returning your raw log records will
  raise `KeyError` inside prompt construction.
- `failing_action(actions)` — the one that broke.
- `read_network(path)` — optional (defaults to `[]`): HAR-shaped network records
  if your artifact carries them. `shared/trace_network.py` interprets them.
- `NOISE_ACTIONS` — optional: action names to drop from the prompt timeline.

### `TestRunner`
- `detect_command(workspace, class_simple, method)` — CLI arguments to run one test.
- `apply_browser_mode(cmd, properties)` — translate `HEADLESS_BROWSER` into your
  framework's native flag or property.

### `DiagnosticEngine`
Translates your framework's error text into semantics the agents act on.

- `is_ambiguous_locator(message)` — the selector matched several elements. If
  your framework silently takes the first match, return `False` honestly.
- `is_locator_resolution_failure(message)` — the locator matched nothing usable:
  not found, stale, not interactable, timed out waiting for it. Healing's failure
  classifier and element-name extraction ask this rather than matching exception
  names themselves.
- `NULL_VALUE_SIGNALS` — lower-case error text meaning a null reached an input
  call (almost always an unset credential property), so it is not diagnosed as a
  locator problem.

Match the phrasings your framework **actually emits**. The Selenium engine
originally matched `"multiple elements matched"`, a string no Selenium binding
produces, so it returned `False` for every input and the verdict never fired.

### `CodeEngine`
The hardest part: parse and generate this repo's locator code.

- `extract_locators(source)` — every locator declared in a page object.
- `normalize_selector(raw)` — to plain CSS so it can be evaluated against a
  captured DOM. Return `None` for XPath.
- `is_dom_selector(raw)`, `remove_framework_suffixes(selector)`,
  `quote_css_value(value)`, `map_role(role)` — small helpers the locator engine
  calls; see the docstrings in `base.py`.
- `emit_locator(**kwargs)` — native code for a locator. Handle at minimum
  `testid`, `role`+`name`, `placeholder`, `label`, `alt`, `title`, `text`,
  `selector`. **Never return an empty snippet**: an unhandled branch that falls
  through to `{"python": "", "java": ""}` reaches the prompt as a blank and the
  model invents something plausible instead. Quote attribute values through
  `quote_css_value` — `[data-testid=my testid]` is not valid CSS.
- `build_has_text_selector(...)` — must be valid for a **live browser**, because
  that is what receives it. Not a BeautifulSoup or jQuery extension.
- Optionally emit a `findby` key alongside `python`/`java` when the framework
  declares locators as page-object fields rather than inline calls.
- `ELEMENT_TYPES` — the type names page objects declare elements with.
- `LOCATOR_CALLS` — the calls whose first string argument is a selector; the edit
  guards read selectors out of added lines through these.
- `RAW_DRIVER_CALLS` — `(pattern, label)` pairs for calls that bypass the repo's
  wrappers; an edit that adds one is rejected.

`tests/unit/test_frameworks.py` enforces most of the above for every registered
plugin. Add yours to the parametrisation and it is checked automatically.

---

## Step 2: register it

Add it to `_BUILDERS` in `shared/frameworks/__init__.py`:

```python
_BUILDERS = {
    detect.PLAYWRIGHT: PlaywrightPlugin,
    detect.SELENIUM: SeleniumPlugin,
    detect.CYPRESS: CypressPlugin,
}
```

Then teach `shared/frameworks/detect.py` about it: a name constant, an entry in
`SUPPORTED` (the contract tests in `tests/unit/test_frameworks.py` are
parametrised over it), and a build-file marker:

```python
CYPRESS = "cypress"
SUPPORTED = (PLAYWRIGHT, SELENIUM, CYPRESS)

_BUILD_MARKERS = (
    ...
    (CYPRESS, re.compile(r"\"cypress\"\s*:")),
)
```

**Detection is how the framework is chosen, not configuration.** A repo either is
or is not a Cypress repo, so asking a human to select it can only be redundant or
wrong — and it was wrong: `AUTOMATION_FRAMEWORK=selenium` was set against a
Playwright repo, which silently emptied locator extraction, rejected every trace,
and disabled ambiguous-locator diagnosis, with no error anywhere.

Precedence (`detect.resolve()`): `AUTOMATION_FRAMEWORK` → the repo's build files →
`config/repo-map.json` → Playwright. `AUTOMATION_FRAMEWORK` is an explicit
override and warns loudly when it contradicts the repo. There is deliberately no
Studio setting for it: a dropdown used to write it to `config/.env`, where it
silently overrode detection for every repo. Set it only to debug detection.

---

## Step 3: target repository requirements

The agents diagnose from artifacts rather than by replaying the journey — signing
in again and rebuilding the test data is slow and often impossible. If your
framework does not produce these natively, **write a listener in the target repo
that does**. `Selenium-Automation-Framework`'s `AgentTelemetry.java` is a working
example of exactly this, wired into `TestListener.onTestFailure`.

### 1. DOM snapshot — MANDATORY
The complete HTML of the page at the moment of failure. This is what the agent
reads to find the replacement locator; without it, it is guessing.

- Write to `<results>/dom/<method>_<timestamp>.html`.
- First line must be the header `shared/dom_snapshot.py` parses:
  ```html
  <!-- qa-agent-network:dom-snapshot test="<method>" url="<url>" capturedAt="<iso8601>" -->
  ```
- Use `document.documentElement.outerHTML`, **not** the framework's
  "page source" accessor. Page source returns the document as originally served,
  so on any single-page app it shows markup that has not existed since load —
  precisely the wrong evidence for a locator that stopped matching.

### 2. Action timeline — MANDATORY unless your framework traces natively
One JSON object per line, appended as the test runs.

- Write to `<results>/telemetry/<method>_<timestamp>.jsonl` (or anything your
  `discover()` finds).
- One file per test method. A fresh file per action scatters the timeline and
  destroys the ordering, which is the whole value.
- Recognised field names: `action`/`command`/`event`, `selector`/`locator`/`target`,
  `url`, `value`/`text`, `error`/`exception`.
- Record successful actions too, and truncate values — an LLM reads this file, so
  never write a credential into it.

### 3. Parked repair mode / CDP port — OPTIONAL, high value
On failure in repair mode, leave the browser open and publish its CDP endpoint to
`<results>/.repair-session.json`. The agent attaches and can count how many
elements a candidate selector matches before any code is edited.

- Honour a `repairPort` property rather than hardcoding 9222. The agent derives a
  port per session; with a fixed one, only the first of N concurrent runs can
  ever park and the rest silently lose live repair.
- Launch the browser **detached**, or it dies with the JVM before anything can
  attach.

### 4. Conventions the agents read — needed for the full feature set
These are not plugin concerns; the agents read them straight from the target
repo, whatever the framework. Missing ones degrade a feature rather than break a run.

| What | Where in the target repo | Used by |
|------|--------------------------|---------|
| An agent guide (framework APIs, wrappers, naming) | `CLAUDE.md` at the repo root | Authoring (parse/generate/fix) and healing fix — the model's source of truth for the repo's own APIs |
| One `logStep("…")` per test step, stating action and expected outcome | test classes | Authoring's narration check (`shared/logstep_narration.py`), and adaptation's derived intent contracts |
| Assertions through a helper, with the message as the last argument (e.g. `AssertHelper.*`) | tests and helpers | `shared/assertion_graph.py` — what a test proves, and whether an edit weakened it |
| Per-environment properties `parameters/{environment}-{country}.properties` | `src/main/resources/` | Authoring writes URL and credential keys there instead of hard-coding them |
| Element fingerprint script `locator-capture.js` | `src/main/resources/` | The healing Locate engine (`shared/locator_capture.py`) |
| Page baselines, written on each successful page load (`baselineDir` in `config.properties`) | `src/main/resources/baselines/` by default | Diagnosis and Locate (`shared/baseline.py`); committed by the ship steps and `scripts/commit_baselines.py` |
| Authored intent contracts (optional) | `src/test/resources/intents/` | Adaptation (`shared/intent.py`); derived from source when absent |

`Playwright-Automation-Framework` has all of these and is the reference target.

### Degrading honestly
Missing artifacts are never fatal — the agents fall back to weaker evidence and
say so. What matters is that absence is visible rather than silently synthesised.
`shared/trace_network.py` is the model: it is Playwright-only, returns empty for
anything else, and documents that a non-Playwright run simply loses that channel.

---

## Step 4: verify

```bash
# Contract tests — parametrised over every registered plugin.
pytest tests/unit/test_frameworks.py

# Detection resolves your repo without an env var (prints (framework, source)).
python3 -c "from shared.frameworks import detect; print(detect.resolve('/path/to/repo'))"
```

Then run one real failing test in the target repo and confirm the artifacts round
trip:

```python
from shared import telemetry
found = telemetry.discover(results_dir, "yourTestMethod")
actions = telemetry.read_actions(found[0])
print(telemetry.format_for_prompt(actions))
```

`locator-eval/` is a Playwright-specific benchmark — it launches Chromium
directly and asserts on Playwright syntax, so a non-Playwright plugin fails it by
construction. Use it to check you have not regressed Playwright, not to grade a
new plugin.

---

## Known gaps

- **No agent has run end to end against a Selenium repo yet.** What is proven:
  the plugin contracts (`tests/unit/test_frameworks.py`, parametrised over both
  plugins), `@FindBy` emission, generated XPath validated against a real DOM,
  diagnostics against real Selenium exception text, and detection resolving
  `Selenium-Automation-Framework` from its `pom.xml`. Not yet proven:
  `detect_command` producing a working Maven invocation, `dom_snapshot.py` parsing
  a real snapshot, and the locator ladder editing a live `@FindBy` page object.
- In that repo `AgentTelemetry.recordAction` is only called from
  `onTestFailure`, so a run there produces a one-line timeline. It needs wiring
  into the interaction wrappers (work in the target repo, not here).
