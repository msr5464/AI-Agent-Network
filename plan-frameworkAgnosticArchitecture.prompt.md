## Plan: Framework Agnostic Architecture via Plugin Suite

**TL;DR** 
The current QA Agent Network is tightly coupled to Playwright conventions (trace parsing, MCP tools, runner arguments, locator strictness rules, and code generation syntax). To support multiple automation frameworks (like Selenium), we will adopt **Option B: Adapter / Plugin Architecture**. However, fresh eyes analysis reveals a standard runtime adapter is insufficient. We must build a **Framework Plugin Suite** because the coupling exists across 5 dimensions: Runtime, Telemetry, Diagnostics, Code Synthesis, and LLM MCP integration.

### Analysis & Assessment
1. **Current architecture assessment**: Four agents share a common `shared/` library. The system implicitly assumes Playwright at multiple levels (traces, locators, page modes, test runners, headless toggles, code parsers, and the `@playwright/mcp` server). 
2. **Hidden Framework-coupling analysis**: 
    - **Diagnostics**: Hardcoded rules for Playwright's "strict mode violation".
    - **Telemetry**: Depends on Playwright `.zip` traces containing `trace.trace` event streams.
    - **Code Parsing/Synthesis**: Explicit regex for `page.locator(...)`, `getByRole`, and Playwright pseudo-classes (`:has-text`).
    - **Agent Control (MCP)**: Relies on `@playwright/mcp` for the LLM to drive the browser.
3. **Feasibility assessment**: High feasibility, but requires a wider interface than initially planned. The Plugin must handle code synthesis and diagnostic rules, not just test execution.
4. **Recommended architecture**: **Option B (Core + Framework Plugin Suite)**. We define a standard internal API for common capabilities (`TelemetryParser`, `TestRunner`, `DiagnosticEngine`, `CodeEngine`, `MCPProvider`). A `PlaywrightPlugin` provides the concrete implementations. 
5. **Alternative approaches considered**: 
    - *Option A (Configuration-driven)*: Insufficient. Frameworks have different trace artifacts, DOM semantics, and different execution commands.
    - *Option C (Framework-specific agents)*: Rejected due to immense duplication of prompt logic and AI agent workflows.

---

**Steps**
1. **Define the Plugin Interfaces (Contracts)**
   - Create a new `shared/frameworks/` module to house framework contracts.
   - Define interfaces for the 5 pillars:
     - `TelemetryParser` (reads traces/logs/screenshots)
     - `TestRunner` (executes tests, handles CLI args)
     - `DiagnosticEngine` (interprets framework-specific errors like strict-mode vs NoSuchElement)
     - `CodeEngine` (AST/Regex for parsing and generating locators)
     - `MCPProvider` (provides the correct MCP server config for Claude)
2. **Implement Playwright Plugin (Reference Implementation)**
   - Create `shared/frameworks/playwright/` containing implementations for the defined contracts.
   - Move existing Playwright-specific logic (from `playwright_trace.py`, `page_identity.py`, `dom_snapshot.py`, `mcp_config.py`, `browser_mode.py`, `test_runner.py`) into this plugin.
3. **Refactor Core Shared Helpers to Use Plugins**
   - Update `shared/test_runner.py` and `shared/diagnosis.py` to route requests to the active plugin.
   - Generalize the `PLAYWRIGHT_HEADLESS` logic to a generic `FRAMEWORK_HEADLESS` state.
4. **Refactor Agents** *(Parallel with Step 3)*
   - Update `test-authoring-agent`, `test-healing-agent`, and others to load the framework plugin via environment variable (e.g. `AUTOMATION_FRAMEWORK=playwright`), and use plugin methods.
5. **Update Prompts and Instructions**
   - Generalize prompts in `config/prompts/` to refer to the "automation framework" rather than "Playwright" directly. Inject framework-specific conventions, wrappers, and code syntax via the `CodeEngine`.
6. **Establish New Framework Integration Workflow**
   - Document the mandatory and optional interfaces a new framework plugin (like Selenium) must implement.

**Relevant files**
- `/shared/frameworks/base.py` — *New*: Defines the core contracts (TelemetryParser, TestRunner, DiagnosticEngine, CodeEngine, MCPProvider).
- `/shared/frameworks/playwright_plugin.py` — *New*: The concrete implementation migrating Playwright code.
- `/shared/playwright_trace.py` — Migrate to `TelemetryParser` implementation.
- `/shared/test_runner.py` — Update to delegate test execution to the active plugin.
- `/shared/diagnosis.py` — Migrate strict-mode rules to `DiagnosticEngine`.
- `/shared/page_identity.py` / `/shared/locator_emit.py` — Migrate regex/generation to `CodeEngine`.
- `/agents/*/actions/*.py` — Replace direct Playwright/MCP calls with plugin delegates.

**Verification**
1. **Unit Tests Validation**: Run the existing `pytest` suite in `/tests/unit/` to ensure no functionality is broken (specifically `test_browser_mode.py`, `test_diagnosis.py`, `test_page_identity.py`).
2. **End-to-End Run**: Execute a `test-healing-agent` run using the Playwright plugin and verify it still successfully diagnoses, fixes, and verifies a broken locator in the Jarvis repo.
3. **Mock Plugin Check**: Create a dummy plugin (e.g. `SeleniumMockPlugin`), inject it, and ensure the agents fail cleanly at the plugin boundaries or correctly generate non-Playwright code in dry-runs.
4. **Adapter Capability & Quality Benchmarking**: Use the existing `locator-eval/` suite to benchmark new plugins (like Selenium) against the Playwright baselines. This ensures objective measurement of agent confidence and fix quality across frameworks.
5. **Strict Artifact Contracts**: Plugins must guarantee standard contextual outputs (e.g., DOM snapshots at failure, action timelines). If a framework (like Selenium) lacks native traces, the plugin contract defines what custom listeners/wrappers are required in the target repository to achieve parity.

### Additions During Implementation (Post-Plan Discoveries)
The following steps were executed during implementation to achieve full agnosticism and went beyond the initial plan:

1. **Implemented the Selenium Plugin**: Despite the initial scope limitation, the `SeleniumPlugin` was fully implemented (`shared/frameworks/selenium_plugin.py`) as the primary proof of the architecture. It handles `driver.findElement` syntax, parses JSONL logs for telemetry instead of `.zip` traces, and translates text matching to XPath.
2. **Selenium MCP Fallback Strategy**: Discovered there is no robust open-source Selenium MCP. The `SeleniumMCPProvider` was built to fall back to the `@playwright/mcp` server. By mandating Selenium 4 in the target repository, we can expose the underlying Chrome DevTools Protocol (CDP) port. Because CDP is a browser-level protocol, Playwright's MCP can successfully attach to a browser launched by Selenium, allowing live DOM inspection to work natively.
3. **Global Headless Environment Variable Rename**: The system was globally hardcoded to `PLAYWRIGHT_HEADLESS`. This was renamed to `HEADLESS_BROWSER` across all shell scripts, python code, tests, and API settings (`qa_agents_server/agent_settings.py`) to prevent framework-specific configuration leakage. `AUTHORING_PLAYWRIGHT_TIMEOUT_MS` was also renamed to `AUTHORING_BROWSER_TIMEOUT_MS`.
4. **Generalizing Agent Prompts**: Hardcoded Playwright syntax instructions in `config/prompts/authoring.md` (e.g., "use `page.locator()`") were removed and replaced with dynamic instructions to use the target framework's native syntax. 
5. **Generalizing Edit Guards**: The safety mechanisms in `shared/edit_guards.py` were refactored to check for generic "ambiguous locator errors" instead of explicitly searching for Playwright's "strict mode violation" string.
6. **Framework Integration Guide Creation**: To solve the onboarding problem for new frameworks, a detailed `docs/FRAMEWORK_INTEGRATION.md` guide was authored. It explicitly defines the mandatory artifacts (DOM snapshots, action telemetry JSONL, and optional CDP port) that a target repository must generate to integrate successfully with the agents.

**Decisions**
- **Existing Conventions First**: The plugin must expose the target repository's existing base classes, wrappers, and structures to the AI prompt. The AI will strictly reuse existing framework conventions.
- **Framework Discovery**: The active framework will initially be set via an environment variable (`AUTOMATION_FRAMEWORK`), defaulting to `playwright` for backward compatibility.
- **Scope limitation**: We are NOT building the second framework plugin yet. We are building the architecture and the *Playwright Reference Plugin* as proof of concept. *(Update: Overruled. Selenium Plugin was built and proven).*
- **AI Test Studio UI Integration**: The framework configuration must be passed from the AI Test Studio UI when triggering an agent run (e.g., via the API in `qa_agents_server/routes.py`). The UI layer will eventually need a dropdown/configuration option to specify the target framework.
- **Selenium MCP Requirement**: A key finding is that Playwright agents use an MCP server (`@playwright/mcp`) for live browser exploration. For a future Selenium plugin to achieve parity, a "Selenium MCP Server" will need to be developed or sourced. *(Update: Handled via the Selenium 4 CDP Fallback Strategy).*