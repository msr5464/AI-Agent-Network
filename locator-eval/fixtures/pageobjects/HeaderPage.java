package automation.modules;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/** Generated fixture page object -- the file the healer patches. */
public class HeaderPage {

    private final Page page;

    public HeaderPage(Page page) {
        this.page = page;
    }

    public Locator cartLink() {
        return page.locator(".shopping_cart_link");
    }

    public Locator cartCount() {
        return page.locator("[data-cart]");
    }
}
