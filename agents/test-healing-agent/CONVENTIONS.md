# Thanos Automation Framework — Conventions for AI Auto-Fix

Read this before making any code change. Every rule here reflects existing patterns in this repo.
Violations will cause test failures or CI failures even if the locator itself is correct.

---

## 1. NEVER use raw Selenium — always use framework wrappers

This is the most critical rule. Raw Selenium calls bypass logging, retry logic, and wait handling.

### Clicking
```java
// CORRECT
Element.click(testConfig, element, "description");
Element.clickWithoutScroll(testConfig, element, "description");
Element.clickThroughJS(testConfig, element, "description");

// WRONG — do not write
element.click();
driver.findElement(By.id("x")).click();
```

### Typing / entering data
```java
// CORRECT
Element.enterData(testConfig, element, textToEnter, "description");
Element.clearData(testConfig, element, "description");

// WRONG
element.sendKeys("text");
element.clear();
```

### Reading text / attributes
```java
// CORRECT
String text = Element.getText(testConfig, element, "description");
String attr = Element.getAttributeText(testConfig, element, "attribute", "description");
boolean visible = Element.isElementDisplayed(testConfig, element);

// WRONG
element.getText();
element.getAttribute("attr");
```

### Dynamic element lookup (when @FindBy is not suitable)
```java
// CORRECT — use How enum
WebElement el = Element.getPageElement(testConfig, How.css, "[data-cy='selector']");
WebElement el = Element.getPageElement(testConfig, How.xPath, "//div[@data-cy='x']");
List<WebElement> els = Element.getPageElements(testConfig, How.css, ".class");
WebElement el = Element.getPageElementWithRetry(testConfig, How.css, "[data-cy='x']");  // for flaky elements
```

---

## 2. NEVER use Thread.sleep — always use WaitHelper

```java
// CORRECT — in action methods and verifications
WaitHelper.waitForElementToBeDisplayed(testConfig, element, "description");
WaitHelper.waitForElementToBeDisplayed(testConfig, element, "description", 15); // custom seconds
WaitHelper.waitForElementToBeClickable(testConfig, element, "description");
WaitHelper.waitForElementToBeHidden(testConfig, element, "description");
WaitHelper.waitForElementToBeDisplayedAndClickable(testConfig, element, "description");
WaitHelper.waitForSeconds(testConfig, 2);           // only when truly needed, not for elements

// WRONG
Thread.sleep(3000);
driver.manage().timeouts().implicitlyWait(5, TimeUnit.SECONDS);
```

### waitForPageLoad vs waitForElementToBeDisplayed — critical distinction

- `WaitHelper.waitForPageLoad(testConfig, element)` — **ONLY inside page object constructors**
- `WaitHelper.waitForElementToBeDisplayed(testConfig, element, "desc")` — **everywhere else** (action methods, verifications)

```java
// CORRECT: waitForPageLoad in constructor only
public LoginPage(Config testConfig) {
    super(testConfig);
    PageFactory.initElements(testConfig.driver, this);
    waitForLoaderDisappeared();
    WaitHelper.waitForPageLoad(testConfig, usernameField);  // ← OK here
}

// CORRECT: waitForElementToBeDisplayed in action methods
public void clickOnSubmitButton() {
    WaitHelper.waitForElementToBeDisplayed(testConfig, submitButton, "Submit button"); // ← NOT waitForPageLoad
    Element.click(testConfig, submitButton, "Submit button");
}

// WRONG: waitForPageLoad outside constructor
public void clickOnSubmitButton() {
    WaitHelper.waitForPageLoad(testConfig, submitButton);  // ← WRONG
}
```

---

## 3. Locator strategy — official priority: id > name > css > xpath

The project's official preference is: **`id` > `name` > `css` > `xpath`**

Within CSS, prefer `data-cy` attributes because they are explicitly added for test stability.

```java
// 1. id — highest priority (use when a stable id exists)
@FindBy(id = "login-username-field")

// 2. name attribute
@FindBy(name = "username")

// 3. CSS — preferred form: data-cy attribute (most stable CSS selector in this repo)
@FindBy(css = "[data-cy='login-step-start-username']")
// Also acceptable CSS: meaningful class names (not generated hashes)
@FindBy(css = ".text-h4.text-weight-bolder")

// 4. XPath — only when id/name/css cannot work
@FindBy(xpath = "//input[@data-cy='business-setting-fields-registration-types-input']")
// XPath with text: use contains(), never exact match
@FindBy(xpath = "//button[contains(text(),'Submit')]")
```

**NEVER use:**
- Positional XPath: `//div[1]/span[2]`
- Auto-generated class names that look like hashes
- Deep nesting: `//body/main/section/div/article/button`
- Exact text XPath: `//button[text()='Submit']` (use `contains()` instead)

### Fallback with @FindAll (first match wins)
```java
@FindAll({
    @FindBy(css = "[data-cy='preferred-selector']"),
    @FindBy(xpath = "//fallback/xpath")
})
private WebElement elementWithFallback;
```

---

## 4. Element declaration in page objects

All elements are declared as `private WebElement` fields with `@FindBy`.
Lists use `List<WebElement>`.
Static XPath/CSS strings for dynamic content use a descriptive field name ending in `XPath` or `Css`.

```java
// Single element
@FindBy(css = "[data-cy='element-id']")
private WebElement elementName;

// List of elements
@FindBy(css = ".el-tabs__item.is-top")
protected List<WebElement> tabItems;

// Dynamic selector (built at runtime via String.format)
private final String dynamicItemXpath = "//div[@data-cy='item-%s']";
// Usage: Element.getPageElement(testConfig, How.xPath, String.format(dynamicItemXpath, value))
```

**Element field naming:**
- `camelCase` ending with the element type: `usernameField`, `submitButton`, `successMessage`, `loadingSpinner`
- Page-scoped XPath variables: `blockReasonPopUpHeaderXpath`, `categoryItemValueXpath`

---

## 5. Page object constructor — mandatory pattern

Every page object constructor MUST follow this exact pattern:

```java
public LoginPage(Config testConfig) {
    super(testConfig);                                    // 1. call BasePage constructor
    this.testConfig = testConfig;
    PageFactory.initElements(testConfig.driver, this);   // 2. initialise @FindBy elements
    waitForLoaderDisappeared();                           // 3. wait for loading bars/spinners
    WaitHelper.waitForPageLoad(testConfig, titleElement); // 4. wait for page-specific anchor element
    verifyPageIsLoaded();                                 // 5. assert page loaded correctly
}
```

- `waitForLoaderDisappeared()` is inherited from `BasePage` — always call it
- Do NOT call `PageFactory.initElements` anywhere else
- The constructor must NOT take any parameter other than `Config testConfig`

---

## 6. Assertions — always use AssertHelper, never raw TestNG Assert

```java
// CORRECT
AssertHelper.assertElementIsDisplayed(testConfig, "description", element);
AssertHelper.assertElementIsNotDisplayed(testConfig, "description", element);
AssertHelper.assertElementText(testConfig, "description", expectedText, element);
AssertHelper.assertPartialElementText(testConfig, "description", partialText, element);
AssertHelper.compareEquals(testConfig, "description", expected, actual);
AssertHelper.compareContains(testConfig, "description", expectedText, actualText);
AssertHelper.compareTrue(testConfig, "description", condition);
AssertHelper.assertNotNull(testConfig, "description", object);

// WRONG
Assert.assertEquals(actual, expected);
assertTrue(condition);
element.getText().equals("something");
```

---

## 7. Imports — standard set for page objects

```java
import java.util.List;
import org.openqa.selenium.WebElement;
import org.openqa.selenium.support.FindBy;
import org.openqa.selenium.support.FindAll;
import org.openqa.selenium.support.PageFactory;
import Automation.Utils.AssertHelper;
import Automation.Utils.BasePage;
import Automation.Utils.Config;
import Automation.Utils.Element;
import Automation.Utils.Element.How;
import Automation.Utils.WaitHelper;
```

Do NOT import raw Selenium:
```java
// WRONG — do not add these
import org.openqa.selenium.By;
import org.openqa.selenium.support.ui.WebDriverWait;
import org.openqa.selenium.support.ui.ExpectedConditions;
```

---

## 8. Package and class naming

| What | Pattern | Example |
|---|---|---|
| Page object | `Automation.{Product}.customer.web` | `Automation.Access.customer.web.LoginPage` |
| Dash page | `Automation.{Product}.dash.web` | `Automation.Access.dash.web.DashBoardPage` |
| API details | `Automation.{Product}.customer.api` | `Automation.Access.customer.api.AccessApiDetails` |
| Helper | `Automation.{Product}.customer.helpers` | `Automation.Access.customer.helpers.AccessHelper` |
| Test class | `Automation.{Product}.{Module}.web.customer` | `Automation.Access.login.web.customer.TestLoginFlows` |
| Utilities | `Automation.Utils` | `Automation.Utils.Element` |

---

## 9. API test pattern — RestAssured via ApiHelper

Never call RestAssured directly. Use `ApiHelper` and `ApiDetails` enums.

```java
// CORRECT
ApiHelper apiHelper = new ApiHelper(testConfig);
Response response = apiHelper.executeRequestAndGetResponse(AccessApiDetails.PostAuthLogin, requestObject);
AssertHelper.compareEquals(testConfig, "Status Code", 200, response.statusCode());

// WRONG
RestAssured.given().post("/v1/auth/login");
```

---

## 10. Logging — logStep only in test classes, logComment everywhere else

`logStep` and `logComment` are NOT interchangeable — scope matters:

```java
// In TEST CLASSES only — use logStep()
logStep(testConfig, "Enter valid credentials and click Login");
logStep(testConfig, "Verify dashboard loads with correct user name displayed");

// In PAGE OBJECTS and HELPERS — use testConfig.logComment()
testConfig.logComment("Clicking submit button");
testConfig.logComment("Waiting for success toast message");

// WRONG — logStep in a page object or helper
logStep(testConfig, "clicking button");  // ← never in page objects/helpers
```

Other logging methods (all classes):
```java
testConfig.logPass("assertion passed");
testConfig.logFail("assertion failed — soft, test continues");
testConfig.logFailToEndExecution("hard failure — stops test immediately");
testConfig.logWarning("non-critical issue");
testConfig.logException("context message", exception);
```

`logStep` descriptions must be in plain English explaining the full action AND expected outcome so anyone can follow the test flow without reading the code.

---

## 11. Page object scope and navigation chaining

**Each page object contains ONLY the elements and actions for that specific page.**
A page object must not contain locators or methods that belong to another page.

**Navigation methods must return the next page object** — this enables fluent chaining:
```java
// CORRECT — method that navigates away returns the next page
public DashBoardPage clickOnLoginButton() {
    Element.click(testConfig, loginButton, "Login button");
    return new DashBoardPage(testConfig);
}

public SettingsPage clickOnSettingsIcon() {
    Element.click(testConfig, settingsIcon, "Settings icon");
    return new SettingsPage(testConfig);
}

// WRONG — navigation method returns void
public void clickOnLoginButton() {
    Element.click(testConfig, loginButton, "Login button");
    // caller has no way to get the next page
}
```

**Action-only methods (no navigation) return void:**
```java
public void inputUsername(String username) {
    Element.enterData(testConfig, usernameField, username, "Username field");
}
```

---

## 12. Helper vs Page Object — where to put functions

| Scenario | Where to put it |
|---|---|
| Single action on current page | Method in the **Page Object** |
| Multiple actions on the same page | Method in the **Page Object** (calls its own methods) |
| Actions spanning 2+ page objects | Method in the **Helper class** |
| Reusable test data / static strings | **StaticData Helper** (`{Product}StaticDataBase.java`) |

```java
// Helper method — involves LoginPage AND DashBoardPage (2 page objects) ← correct
public DashBoardPage doLogin() {
    loginPage = new LoginPage(testConfig);
    loginPage.enterUsername(testConfig.testData.get("Username"));
    loginPage.enterPassword(testConfig.testData.get("Password"));
    return loginPage.clickLoginButton();
}

// WRONG in helper — only touches one page object, belongs in LoginPage instead
public void enterUsername(String name) {
    loginPage.enterData(testConfig, usernameField, name, "Username");
}
```

**Test classes must not create page object instances directly** — always go through a Helper:**
```java
// CORRECT — test uses Helper
AccessHelper accessHelper = new AccessHelper(testConfig, 5);
accessHelper.doLogin();

// WRONG — test instantiates page object directly
LoginPage loginPage = new LoginPage(testConfig);
```

---

## 13. Code style rules (apply when writing or modifying any code)

### Use full variable names — no abbreviations
```java
// CORRECT
String merchantName = testConfig.testData.get("MerchantName");
String orderId = testConfig.testData.get("OrderId");

// WRONG
String merchName = ...;
String orderID = ...;   // note: Id not ID
```

### Avoid unnecessary variables — pass values directly
```java
// CORRECT
AssertHelper.compareEquals(testConfig, "Order amount", testConfig.testData.get("Amount"), actualAmount);

// WRONG — intermediate variable adds no value
String amount = testConfig.testData.get("Amount");
AssertHelper.compareEquals(testConfig, "Order amount", amount, actualAmount);
```

### No commented-out code, no System.out.println
```java
// WRONG — remove before committing
// loginPage.clickOldButton();
System.out.println("debug: " + response);
```

### Prefer assertElementText over assertElementDisplayed when text can be verified
```java
// CORRECT — verifies both presence AND correct text
AssertHelper.assertElementText(testConfig, "Success toast", "Login successful", toastMessage);

// LESS PREFERRED — only checks visibility, not content
AssertHelper.assertElementIsDisplayed(testConfig, "Success toast", toastMessage);
```

### Enum values use CamelCase, not ALL_CAPS
```java
// CORRECT
public enum ExpectedLandingPage { Dashboard, Transactions, Settings }

// WRONG
public enum ExpectedLandingPage { DASHBOARD, TRANSACTIONS, SETTINGS }
```

### Function names must describe the action they perform
```java
// CORRECT — name explains what it does
public void fillDetailsAndSubmitForm(String name, String amount) { ... }
public DashBoardPage loginWithValidCredentials() { ... }

// WRONG — too generic
public void doAction() { ... }
public void step1() { ... }
```

### URLs must not be hardcoded — put them in properties files
```java
// CORRECT
String url = testConfig.getRunTimeProperty("LoginUrl");

// WRONG
Browser.navigateToUrl(testConfig, "https://qa-1-fe.staging.example.com/login");
```

---

## 14. Running a single test (for verification after fix)

**Gradle:**
```bash
./gradlew test --tests "Automation.Access.login.web.customer.TestLoginFlows.testLoginWith3IncorrectPasswordAttempts"
./gradlew test --tests "Automation.Access.customer.web.LoginPage"
```

**Maven:**
```bash
mvn test -Dtest=TestLoginFlows#testLoginWith3IncorrectPasswordAttempts
```
