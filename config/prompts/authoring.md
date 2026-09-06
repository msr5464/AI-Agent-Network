# Authoring Prompt — test-authoring-agent

Template for `agents/test-authoring-agent/actions/03_generate.py`.
The "Rules (MANDATORY)" section below is loaded at runtime; dynamic context (plan, selectors, references) is injected by Python.

---

## Rules (MANDATORY — violations will cause compilation failures)

1. Every file must compile standalone — include all necessary imports.
2. Data POJO: use `@Data @NoArgsConstructor @AllArgsConstructor @JsonInclude(NON_NULL)`.
   Each field needs `@JsonProperty("snake_case_key")`.
3. Builder: fluent `with*()` methods returning `this`. `withDefaults()` sets null fields.
   `build()` calls `withDefaults()` then constructs the POJO.
4. API enum: implements `ApiDetails`. Include `withPath(String param, String value)` method.
5. Helper: extends `ApiHelper` (import `automation.core.api.ApiHelper`). Pass `customBaseUrl` to `super(config, BASE_URL)`.
   API methods call `execute()`/`executeAndVerify()`/`executeRaw()`.
   For token auth: call `setAuthToken(token)` on the helper after construction.
   Web methods only if they orchestrate 2+ page objects.
6. Page objects: extend `BasePage`. Define all locators using the target framework's native locator syntax.
   Call `waitUntilLoaded()` LAST in constructor. `waitUntilLoaded()` uses `WaitHelper`.
   All interactions use `BasePage` methods (`click`, `fillText`, `getText`, `isElementDisplayed`).
   Navigation methods return the next page object.
7. Test classes: extend `TestBase`. Use `@Test(dataProvider="getConfig", groups={...})`.
   Every `@Test` method has `@TestVariables(automatedBy = QA.Mukesh, country = Country.{country})`.
   Use `allocateUser(config, UserType.{user_type}, Feature.{feature_enum}, Country.{country})`.
   Use `config.logStep()` in test methods only.
7b. Step narration — one `logStep` per step, never one summary line. The run report
   prints one line per `logStep`, so a test narrated once produces a one-line report
   for the whole scenario and a failure cannot be traced to a step; the intent
   contract is derived from the same strings, so a run-on sentence collapses several
   checkable claims into one blob.
   - Every step in the method's `steps` list in the plan gets its own
     `config.logStep("<action AND expected outcome>")`, immediately before the
     call(s) that carry it out, with a blank line between step groups.
   - Setup lines (reading properties/credentials, constructing the helper) get none.
   - A helper may encapsulate ONE step, never the whole scenario: if one call would
     cover several plan steps, split it so the test method shows the flow.
   Enforced after generation by `_repair_step_narration()` in `03_generate.py`
   (`shared/logstep_narration.py` does the analysis); what survives the repair is
   recorded in `03-generate.json` under `under_narrated_tests`.
8. Locators: prefer `[data-cy='...']` > `[id='...']` > `[name='...']` > CSS > XPath.
9. Assertions: ONLY `AssertHelper.*` — never `Assert.*`.
10. Waits: ONLY `WaitHelper.*` — never `Thread.sleep()`.
11. For existing modules: only generate new test class + new helper methods.
    Do NOT regenerate existing Data/Builder/Api files.
12. URLs: never a literal `"http://..."` / `"https://..."` in Java — not in a test, a page
    object, a helper, or a `private static final String BASE_URL` constant. Step 03 writes
    every URL the plan and the browser validation saw into
    `parameters/{environment}-{country}.properties` before generating, and injects the keys
    into the prompt. Read them back:
    - ApiHelper base URL: `super(config, config.getRunTimeProperty("{feature}.api.url"))` —
      inline in the `super()` call; an instance field cannot be referenced there.
    - Navigation: `BrowserHelper.navigateTo(config, config.getRunTimeProperty("{feature}.login.url"))`
    - Reused in a class: `private final String profileUrl = config.getRunTimeProperty(...)` —
      an instance field, since `static` cannot reach `config`.
    A literal that survives generation triggers one targeted repair pass; whatever is left
    is recorded in `03-generate.json` under `hardcoded_urls`. Step 04 rejects any fix that
    adds one.

---

## Output Format (strict)

Return ONLY a JSON object where keys are relative file paths and values are complete file contents.
No prose outside the JSON object.

```json
{
  "src/main/java/automation/modules/{feature}/{Feature}Data.java": "...full file content...",
  "src/main/java/automation/modules/{feature}/api/{Feature}Api.java": "...full file content...",
  "src/test/java/automation/{feature}/{Feature}ApiTest.java": "...full file content..."
}
```
