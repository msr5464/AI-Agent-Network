package automation.modules.demo.web;

import automation.core.web.BasePage;
import automation.core.web.WaitHelper;
import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;

public class HomePage extends BasePage {

    private final Locator pageBody;

    public HomePage(Page page) {
        super(page);
        this.pageBody = page.locator("[data-cy='page-body']");
        waitUntilLoaded();
    }

    @Override
    public void waitUntilLoaded() {
        WaitHelper.waitForVisible(pageBody);
    }

    public boolean isLoaded() {
        return isElementDisplayed(pageBody);
    }
}
