# Framework Agnostic Architecture

**Status: implemented.** This document describes what the system does, and — where
the original plan turned out to be aimed wrongly — what was discarded and why.

## Goal

The agent network should work against any automation framework, not just
Playwright. A repo written in Selenium + TestNG should be authored, healed and
adapted as well as a Playwright one.

## The central distinction

The original plan treated "Playwright" as one coupling to be abstracted behind
five pillars. It is two different things wearing the same name, and separating
them is what made the work tractable:

| | **Convention** — the target repo's | **Instrument** — the agents' own |
|---|---|---|
| What | The syntax its tests are written in (`page.locator(...)` vs `@FindBy`), how they run, what artifacts they leave, what its errors mean | A live browser the agents open to read a DOM, score candidate locators and verify a fix |
| Pluggable? | Yes — this is the whole job | **No. Always Playwright** |
| Where | `shared/frameworks/*_plugin.py` | `shared/mcp_config.py`, `shared/locator_verify.py`, `shared/locator_candidates.py`, `shared/locator_resolve.py` |

The instrument speaks CDP, which is a browser-level protocol — a browser does not
know or care which framework drove it there. Selenium 4 exposes the same DevTools
port, so a Selenium-launched browser is inspected with the Playwright MCP server.

The original plan reached this conclusion and then mislabelled it, describing the
Selenium plugin's use of `@playwright/mcp` as a regrettable *"Selenium MCP
Fallback Strategy"*. It is the correct design, not a fallback. Naming it properly
removed an interface and a large amount of would-be migration work.

## What exists

Four contracts in `shared/frameworks/base.py`, implemented by `PlaywrightPlugin`
and `SeleniumPlugin`:

- **`TelemetryParser`** — `discover()` finds the framework's artifacts,
  `read_actions()` parses them into one shared action schema
  (`ACTION_KEYS`), `failing_action()` picks the one that broke.
- **`TestRunner`** — `detect_command()`, `apply_browser_mode()`.
- **`DiagnosticEngine`** — `is_ambiguous_locator()`,
  `is_locator_resolution_failure()`.
- **`CodeEngine`** — `extract_locators()`, `normalize_selector()`,
  `emit_locator()`, `build_has_text_selector()`, `map_role()`, plus an optional
  `findby` output for frameworks that declare locators as page-object fields.

Selection is in `shared/frameworks/detect.py` and `__init__.py`.
`docs/FRAMEWORK_INTEGRATION.md` is the guide for adding a framework.

## Decisions

**Framework is derived from the repository, not configured.** `detect.py` reads
the repo's build files (`pom.xml`, `package.json`, …), with `repo-map.json` as a
secondary source and `AUTOMATION_FRAMEWORK` as an explicit override that warns
loudly when it contradicts what the repo contains. A repo either is or is not a
Playwright repo, so a setting can only agree or be wrong.

*It was wrong.* `config/.env` said `selenium` while the target repo was
Playwright-Java. Locator extraction returned `[]` for every page object, every
trace was rejected, and `AMBIGUOUS_LOCATOR` could never fire — silently, with no
error anywhere. Deriving it deleted that entire class of bug, and with it four
planned work items: the UI dropdown, per-run payload plumbing, the process-global
plugin singleton, and the "one framework per server" limitation. Framework became
per-run for free, because `FRAMEWORK_DIR` already points at each run's own
worktree.

**No `MCPProvider`.** Discarded — see the table above. Both implementations
returned the same server and the same allowed-tools list. One implementation now
lives in `shared/mcp_config.py`.

**The instrument stays Playwright, deliberately.** `locator_verify.py`,
`locator_candidates.py` and `locator_resolve.py` use `sync_playwright` directly
and should keep doing so. They are marked as such so the next reader does not
"fix" them.

**Existing conventions first.** The plugin exposes the target repo's own idiom to
the prompt. The Selenium repo uses `@FindBy` PageFactory exclusively, so
`emit_locator` emits `@FindBy` for it — emitting inline `driver.findElement`
would have contradicted the repo on every fix.

## Things that were reported done and were not

Recorded because each one *looked* complete in review:

- **The headless env rename.** `agent_settings.py` still wrote
  `PLAYWRIGHT_HEADLESS` while `browser_mode.py` read `HEADLESS_BROWSER`, and
  `config/.env` had both with opposite values — so the admin toggle did nothing
  *and displayed the inverse of reality*. Likewise `AUTHORING_PLAYWRIGHT_TIMEOUT_MS`
  was written by the UI and read by nobody, silently reverting the timeout to 30s.
- **Prompt generalisation.** `config/prompts/authoring.md` was edited to say "the
  target framework's native locator syntax". Nothing loaded that file. The live
  prompt — an f-string in `03_generate.py` — still said "using `page.locator()`".
  The file is gone; the live prompt now injects the syntax from the CodeEngine.
  `config/prompts/README.md` records the invariant that every file there must
  have a loader.
- **Plugin routing.** `locator_emit.synthesize()` was fully plugin-routed and
  called by nothing; the live path (`emit()` → `candidates_for()`) was hardcoded
  Playwright. The CodeEngine had almost no effect on emitted locators.
  `candidates_for` is now routed and `synthesize` is deleted. Same pattern in
  `test_runner._apply_browser_mode`, a private duplicate that shadowed the
  plugin's method.
- **Selenium telemetry.** Every discovery site globbed `traces/*.zip` while the
  Selenium parser accepted only `.jsonl` — it could never be handed a path it
  would take. Fixed by putting `discover()` in the contract.

The common thread: **a plugin-routed function that nothing calls looks identical
to a completed migration in review.** `tests/unit/test_frameworks.py` exists
because none of the above had a test that would have failed.

## Verification

- `pytest tests/unit/test_frameworks.py` — contracts, parametrised over every
  registered plugin; detection against both real target repos.
- `pytest tests/unit/` — 1240 tests. `tests/conftest.py` pins the framework so
  the suite no longer depends on the developer's environment.
- `locator-eval/` is a Playwright-specific benchmark, useful for checking
  Playwright has not regressed, not for grading a new plugin.

## Still open

**No agent has ever run against a Selenium repo.** This is the largest gap and
worth stating plainly, because everything else about Selenium is verified and
that can read as more than it is. What IS proven: the plugin contracts
(`tests/unit/test_frameworks.py`, parametrised over both plugins), `@FindBy`
emission, generated XPath validated against a real DOM, diagnostics against real
Selenium exception text, detection resolving `Selenium-Automation-Framework` from
its `pom.xml`, and a Java→Python telemetry round trip using output from a
compiled `AgentTelemetry`. What is NOT: `detect_command` producing a working
Maven invocation, `dom_snapshot.py` finding and parsing a real snapshot, and the
locator ladder emitting into a live `@FindBy` page object during an actual fix.

Related, and blocking a meaningful Selenium run: `AgentTelemetry.recordAction`
is currently called only from `onTestFailure`, so a real run there would produce
a one-line timeline rather than the "which selectors worked before the one that
did not" that makes it useful. It needs wiring into that repo's interaction
wrappers — work in the target repository, not here.

Smaller items:

- The healing agent's `00_reproduce.py` classifies errors from a hardcoded list
  mixing both frameworks' strings; it should go through `DiagnosticEngine`.
- Several inline prompts in `01_fix.py` and `02_validate_web.py` still describe
  Playwright behaviour in prose.
- `shared/page_identity.py` retains ~110 lines of Playwright constants left
  behind when its functions were delegated. Dead, not harmful.
