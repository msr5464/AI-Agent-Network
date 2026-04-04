# qa-auto-create — Master Context

Read this file first. Every time. Before doing anything else.

## What This Agent Does

Takes plain English test steps from a `.txt` file in the queue, generates complete
framework-compliant Java test code for the Thanos-pw automation repository, validates
generated web flows with a headless Playwright script, runs the generated test via Maven,
fixes any failures iteratively, and raises a GitHub PR.

Runs independently. One session = one feature input file = one PR (or Slack alert if tests fail).

---

## Architecture

```
run.sh (orchestrator)
  │
  ├─ 01_parse.py          [Python + Claude]   Plain text → structured generation plan
  ├─ 02_validate_web.py   [Python + Claude]   Generate + run headless Playwright Node.js script → selector map
  ├─ 03_generate.py       [Python + Claude]   Write Java files to Thanos-pw repo
  ├─ 04_run_and_fix.py    [Python + Claude]   Run mvn test → fix failures → retry loop
  └─ 05_ship.py           [Python only]       Git branch + commit + push + gh pr create
```

---

## Step Responsibilities

| Step | Owns | Does NOT do |
|------|------|-------------|
| **01 Parse** | Read plain text, call Claude, produce plan JSON | No file writes to Thanos-pw |
| **02 Validate Web** | Generate + run Node.js Playwright script, collect selectors | No Java codegen |
| **03 Generate** | Write all Java files to Thanos-pw | No test running |
| **04 Run+Fix** | Run mvn test, call Claude to fix failures, retry | No git push |
| **05 Ship** | Branch + commit + push + PR creation | No AI calls |

---

## Data Flow

```
queue/<feature>.txt  (plain English test steps)
    ↓
01-parse.json            (structured generation plan: classes, fields, methods)
    ↓
02-validate-web.json     (confirmed DOM selectors, or empty if not a web test)
    ↓
03-generate.json         (list of Java files written to Thanos-pw)
    ↓
04-run-and-fix.json      (test run results, applied fixes)
.fix-passed              (gate: true / false / skipped)
    ↓
05-ship.json             (PR URL, Slack status)
.verdict                 (APPROVED / NEEDS-REVIEW)
    ↓
queue/processed/<feature>.txt  (moved after completion)
```

---

## Input File Format

Plain text file at `queue/<feature>.txt`. Claude in step 01 is flexible about exact format.
The minimum required information:

```
Feature: payments
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

**.verdict**
- `APPROVED`      — test passed, PR created
- `NEEDS-REVIEW`  — test still failing, PR created with warning

---

## Audit Trail

**Session folder:** `agents/qa-auto-create/audit/$SESSION_ID/`

| File | Written by | Purpose |
|------|-----------|---------|
| `00-session-init.md` | run.sh | Session metadata, env snapshot |
| `01-parse.json` + `.md` | Parse | Generation plan |
| `02-validate-web.json` + `.md` | Validate Web | Selector map, step results |
| `02-validate-web.js` | Validate Web | The generated Playwright script |
| `03-generate.json` + `.md` | Generate | List of files written |
| `04-run-and-fix.json` + `.md` | Run+Fix | Test output, applied fixes |
| `.fix-passed` | Run+Fix | Gate: true / false / skipped |
| `05-ship.json` + `.md` | Ship | PR URL, Slack status |
| `.verdict` | Ship | APPROVED / NEEDS-REVIEW |

---

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAUDE_CLI_PATH` | Path to claude CLI binary | `claude` |
| `AUTOCREATE_MODEL` | Claude model for all AI steps | `claude-opus-4-6` |
| `WORKSPACE_DIR` | Parent directory containing Thanos-pw | required |
| `GITHUB_TOKEN` | GitHub auth token for PR creation | required |
| `GITHUB_ORG` | GitHub org/user owning the repo | required |
| `GITHUB_REPO_AUTOMATION` | Name of the Thanos-pw repo dir | `Thanos-pw` |
| `GITHUB_DEFAULT_BRANCH` | Base branch for PRs | `main` |
| `GITHUB_PR_REVIEWERS` | Comma-separated reviewer handles | optional |
| `AUTOCREATE_BRANCH_PREFIX` | Branch name prefix | `feat/qa-autocreate` |
| `MAX_FIX_ATTEMPTS` | Max retry cycles for failing tests | `3` |
| `AUTO_PUSH` | Set `false` to skip PR creation (dry-run) | `true` |
| `AUTOCREATE_ENVIRONMENT` | Maven `-Denvironment=` value | `staging` |
| `AUTOCREATE_COUNTRY` | Maven `-Dcountry=` value | `SG` |
| `PLAYWRIGHT_TIMEOUT_MS` | Timeout for Playwright validation steps | `30000` |
| `SLACK_BOT_TOKEN` | Slack bot token | optional |
| `SLACK_NOTIFY_CHANNEL` | Slack channel for success notifications | optional |
| `SLACK_ALERT_CHANNEL` | Slack channel for failure alerts | optional |
| `SESSION_ID`, `AUDIT_DIR`, `INPUT_FILE`, `FEATURE` | Set by run.sh — do not set manually | — |

---

## How to Run

```bash
# Direct mode — process a specific feature input file
make run AGENT=qa-auto-create FEATURE=payments

# Queue mode — picks the oldest .txt in the queue
make run AGENT=qa-auto-create

# Dry-run — generates, tests, but no PR pushed
AUTO_PUSH=false make run AGENT=qa-auto-create FEATURE=payments

# View audit trail
make audit AGENT=qa-auto-create
make audit AGENT=qa-auto-create SESSION=20260330-143022-create-payments
```

---

## Thanos-pw Framework Conventions

**The following rules are MANDATORY for all generated Java code. Claude must follow these
exactly — any deviation will cause test compilation or runtime failures.**

### Package Structure (new module)
```
src/main/java/automation/modules/{feature}/
  {Feature}Data.java
  {Feature}Builder.java
  {Feature}Helper.java          extends AuthHelper
  api/{Feature}Api.java         enum implements ApiDetails
  web/{Page}Page.java           extends BasePage

src/test/java/automation/{feature}/
  {Feature}ApiTest.java         extends TestBase
  {Feature}WebTest.java         extends TestBase
```

### Data POJO
```java
@Data @NoArgsConstructor @AllArgsConstructor
@JsonInclude(JsonInclude.Include.NON_NULL)
public class PaymentData {
    @JsonProperty("recipient_id") private String recipientId;
    @JsonProperty("amount")       private String amount;
    @JsonProperty("currency")     private String currency;
    // response-only (set by server, not sent in request body):
    @JsonProperty("id")           private String id;
    @JsonProperty("status")       private String status;
}
```
- All field names map JSON `snake_case` → Java `camelCase` via `@JsonProperty`
- `@JsonInclude(NON_NULL)` omits null fields from request bodies
- Response-only fields (id, status, createdAt) must still be on the POJO but not set in Builder

### Builder
```java
public class PaymentBuilder {
    private String recipientId;
    private String amount;
    private String currency = "SGD";   // default value

    public PaymentBuilder withRecipientId(String id)  { this.recipientId = id; return this; }
    public PaymentBuilder withAmount(String amount)    { this.amount = amount;  return this; }
    public PaymentBuilder withCurrency(String cur)     { this.currency = cur;   return this; }

    public PaymentBuilder withDefaults() {
        if (recipientId == null) recipientId = DataGenerator.randomUUID();
        if (amount == null)      amount = "100";
        return this;
    }

    public PaymentData build() {
        withDefaults();
        PaymentData p = new PaymentData();
        p.setRecipientId(recipientId);
        p.setAmount(amount);
        p.setCurrency(currency);
        return p;
    }
}
```

### API Enum
```java
public enum PaymentApi implements ApiDetails {
    CreatePayment(Method.POST,   "/v1/payments",       201),
    GetPayment(   Method.GET,    "/v1/payments/{id}",  200),
    ListPayments( Method.GET,    "/v1/payments",       200),
    DeletePayment(Method.DELETE, "/v1/payments/{id}",  200);

    private final Method method;
    private final String endpoint;
    private final int expectedStatus;

    PaymentApi(Method method, String endpoint, int expectedStatus) {
        this.method = method; this.endpoint = endpoint; this.expectedStatus = expectedStatus;
    }

    @Override public Method getMethod()       { return method; }
    @Override public String getEndpoint()     { return endpoint; }
    @Override public int getExpectedStatus()  { return expectedStatus; }

    public ApiDetails withPath(String param, String value) {
        String resolved = this.endpoint.replace("{" + param + "}", value);
        final Method m = this.method; final int s = this.expectedStatus;
        return new ApiDetails() {
            @Override public Method getMethod()      { return m; }
            @Override public String getEndpoint()    { return resolved; }
            @Override public int getExpectedStatus() { return s; }
        };
    }
}
```

### Helper
```java
public class PaymentHelper extends AuthHelper {
    public PaymentHelper(Config config) { super(config); }

    // API workflows
    public PaymentData createPayment(PaymentData payment) {
        PaymentData created = executeAndVerify(PaymentApi.CreatePayment, payment, payment);
        AssertHelper.assertNotNull(config, created.getId(), "Payment ID generated");
        return created;
    }
    public PaymentData getPayment(String paymentId) {
        return execute(PaymentApi.GetPayment.withPath("id", paymentId), null, PaymentData.class);
    }

    // Web workflows (only if orchestrating 2+ page objects)
    public PaymentFormPage createPaymentViaUI(DashboardPage dashboard, PaymentData payment) {
        PaymentListPage list = dashboard.navigateToPayments();
        PaymentFormPage form = list.clickNewPayment();
        form.createPayment(payment);
        return form;
    }
}
```

### Page Object
```java
public class PaymentListPage extends BasePage {
    private final Locator newPaymentButton;
    private final Locator paymentList;

    public PaymentListPage(Config config) {
        super(config);
        newPaymentButton = page.locator("[data-cy='new-payment-btn']");
        paymentList      = page.locator("[data-cy='payment-list']");
        waitUntilLoaded();   // ONLY in constructor
    }

    @Override protected void waitUntilLoaded() {
        WaitHelper.waitForElementToBeVisible(config, paymentList, "Payment list");
    }

    public PaymentFormPage clickNewPayment() {
        click(newPaymentButton, "New Payment button");
        return new PaymentFormPage(config);   // always return next page
    }

    public boolean isPaymentVisible(String reference) {
        Locator row = page.locator("[data-cy='payment-row']:has-text('" + reference + "')");
        return isElementDisplayed(row);
    }
}
```

### API Test Class
```java
package automation.payments;

import org.testng.annotations.Test;
import automation.core.*;
import automation.core.Enums.*;
import automation.modules.payments.*;
import automation.modules.payments.api.PaymentApi;

public class PaymentApiTest extends TestBase {

    @Test(description = "Create a payment and verify it is returned by GET",
          dataProvider = "getConfig", groups = {GROUP_REGRESSION, GROUP_API})
    @TestVariables(automatedBy = QA.Mukesh, country = Country.SG)
    public void createAndVerifyPayment(Config config) {
        User user = allocateUser(config, UserType.Admin, Feature.CARD, Country.SG);

        PaymentHelper payments = new PaymentHelper(config);
        payments.loginAndSetAuth(user);

        config.logStep("Create a payment of 100 SGD and verify the ID is returned");
        PaymentData payment = new PaymentBuilder().withAmount("100").withCurrency("SGD").build();
        PaymentData created = payments.createPayment(payment);

        config.logStep("Fetch the payment by ID and verify status is PENDING");
        PaymentData fetched = payments.getPayment(created.getId());
        AssertHelper.assertEquals(config, fetched.getStatus(), "PENDING", "Payment status is PENDING");
    }
}
```

### Web Test Class
```java
package automation.payments;

import org.testng.annotations.Test;
import automation.core.*;
import automation.core.Enums.*;
import automation.modules.payments.*;
import automation.modules.payments.web.*;
import automation.modules.access.web.DashboardPage;

public class PaymentWebTest extends TestBase {

    @Test(description = "Create a payment via UI and verify success message",
          dataProvider = "getConfig", groups = {GROUP_REGRESSION, GROUP_WEB})
    @TestVariables(testrailData = "1:C0001:WEB", automatedBy = QA.Mukesh, country = Country.SG)
    public void createPaymentViaUI(Config config) {
        User user = allocateUser(config, UserType.Admin, Feature.CARD, Country.SG);

        PaymentData payment = new PaymentBuilder().withAmount("100").withCurrency("SGD").build();

        PaymentHelper payments = new PaymentHelper(config);
        DashboardPage dashboard = payments.doLogin(user);

        config.logStep("Navigate to Payments, click New Payment, fill form and submit");
        PaymentFormPage form = payments.createPaymentViaUI(dashboard, payment);

        config.logStep("Verify success message is displayed");
        AssertHelper.assertTrue(config, form.isSuccessMessageVisible(), "Success message should appear");
    }
}
```

---

## Critical Rules (enforced during code review)

**DO:**
- Extend `TestBase` in all test classes
- Use `@TestVariables` on every `@Test` method
- Use `dataProvider = "getConfig"` or `"getTwoConfigs"`
- Allocate users via `allocateUser(config, UserType, Feature, Country)` — never hardcode
- Use `[data-cy='...']` as primary locator strategy
- Use `BasePage` methods in page objects (`click`, `fillText`, `getText`)
- Use `AssertHelper` for all assertions
- Call `waitUntilLoaded()` in page constructors only
- Use `WaitHelper` everywhere else — never `Thread.sleep()`
- Return the next page object from every navigation method
- Use `config.logStep()` in test classes, `config.logComment()` in helpers/pages
- Use `@JsonInclude(NON_NULL)` and `@JsonProperty` on all Data POJOs

**DON'T:**
- Hardcode credentials — use `allocateUser()`
- Call Playwright locator methods directly — use `BasePage`/`Element` wrappers
- Use `Assert.*` — use `AssertHelper.*`
- Use `Thread.sleep()` — use `WaitHelper`
- Use XPath unless nothing else works
- Use CSS class names like `.v-btn` or hash classes as locators
- Instantiate page objects in test classes — use Helper methods
- Use `Log.step()` inside helpers or page objects
- Share users between test methods
- Hardcode URLs — put them in properties files

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
