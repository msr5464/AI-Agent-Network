package automation.modules.demo;

import automation.core.api.ApiHelper;
import automation.core.config.TestConfig;
import automation.modules.demo.web.HomePage;
import com.microsoft.playwright.Page;

public class DemoHelper extends ApiHelper {

    private static final String BASE_URL = "https://example.com";

    public DemoHelper(TestConfig config) {
        super(config, BASE_URL);
    }

    public DemoHelper(TestConfig config, String customBaseUrl) {
        super(config, customBaseUrl != null ? customBaseUrl : BASE_URL);
    }

    public HomePage visitHome(Page page) {
        page.navigate(BASE_URL);
        return new HomePage(page);
    }
}
