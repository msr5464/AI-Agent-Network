package automation.modules;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/** Generated fixture page object -- the file the healer patches. */
public class ProfilePage {

    private final Page page;

    public ProfilePage(Page page) {
        this.page = page;
    }

    public Locator saveButton() {
        return page.locator("#save-btn");
    }

    public Locator saveByText() {
        return page.locator("button:text-is(\"Save changes\")");
    }

    public Locator saveNested() {
        return page.locator("main > section#profile_section > button");
    }
}
