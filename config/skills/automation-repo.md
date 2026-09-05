# Automation Repository — Skills & Conventions

This file is loaded as a system prompt (`--system-prompt-file`) by test-healing-agent when
generating locator fixes. It gives Claude persistent domain context about the automation
framework so it doesn't need to be re-explained in every fix prompt.

---

## Framework Overview

- **Language**: Java
- **Test framework**: TestNG
- **Browser automation**: Selenium WebDriver
- **API testing**: RestAssured via ApiHelper
- **Build tool**: Maven (run: `mvn test -Dtest=ClassName#methodName`)
- **Page object pattern**: Custom wrapper classes extending `BasePage`

---

## Critical Rule #1: NEVER use raw Selenium — always use framework wrappers

### Clicking
```java
// CORRECT
Element.click(testConfig, element, "description");
Element.clickWithoutScroll(testConfig, element, "description");
Element.clickThroughJS(testConfig, element, "description");

// WRONG
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
```

### Reading text / attributes
```java
// CORRECT
String text = Element.getText(testConfig, element, "description");
boolean visible = Element.isElementDisplayed(testConfig, element);

// WRONG
element.getText();
```

### Dynamic element lookup
```java
// CORRECT — use How enum
WebElement el = Element.getPageElement(testConfig, How.css, "[data-cy='selector']");
WebElement el = Element.getPageElementWithRetry(testConfig, How.css, "[data-cy='x']");
```

---

## Critical Rule #2: NEVER use Thread.sleep — always use WaitHelper

```java
// CORRECT
WaitHelper.waitForElementToBeDisplayed(testConfig, element, "description");
WaitHelper.waitForElementToBeClickable(testConfig, element, "description");
WaitHelper.waitForElementToBeHidden(testConfig, element, "description");

// WRONG
Thread.sleep(3000);
```

### waitForPageLoad vs waitForElementToBeDisplayed

- `WaitHelper.waitForPageLoad(testConfig, element)` — **ONLY inside page object constructors**
- `WaitHelper.waitForElementToBeDisplayed(testConfig, element, "desc")` — **everywhere else**

---

## Locator Strategy — Priority Order

1. `id` — highest priority when stable id exists
2. `name` attribute
3. `css [data-cy='...']` — most stable CSS in this repo
4. `css` — meaningful class names (not generated hashes)
5. `xpath` — only when id/name/css cannot work; use `contains()`, never exact text match

**NEVER use:**
- Positional XPath: `//div[1]/span[2]`
- Auto-generated class names (hash-like strings)
- Exact text XPath: `//button[text()='Submit']` — use `contains()` instead

### @FindBy declaration pattern
```java
// Single element
@FindBy(css = "[data-cy='element-id']")
private WebElement elementName;

// List of elements
@FindBy(css = ".el-tabs__item.is-top")
protected List<WebElement> tabItems;

// Fallback with @FindAll (first match wins)
@FindAll({
    @FindBy(css = "[data-cy='preferred-selector']"),
    @FindBy(xpath = "//fallback/xpath")
})
private WebElement elementWithFallback;
```

---

## Page Object Constructor — Mandatory Pattern

```java
public LoginPage(Config testConfig) {
    super(testConfig);                                    // 1. call BasePage constructor
    this.testConfig = testConfig;
    PageFactory.initElements(testConfig.driver, this);   // 2. initialise @FindBy elements
    waitForLoaderDisappeared();                           // 3. wait for loading bars/spinners
    WaitHelper.waitForPageLoad(testConfig, titleElement); // 4. wait for page anchor element
    verifyPageIsLoaded();                                 // 5. assert page loaded correctly
}
```

---

## Assertions — Always use AssertHelper

```java
// CORRECT
AssertHelper.assertElementIsDisplayed(testConfig, "description", element);
AssertHelper.assertElementText(testConfig, "description", expectedText, element);
AssertHelper.compareEquals(testConfig, "description", expected, actual);

// WRONG
Assert.assertEquals(actual, expected);
assertTrue(condition);
```

---

## Navigation Methods — Return Next Page Object

```java
// CORRECT — navigation returns next page
public DashBoardPage clickOnLoginButton() {
    Element.click(testConfig, loginButton, "Login button");
    return new DashBoardPage(testConfig);
}

// WRONG — returns void
public void clickOnLoginButton() {
    Element.click(testConfig, loginButton, "Login button");
}
```

---

## Logging Scope

- **Test classes only**: `logStep(testConfig, "description")`
- **Page objects and helpers**: `testConfig.logComment("description")`
- Never use `System.out.println`
- Never use `logStep` in page objects or helpers
- **One `logStep` per step, never one summary line.** The report prints one line per
  `logStep`, so a scenario narrated once fails with a report that cannot say which
  step broke. Each `logStep` goes immediately before the call(s) it describes:

```java
// WRONG — one line for a four-step scenario
logStep(testConfig, "Login, toggle the trailing dot in the summary, save, and verify it persists");
String[] result = helper.toggleProfileSummaryDot(username, password);

// RIGHT — one line per step, each in front of the calls that carry it out
logStep(testConfig, "Login to Naukri and open the profile page");
ProfilePage profile = helper.loginAndOpenProfile(username, password);

logStep(testConfig, "Toggle the trailing dot in Profile Summary and save the change");
String saved = profile.toggleTrailingDotAndSave();

logStep(testConfig, "Verify the summary shown after reload matches the saved value");
AssertHelper.assertEquals(testConfig, profile.reload().getProfileSummary(), saved,
    "Profile Summary after reload should match the saved modified summary");
```

  Setup lines (reading properties or credentials, constructing a helper) get no
  `logStep`. A helper may encapsulate one step; it must not swallow the whole
  scenario, because then there is nothing left for the test to narrate.

---

## Running a Single Test (for verification)

```bash
# Maven
mvn test -Dtest=TestLoginFlows#testLoginWith3IncorrectPasswordAttempts

# Gradle
./gradlew test --tests "Automation.Access.login.web.customer.TestLoginFlows.testLoginWith3IncorrectPasswordAttempts"
```

---

## Standard Imports for Page Objects

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

**Do NOT import raw Selenium:**
```java
// WRONG
import org.openqa.selenium.By;
import org.openqa.selenium.support.ui.WebDriverWait;
```
