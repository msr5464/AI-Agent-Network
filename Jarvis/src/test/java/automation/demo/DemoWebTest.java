package automation.demo;

import automation.core.TestBase;
import automation.core.annotations.TestVariables;
import automation.core.config.TestConfig;
import automation.core.enums.Country;
import automation.core.enums.Feature;
import automation.core.enums.QA;
import automation.core.enums.UserType;
import automation.core.helper.AssertHelper;
import automation.modules.demo.DemoHelper;
import automation.modules.demo.web.HomePage;
import org.testng.annotations.Test;

public class DemoWebTest extends TestBase {

    private static final String BASE_URL = "https://example.com";

    @Test(dataProvider = "getConfig", groups = {"web", "demo"})
    @TestVariables(automatedBy = QA.Mukesh, country = Country.SG)
    public void visitHome(TestConfig config) {
        config.logStep("Allocate Admin user");
        allocateUser(config, UserType.Admin, Feature.CARD, Country.SG);

        config.logStep("Navigate to the home page at the base URL");
        config.getPage().navigate(BASE_URL);

        config.logStep("Verify the home page loads");
        HomePage homePage = new HomePage(config.getPage());
        AssertHelper.assertTrue(homePage.isLoaded(), "Home page should be loaded");
    }
}
