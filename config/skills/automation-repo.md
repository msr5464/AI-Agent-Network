# Automation Repository — Skills & Conventions

This file is loaded as a system prompt (`--system-prompt-file`) by the
test-healing-agent's fix step and the test-adaptation-agent's web exploration.
It holds the rules that hold for any automation repo these agents work on. It
deliberately names no framework API: the automation repo's own `CLAUDE.md`, and
the page objects and tests you are shown, are the source of truth for class
names, method names and signatures. Copy the patterns you see there; never
invent an API.

---

## Framework shape

- **Language / runner**: Java, TestNG, built and run with Maven.
- **Browser automation**: whatever the repo uses (Playwright or Selenium), always
  behind the repo's own wrapper classes.
- **Page objects**: one class per page, extending the repo's base page class.
  Navigation methods return the next page object.
- **Helpers**: orchestrate several page objects; tests call helpers and page
  objects, then assert.

---

## Rule 1: never call the browser driver directly — use the repo's wrappers

Every click, fill, read and wait goes through the repo's wrapper methods, which
log, wait and screenshot consistently. Calling the driver or locator object
directly is wrong in every framework:

```java
// WRONG — raw driver / locator calls
locator.click();                 // Playwright
element.sendKeys("text");        // Selenium
driver.findElement(By.id("x"));  // Selenium
```

Use the equivalent wrapper call exactly as the surrounding code does.

## Rule 2: never use `Thread.sleep` — use the repo's wait helper

Wait on a condition (visible, hidden, page loaded) through the wait helper the
repo already uses. A fixed sleep is always wrong.

## Rule 3: assertions go through the assertion helper

Use the repo's assertion helper (e.g. `AssertHelper`), never `Assert.*` or a bare
`assertTrue`. Keep the existing assertion's strength: never replace an equality
check with a weaker one (`contains`, `isTrue`) to make a test pass.

---

## Locator strategy — priority order

1. A test id attribute — `[data-cy='…']`, `[data-testid='…']`, `[data-test='…']`
2. `#id` — a stable, human-meaningful id
3. `[name='…']`
4. CSS on meaningful class names (not generated hashes)
5. XPath — only when nothing above works; use `contains()`, never exact text

**Never use:**
- Positional selectors: `//div[1]/span[2]`, `:nth-child(3)` without an anchor
- Auto-generated class names (hash-like strings)
- Exact-text XPath: `//button[text()='Submit']` — use `contains()`
- Escaped double quotes inside a selector string: write `[data-cy='x']` and
  `button:has-text('Login')`, not `has-text(\"Login\")`. Use double quotes only
  when the value itself contains an apostrophe.

Declare a locator the way the page object already declares its others (inline
field initialisation, `@FindBy`, …) — match the file, do not introduce a second
style.

---

## Logging

- **Test classes**: one `logStep` call per step of the scenario, in the form the
  repo uses (e.g. `config.logStep("…")`).
- **Page objects and helpers**: the repo's comment-level logger
  (e.g. `Log.comment(config, "…")`) — never `logStep`.
- Never `System.out.println`.
- **One `logStep` per step, never one summary line.** The report prints one line
  per `logStep`, so a scenario narrated once fails with a report that cannot say
  which step broke. Each `logStep` goes immediately before the call(s) it
  describes:

```java
// WRONG — one line for a four-step scenario
config.logStep("Login, toggle the trailing dot in the summary, save, and verify it persists");
String[] result = helper.toggleProfileSummaryDot(username, password);

// RIGHT — one line per step, each in front of the calls that carry it out
config.logStep("Login and open the profile page");
ProfilePage profile = helper.loginAndOpenProfile(username, password);

config.logStep("Toggle the trailing dot in Profile Summary and save the change");
String saved = profile.toggleTrailingDotAndSave();

config.logStep("Verify the summary shown after reload matches the saved value");
AssertHelper.assertEquals(config, profile.reload().getProfileSummary(), saved,
    "Profile Summary after reload should match the saved modified summary");
```

(Illustrative names — use the repo's real classes and methods.)

Setup lines (reading properties or credentials, constructing a helper) get no
`logStep`. A helper may encapsulate one step; it must not swallow the whole
scenario, because then there is nothing left for the test to narrate.

---

## Running a single test (for verification)

```bash
mvn test -Dtest=ClassName#methodName
```

The agents pass the repo's own environment properties and browser mode; do not
add flags of your own.

---

## Edits

- Change the minimum: a locator fix edits the locator string, nothing else.
- Keep imports to what the file already uses; never import raw driver classes
  (`org.openqa.selenium.By`, `WebDriverWait`, …) to work around a wrapper.
- Never hard-code a URL or credential in Java — they live in the repo's
  properties files.
