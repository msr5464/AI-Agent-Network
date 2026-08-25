# Fix Results

**Build Tag:** local-GitHubLoginTest  
**Attempt:** 2  
**Eligible tests:** 1 | **Distinct locator fixes:** 0 | **Tests verified:** 0 | **Applied but unverified:** 0 | **Failed:** 1  
**Gate:** `false`  
**PR Branch:** `none`

## What each attempt changed

**Attempt 1**
- `automation.github.GitHubLoginTest.verifyLoginOnGitHubUsingStoredSession` → **failed — reverted** in `DashboardPage.java`
  - The locator `img[class*='avatar']` was too broad — it matches any avatar image on the page (org icons, contributor thumbnails, etc.) rather than the authenticated user's nav widget. Replaced `avatarWidget` with a selector targeting the authenticated user header (`.AppHeader-user, [data-login]`) and updated both `assertPageLoaded` and `isLoggedIn()` to use it.

  ```diff
  --- a/DashboardPage.java
  +++ b/DashboardPage.java
  @@ -14,7 +14,7 @@
       public DashboardPage(Config config)
       {
           super(config);
  -        avatarWidget = page.locator("img[class*='avatar']").first();
  +        avatarWidget = page.locator(".AppHeader-user, [data-login]").first();
           userMenu     = page.locator("summary[aria-label*='View profile'], .AppHeader-user");
           assertPageLoaded(avatarWidget);
       }
  ```

**Attempt 2**
- `automation.github.GitHubLoginTest.verifyLoginOnGitHubUsingStoredSession` → **failed — reverted** in `DashboardPage.java`
  - The constructor used the user avatar as the assertPageLoaded anchor, so the constructor itself threw when the session was not active (avatar never rendered). Replaced the load anchor with the GitHub homepage link (always present in the nav), and updated isLoggedIn() to check for the [data-login] attribute element that GitHub renders only for authenticated users.

  ```diff
  --- a/DashboardPage.java
  +++ b/DashboardPage.java
  @@ -8,15 +8,17 @@
   public class DashboardPage extends BasePage
   {
   
  +    private final Locator pageAnchor;
       private final Locator avatarWidget;
       private final Locator userMenu;
   
       public DashboardPage(Config config)
       {
           super(config);
  -        avatarWidget = page.locator("img[class*='avatar']").first();
  +        pageAnchor   = page.locator("a[aria-label='Homepage']");
  +        avatarWidget = page.locator("[data-login]").first();
           userMenu     = page.locator("summary[aria-label*='View profile'], .AppHeader-user");
  -        assertPageLoaded(avatarWidget);
  +        assertPageLoaded(pageAnchor);
       }
   
       public boolean isLoggedIn()
  ```

## Failed Fixes

### ❌ automation.github.GitHubLoginTest.verifyLoginOnGitHubUsingStoredSession (`test_failed`)
- **Root Cause:** Failed to load Element Locator@img[class*='avatar'] >> nth=0 in DashboardPage
- **Fix applied but test still failing**
```
e].dumpstream.
[ERROR] -> [Help 1]
[ERROR] 
[ERROR] To see the full stack trace of the errors, re-run Maven with the -e switch.
[ERROR] Re-run Maven using the -X switch to enable full debug logging.
[ERROR] 
[ERROR] For more information about the errors and possible solutions, please read the following articles:
[ERROR] [Help 1] http://cwiki.apache.org/confluence/display/MAVEN/MojoFailureException
```

