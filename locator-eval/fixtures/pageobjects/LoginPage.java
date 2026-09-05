package automation.modules;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/** Generated fixture page object -- the file the healer patches. */
public class LoginPage {

    private final Page page;

    public LoginPage(Page page) {
        this.page = page;
    }

    public Locator usernameField() {
        return page.locator("#user-name");
    }

    public Locator usernameByName() {
        return page.locator("[name=\"user-name\"]");
    }

    public Locator usernameNested() {
        return page.locator("#login_button_container .form_group > input[type=\"text\"]");
    }

    public Locator loginButton() {
        return page.locator("button#login-button");
    }

    public Locator loginByText() {
        return page.locator("button:has-text(\"Login\")");
    }

    public Locator loginNested() {
        return page.locator("#login_button_container form button");
    }
}
