# Fix Results

**Build Tag:** local-GitHubLoginTest  
**Attempt:** 1  
**Eligible tests:** 1 | **Distinct locator fixes:** 0 | **Tests verified:** 0 | **Applied but unverified:** 0 | **Failed:** 1  
**Gate:** `false`  
**PR Branch:** `none`

## What each attempt changed

**Attempt 1**
- `automation.github.GitHubLoginTest.verifyLoginOnGitHubUsingStoredSession` → **failed — reverted** in `DashboardPage.java`
  - The locator `img[class*='avatar']` is too broad and no longer matches GitHub's current header avatar element reliably. Updated to a multi-fallback CSS selector targeting the authenticated-user avatar specifically inside GitHub's AppHeader, including `img.avatar-user` (the user-specific avatar class GitHub uses) and `[data-login] img` as a robust logged-in indicator.

  ```diff
  --- a/DashboardPage.java
  +++ b/DashboardPage.java
  @@ -14,7 +14,7 @@
       public DashboardPage(Config config)
       {
           super(config);
  -        avatarWidget = page.locator("img[class*='avatar']").first();
  +        avatarWidget = page.locator("img.avatar-user, [data-login] img, .AppHeader-user img").first();
           userMenu     = page.locator("summary[aria-label*='View profile'], .AppHeader-user");
           assertPageLoaded(avatarWidget);
       }
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

