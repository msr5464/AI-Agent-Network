package automation.modules;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/** Generated fixture page object -- the file the healer patches. */
public class SummaryPage {

    private final Page page;

    public SummaryPage(Page page) {
        this.page = page;
    }

    public Locator editButton() {
        return page.locator("div.rounded-2xl button[type=\"submit\"]");
    }
}
