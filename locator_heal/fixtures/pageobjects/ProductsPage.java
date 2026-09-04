package automation.modules;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.AriaRole;

/** Generated fixture page object -- the file the healer patches. */
public class ProductsPage {

    private final Page page;

    public ProductsPage(Page page) {
        this.page = page;
    }

    public Locator addBackpack() {
        return page.locator("[data-testid=\"add-backpack\"]");
    }

    public Locator sortDropdown() {
        return page.locator(".product_sort_container");
    }

    public Locator firstProduct() {
        return page.locator(".grid > .inventory_item:nth-child(1) button");
    }
}
